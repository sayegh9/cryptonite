"""Deterministic scanner stop and target calculation shared with on-demand debates.

The calculation is extracted from ScannerEngine.evaluate. Only closed higher-timeframe bars enter
its target selection; no model-suggested price is accepted here.
"""

import math
from decimal import Decimal
from typing import Any

import polars as pl
from cryptonite_scanner.models import RiskConfig


def derive_levels(
    row: dict[str, Any], risk_config: RiskConfig, direction: str, price_precision: int,
    enriched_htf: pl.DataFrame | None = None,
) -> tuple[Decimal, Decimal, Decimal, str, str, float, Decimal] | None:
    """Return entry, stop, target, stop type, target type, R:R, exact R:R, or None for bad inputs."""
    raw_atr = row.get("atr")
    if (not isinstance(raw_atr, (int, float)) or not math.isfinite(raw_atr) or raw_atr <= 0
            or price_precision < 0):
        return None
    stop_mode = getattr(risk_config, "stop_mode", "structure")
    min_atr_mult = float(getattr(risk_config, "min_atr_mult", 0.5))
    max_atr_mult = float(getattr(risk_config, "max_atr_mult", 4.0))
    fallback_stop_atr_mult = float(getattr(risk_config, "fallback_stop_atr_mult", 2.0))
    fallback_target_atr_mult = float(getattr(risk_config, "fallback_target_atr_mult", 3.0))
    entry_dec = round(Decimal(str(row["close"])), price_precision)
    entry_float = float(entry_dec)
    atr_float = float(raw_atr)
    signal_bar_end_ns = row["interval_end_exclusive_ns"]

    stop_val: float
    stop_type: str
    target_val: float | None = None
    target_type: str | None = None

    if direction == "SHORT":
        # SHORT STOP
        if stop_mode in ("structure", "donchian"):
            d_high = row.get("donchian_upper")
            if d_high is not None and isinstance(d_high, (int, float)) and math.isfinite(d_high):
                d_high_float = float(d_high)
                dist = d_high_float - entry_float
                min_dist = min_atr_mult * atr_float
                max_dist = max_atr_mult * atr_float
                if d_high_float > entry_float and min_dist <= dist <= max_dist:
                    stop_val = d_high_float
                    stop_type = "donchian_high"
                else:
                    stop_val = entry_float + (fallback_stop_atr_mult * atr_float)
                    stop_type = "atr_fallback"
            else:
                stop_val = entry_float + (fallback_stop_atr_mult * atr_float)
                stop_type = "atr_fallback"
        else:
            stop_val = entry_float + (float(risk_config.stop_loss_atr_mult) * atr_float)
            stop_type = "atr_fixed"

        # SHORT TARGET
        if enriched_htf is not None and not enriched_htf.is_empty():
            closed_htf = enriched_htf.filter(pl.col("interval_end_exclusive_ns") <= signal_bar_end_ns)
            if not closed_htf.is_empty():
                latest_htf = closed_htf.slice(-1, 1).to_dicts()[0]
                htf_low = latest_htf.get("donchian_lower")
                htf_bb_low = latest_htf.get("bb_lower")
                min_target_dist = 1.5 * atr_float
                max_target_dist = 6.0 * atr_float

                if htf_low is not None and isinstance(htf_low, (int, float)) and math.isfinite(htf_low):
                    htf_low_float = float(htf_low)
                    dist = entry_float - htf_low_float
                    if htf_low_float < entry_float and min_target_dist <= dist <= max_target_dist:
                        target_val = htf_low_float
                        target_type = "htf_donchian_low"

                if target_val is None and htf_bb_low is not None and isinstance(htf_bb_low, (int, float)) and math.isfinite(htf_bb_low):
                    htf_bb_float = float(htf_bb_low)
                    dist = entry_float - htf_bb_float
                    if htf_bb_float < entry_float and min_target_dist <= dist <= max_target_dist:
                        target_val = htf_bb_float
                        target_type = "htf_bb_lower"

        if target_val is None:
            target_mult = fallback_target_atr_mult if stop_mode in ("structure", "donchian") else float(risk_config.take_profit_atr_mult)
            target_val = entry_float - (target_mult * atr_float)
            target_type = "atr_fallback" if stop_mode in ("structure", "donchian") else "atr_fixed"

    else:
        # LONG STOP
        if stop_mode in ("structure", "donchian"):
            d_low = row.get("donchian_lower")
            if d_low is not None and isinstance(d_low, (int, float)) and math.isfinite(d_low):
                d_low_float = float(d_low)
                dist = entry_float - d_low_float
                min_dist = min_atr_mult * atr_float
                max_dist = max_atr_mult * atr_float
                if d_low_float < entry_float and min_dist <= dist <= max_dist:
                    stop_val = d_low_float
                    stop_type = "donchian_low"
                else:
                    stop_val = entry_float - (fallback_stop_atr_mult * atr_float)
                    stop_type = "atr_fallback"
            else:
                stop_val = entry_float - (fallback_stop_atr_mult * atr_float)
                stop_type = "atr_fallback"
        else:
            stop_val = entry_float - (float(risk_config.stop_loss_atr_mult) * atr_float)
            stop_type = "atr_fixed"

        # LONG TARGET
        if enriched_htf is not None and not enriched_htf.is_empty():
            closed_htf = enriched_htf.filter(pl.col("interval_end_exclusive_ns") <= signal_bar_end_ns)
            if not closed_htf.is_empty():
                latest_htf = closed_htf.slice(-1, 1).to_dicts()[0]
                htf_high = latest_htf.get("donchian_upper")
                htf_bb_up = latest_htf.get("bb_upper")
                min_target_dist = 1.5 * atr_float
                max_target_dist = 6.0 * atr_float

                if htf_high is not None and isinstance(htf_high, (int, float)) and math.isfinite(htf_high):
                    htf_high_float = float(htf_high)
                    dist = htf_high_float - entry_float
                    if htf_high_float > entry_float and min_target_dist <= dist <= max_target_dist:
                        target_val = htf_high_float
                        target_type = "htf_donchian_high"

                if target_val is None and htf_bb_up is not None and isinstance(htf_bb_up, (int, float)) and math.isfinite(htf_bb_up):
                    htf_bb_float = float(htf_bb_up)
                    dist = htf_bb_float - entry_float
                    if htf_bb_float > entry_float and min_target_dist <= dist <= max_target_dist:
                        target_val = htf_bb_float
                        target_type = "htf_bb_upper"

        if target_val is None:
            target_mult = fallback_target_atr_mult if stop_mode in ("structure", "donchian") else float(risk_config.take_profit_atr_mult)
            target_val = entry_float + (target_mult * atr_float)
            target_type = "atr_fallback" if stop_mode in ("structure", "donchian") else "atr_fixed"

    # Quantize levels
    stop_dec = round(Decimal(str(stop_val)), price_precision)
    target_dec = round(Decimal(str(target_val)), price_precision)
    risk = abs(entry_dec - stop_dec)
    reward = abs(target_dec - entry_dec)
    derived_rr = (reward / risk) if risk > Decimal("0") else Decimal("0")
    rr_float = float(round(derived_rr, 4))
    return entry_dec, stop_dec, target_dec, stop_type, target_type, rr_float, derived_rr
