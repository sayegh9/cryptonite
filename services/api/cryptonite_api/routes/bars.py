"""Historical bars serving route using DuckDB and strict 1m-derived resampler."""

from typing import Any

from cryptonite_scanner.resampler import resample_1m_bars
from cryptonite_storage.queries import AnalyticsQueryEngine
from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/v1/bars", tags=["bars"])


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

    # Single-source rule: query 1m base bars from Analytics Lake
    df_1m = engine.query_bars_df(symbol=sym, timeframe="1m", start_ns=start_ns, end_ns=end_ns)
    if df_1m.is_empty():
        return []

    # Resample to target timeframe with strict completeness validation
    try:
        resampled_df = resample_1m_bars(df_1m, tf)
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
