"""Historical bars serving route using DuckDB and strict 1m-derived resampler."""

import asyncio
from typing import Any

import polars as pl
from cryptonite_scanner.resampler import resample_1m_bars
from cryptonite_storage.queries import AnalyticsQueryEngine
from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/v1/bars", tags=["bars"])


def load_resampled_bars(
    engine: AnalyticsQueryEngine,
    symbol: str,
    timeframe: str,
    start_ns: int | None,
    end_ns: int | None,
) -> pl.DataFrame:
    """Blocking lake read and resample, run in a worker thread by ``get_bars``.

    Run on the event loop, this read stalled every other request for seconds. redis-py's 5 s socket timeout then
    failed the session and price reads. A window reads only the partitions that can hold it.
    """
    df_1m = engine.query_bars_df(symbol=symbol, timeframe="1m", start_ns=start_ns, end_ns=end_ns)
    return resample_1m_bars(df_1m, timeframe) if not df_1m.is_empty() else df_1m


@router.get("")
async def get_bars(
    request: Request,
    symbol: str = Query(..., description="Symbol e.g. BTCUSDT"),
    timeframe: str = Query("1m", description="Timeframe e.g. 1m, 5m, 15m, 1h, 4h, 1d"),
    start_time_ms: int | None = Query(None, description="Start time in milliseconds"),
    end_time_ms: int | None = Query(None, description="End time in milliseconds"),
    limit: int = Query(500, le=2000, description="Max bars to return (max 2000)"),
) -> list[dict[str, Any]]:
    """Fetch historical OHLCV bars formatted specifically for Lightweight Charts."""
    engine: AnalyticsQueryEngine = request.app.state.query_engine
    sym = symbol.upper()
    tf = timeframe.lower()

    start_ns = start_time_ms * 1_000_000 if start_time_ms is not None else None
    end_ns = end_time_ms * 1_000_000 if end_time_ms is not None else None

    # Single-source rule: query 1m base bars from Analytics Lake, resampled with strict completeness validation.
    # The read is synchronous, so it runs in a worker thread, off the event loop.
    try:
        resampled_df = await asyncio.to_thread(load_resampled_bars, engine, sym, tf, start_ns, end_ns)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if resampled_df.is_empty():
        return []

    # Apply limit to latest bars
    if len(resampled_df) > limit:
        resampled_df = resampled_df.tail(limit)

    rows = resampled_df.to_dicts()
    result: list[dict[str, Any]] = []

    for r in rows:
        open_ns = r["bar_open_ns"]
        result.append({
            "time": open_ns // 1_000_000_000,  # Unix seconds for Lightweight Charts
            "bar_open_ns": open_ns,
            "bar_close_inclusive_ns": r["bar_close_inclusive_ns"],
            "interval_end_exclusive_ns": r["interval_end_exclusive_ns"],
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r["volume"]),
            "quote_volume": float(r["quote_volume"]),
            "trades_count": int(r["trades_count"]),
        })

    return result
