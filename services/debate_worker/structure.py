"""Fail-closed structural levels for operator-triggered debates.

The scanner's strict 1m resampler, indicator enrichment and pure level calculation are reused.
No model value enters the stop, target or price precision. Missing, stale or gapped lake data
produces no levels; routing then records NO_STRUCTURE before any entry approval or command.
"""

import asyncio
import logging
import os
import time
from pathlib import Path

import polars as pl
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.levels import derive_levels
from cryptonite_scanner.models import RiskConfig
from cryptonite_scanner.resampler import TIMEFRAME_MINUTES, resample_1m_bars
from cryptonite_storage.queries import AnalyticsQueryEngine, lake_symbol

logger = logging.getLogger("cryptonite.debate_worker.structure")
ONE_MINUTE_NS = 60 * 1_000_000_000
LOOKBACK_HIGHER_BARS = 230  # 200-bar indicator warm-up plus closed-bar and gap allowance.


def levels_from_lake(symbol: str, style: str, side: str, precision: int, *,
                     decision_ns: int | None = None, lake_path: str | Path | None = None) -> tuple[float | None, float | None, str]:
    """Return (stop, target, diagnostic) from fresh, complete closed bars, or no levels."""
    try:
        symbol = lake_symbol(symbol)
        if style == "SWING":
            decision_tf, higher_tf = "1h", "4h"
        elif style == "INTRADAY":
            decision_tf, higher_tf = "15m", "1h"
        else:
            return None, None, "profile style is unsupported"
        if side not in ("BUY", "SELL") or not isinstance(precision, int) or not 0 <= precision <= 18:
            return None, None, "side or instrument price precision is unavailable"
        now_ns = decision_ns if decision_ns is not None else time.time_ns()
        higher_ns = TIMEFRAME_MINUTES[higher_tf] * ONE_MINUTE_NS
        decision_step_ns = TIMEFRAME_MINUTES[decision_tf] * ONE_MINUTE_NS
        lake = AnalyticsQueryEngine(lake_path or Path(os.getenv("ANALYTICS_LAKE_PATH", "/app/data/analytics_lake")))
        try:
            bars = lake.query_bars_df(symbol, "1m", now_ns - LOOKBACK_HIGHER_BARS * higher_ns, now_ns)
        finally:
            lake._con.close()
        if bars.is_empty():
            return None, None, "1m lake bars are unavailable"
        decision = resample_1m_bars(bars, decision_tf)
        higher = resample_1m_bars(bars, higher_tf)
        if decision.is_empty() or higher.is_empty():
            return None, None, "closed decision or higher-timeframe bars are unavailable"
        decision = ScannerEngine.enrich_indicators(decision).filter(pl.col("interval_end_exclusive_ns") <= now_ns)
        higher = ScannerEngine.enrich_indicators(higher).filter(pl.col("interval_end_exclusive_ns") <= now_ns)
        if decision.is_empty() or higher.is_empty():
            return None, None, "closed decision or higher-timeframe bars are unavailable"
        row = decision.tail(1).to_dicts()[0]
        age_ns = now_ns - int(row["interval_end_exclusive_ns"])
        if age_ns > 2 * decision_step_ns:
            return None, None, f"latest {decision_tf} bar is older than two bars"
        if row.get("ema_200") is None or row.get("atr") is None:
            return None, None, f"fewer than 200 valid {decision_tf} bars"
        config = RiskConfig(direction="LONG" if side == "BUY" else "SHORT")
        calculated = derive_levels(row, config, config.direction, precision, higher)
        if calculated is None:
            return None, None, "scanner level inputs are invalid"
        entry, stop, target, _, _, _, _ = calculated
        if not (stop < entry < target if side == "BUY" else target < entry < stop):
            return None, None, "calculated levels are on the wrong side of the entry"
        return float(stop), float(target), "scanner levels from closed lake bars"
    except Exception as exc:
        logger.warning("Structure unavailable for %s: %s", symbol, exc)
        return None, None, "lake level calculation failed"


async def on_demand_levels(pg_pool, instrument_id: str, style: str, side: str) -> tuple[float | None, float | None, str]:
    """Read precision from the instrument registry, then calculate outside the event loop."""
    symbol = instrument_id.split(".", 1)[0].removesuffix("-PERP").removesuffix("-MARGIN")
    try:
        async with pg_pool.acquire() as conn:
            precision = await conn.fetchval(
                "SELECT price_precision FROM instruments WHERE instrument_id = $1 AND is_active = true",
                f"{symbol}.BINANCE",
            )
        if precision is None:
            return None, None, "instrument price precision is unavailable"
        return await asyncio.to_thread(levels_from_lake, symbol, style, side, int(precision))
    except Exception as exc:
        logger.warning("Could not load instrument precision for %s: %s", instrument_id, exc)
        return None, None, "instrument price precision is unavailable"
