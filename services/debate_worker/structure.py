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
from cryptonite_scanner.presets import preset_by_slug
from cryptonite_scanner.resampler import TIMEFRAME_MINUTES, resample_1m_bars
from cryptonite_schemas.profile import preset_ids_for
from cryptonite_storage.queries import AnalyticsQueryEngine, lake_symbol

logger = logging.getLogger("cryptonite.debate_worker.structure")
ONE_MINUTE_NS = 60 * 1_000_000_000
LOOKBACK_HIGHER_BARS = 230  # 200-bar indicator warm-up plus closed-bar and gap allowance.


def levels_from_lake(symbol: str, style: str, side: str, precision: int, *,
                     decision_ns: int | None = None, lake_path: str | Path | None = None,
                     preset_id: str | None = None,
                     ) -> tuple[float | None, float | None, str, str | None]:
    """Return (stop, target, diagnostic, source) from fresh, complete closed bars, or no levels.

    ``source`` is ``structure:<preset_id>``: ``preset_id`` when given (the profile's first pick for this side),
    else the first preset the profile style runs for this side (D-P3-7); the levels use its risk config."""
    try:
        symbol = lake_symbol(symbol)
        if style == "SWING":
            decision_tf, higher_tf = "1h", "4h"
        elif style == "INTRADAY":
            decision_tf, higher_tf = "15m", "1h"
        else:
            return None, None, "profile style is unsupported", None
        if side not in ("BUY", "SELL") or not isinstance(precision, int) or not 0 <= precision <= 18:
            return None, None, "side or instrument price precision is unavailable", None
        now_ns = decision_ns if decision_ns is not None else time.time_ns()
        higher_ns = TIMEFRAME_MINUTES[higher_tf] * ONE_MINUTE_NS
        decision_step_ns = TIMEFRAME_MINUTES[decision_tf] * ONE_MINUTE_NS
        lake = AnalyticsQueryEngine(lake_path or Path(os.getenv("ANALYTICS_LAKE_PATH", "/app/data/analytics_lake")))
        try:
            bars = lake.query_bars_df(symbol, "1m", now_ns - LOOKBACK_HIGHER_BARS * higher_ns, now_ns)
        finally:
            lake._con.close()
        if bars.is_empty():
            return None, None, "1m lake bars are unavailable", None
        decision = resample_1m_bars(bars, decision_tf)
        higher = resample_1m_bars(bars, higher_tf)
        if decision.is_empty() or higher.is_empty():
            return None, None, "closed decision or higher-timeframe bars are unavailable", None
        decision = ScannerEngine.enrich_indicators(decision).filter(pl.col("interval_end_exclusive_ns") <= now_ns)
        higher = ScannerEngine.enrich_indicators(higher).filter(pl.col("interval_end_exclusive_ns") <= now_ns)
        if decision.is_empty() or higher.is_empty():
            return None, None, "closed decision or higher-timeframe bars are unavailable", None
        # Each aggregate is complete, but an indicator can still span missing whole candles. Keep the
        # trailing 200 decision bars contiguous so EMA, ATR and Donchian levels describe one live window.
        ends = decision.tail(200)["interval_end_exclusive_ns"].to_list()
        if len(ends) < 200 or any(b - a != decision_step_ns for a, b in zip(ends, ends[1:])):
            return None, None, f"fewer than 200 contiguous {decision_tf} bars", None
        row = decision.tail(1).to_dicts()[0]
        age_ns = now_ns - int(row["interval_end_exclusive_ns"])
        if age_ns > 2 * decision_step_ns:
            return None, None, f"latest {decision_tf} bar is older than two bars", None
        if now_ns - int(higher["interval_end_exclusive_ns"][-1]) > 2 * higher_ns:
            return None, None, f"latest {higher_tf} bar is older than two bars", None
        if row.get("ema_200") is None or row.get("atr") is None:
            return None, None, f"fewer than 200 valid {decision_tf} bars", None
        direction = "LONG" if side == "BUY" else "SHORT"
        preset_id = preset_id or preset_ids_for(style, direction)[0]
        config = preset_by_slug(preset_id).risk.model_copy(update={"direction": direction})
        calculated = derive_levels(row, config, direction, precision, higher)
        if calculated is None:
            return None, None, "scanner level inputs are invalid", None
        entry, stop, target, _, _, _, _ = calculated
        if not (stop < entry < target if side == "BUY" else target < entry < stop):
            return None, None, "calculated levels are on the wrong side of the entry", None
        return float(stop), float(target), "scanner levels from closed lake bars", f"structure:{preset_id}"
    except Exception as exc:
        logger.warning("Structure unavailable for %s: %s", symbol, exc)
        return None, None, "lake level calculation failed", None


async def on_demand_levels(pg_pool, instrument_id: str, style: str, side: str, preset_id: str | None = None,
                           ) -> tuple[float | None, float | None, str, str | None]:
    """Read precision from the instrument registry, then calculate outside the event loop."""
    symbol = instrument_id.split(".", 1)[0].removesuffix("-PERP").removesuffix("-MARGIN")
    try:
        async with pg_pool.acquire() as conn:
            precision = await conn.fetchval(
                "SELECT price_precision FROM instruments WHERE instrument_id = $1 AND is_active = true",
                f"{symbol}.BINANCE",
            )
        if precision is None:
            return None, None, "instrument price precision is unavailable", None
        return await asyncio.to_thread(levels_from_lake, symbol, style, side, int(precision), preset_id=preset_id)
    except Exception as exc:
        logger.warning("Could not load instrument precision for %s: %s", instrument_id, exc)
        return None, None, "instrument price precision is unavailable", None
