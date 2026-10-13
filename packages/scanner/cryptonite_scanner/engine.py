"""Vectorized Polars Scanner Rule Evaluation Engine.

Evaluates multi-factor rule groups over resampled OHLCV DataFrames and returns
strictly analytical SignalCandidate records. Completely isolated from execution.
"""

import logging
import math
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import polars as pl

from cryptonite_scanner.indicators import (
    calc_atr,
    calc_bollinger_bands,
    calc_donchian_channel,
    calc_ema,
    calc_macd,
    calc_rsi,
    calc_volume_ratio,
)
from cryptonite_scanner.models import (
    RiskConfig,
    RuleCondition,
    RuleGroup,
    RuleOperator,
    SignalCandidateRead,
    deduplicate_candidates,
)

logger = logging.getLogger("cryptonite.scanner.engine")


class ScannerEngine:
    """Vectorized rule evaluator over OHLCV DataFrames."""

    @staticmethod
    def enrich_indicators(df: pl.DataFrame) -> pl.DataFrame:
        """Enrich OHLCV DataFrame with standard technical indicator columns."""
        if df.is_empty():
            return df

        bb_lower, bb_mid, bb_upper = calc_bollinger_bands("close", 20, 2.0)
        macd, macd_sig, macd_hist = calc_macd("close", 12, 26, 9)
        donchian_lower, donchian_mid, donchian_upper = calc_donchian_channel("high", "low", 20)

        return df.with_columns(
            ema_20=calc_ema("close", 20),
            ema_50=calc_ema("close", 50),
            ema_200=calc_ema("close", 200),
            rsi=calc_rsi("close", 14),
            bb_lower=bb_lower,
            bb_middle=bb_mid,
            bb_upper=bb_upper,
            macd=macd,
            macd_signal=macd_sig,
            macd_hist=macd_hist,
            atr=calc_atr("high", "low", "close", 14),
            volume_ratio=calc_volume_ratio("volume", 20),
            donchian_lower=donchian_lower,
            donchian_middle=donchian_mid,
            donchian_upper=donchian_upper,
        )

    @classmethod
    def evaluate(
        cls,
        df: pl.DataFrame,
        rule_groups: list[RuleGroup],
        risk_config: RiskConfig,
        instrument_id: str,
        timeframe: str,
        spec_version_id: UUID,
        scan_run_id: UUID,
        price_precision: int | None = 2,
        htf_df: pl.DataFrame | None = None,
        filter_min_rr: bool = False,
    ) -> list[SignalCandidateRead]:
        """Evaluate rule groups against the DataFrame and produce SignalCandidate records."""
        if price_precision is None or not isinstance(price_precision, int) or price_precision < 0:
            logger.error(
                "Missing or invalid price_precision for instrument %s (value=%s). Failing closed.",
                instrument_id,
                price_precision,
            )
            return []

        if df.is_empty():
            return []

        if "donchian_upper" in df.columns:
            enriched = df
        else:
            enriched = cls.enrich_indicators(df)

        # Drop warm-up rows where indicators are null
        valid_df = enriched.filter(
            pl.col("rsi").is_not_null()
            & pl.col("ema_200").is_not_null()
            & pl.col("atr").is_not_null()
        )

        if valid_df.is_empty():
            return []

        # Prepare enriched HTF DataFrame if provided
        enriched_htf: pl.DataFrame | None = None
        if htf_df is not None and not htf_df.is_empty():
            if "donchian_upper" in htf_df.columns and "bb_upper" in htf_df.columns:
                enriched_htf = htf_df
            else:
                enriched_htf = cls.enrich_indicators(htf_df)

        # Evaluate each rule group into a boolean mask expression
        group_masks: list[pl.Expr] = []
        rule_eval_details: list[tuple[RuleGroup, list[tuple[RuleCondition, pl.Expr]]]] = []

        for group in rule_groups:
            if not group.conditions:
                continue

            cond_exprs: list[pl.Expr] = []
            group_cond_details: list[tuple[RuleCondition, pl.Expr]] = []

            for cond in group.conditions:
                cond_expr = cls._build_condition_expr(cond)
                cond_exprs.append(cond_expr)
                group_cond_details.append((cond, cond_expr))

            rule_eval_details.append((group, group_cond_details))

            if group.logic.upper() == "OR":
                group_mask = pl.any_horizontal(cond_exprs)
            else:
                group_mask = pl.all_horizontal(cond_exprs)

            group_masks.append(group_mask)

        if not group_masks:
            return []

        total_mask = pl.all_horizontal(group_masks)
        matched_df = valid_df.filter(total_mask)

        if matched_df.is_empty():
            return []

        # Build candidate records
        candidates: list[SignalCandidateRead] = []
        rows = matched_df.to_dicts()

        direction = (getattr(risk_config, "direction", "LONG") or "LONG").upper()
        stop_mode = getattr(risk_config, "stop_mode", "structure")
        min_atr_mult = float(getattr(risk_config, "min_atr_mult", 0.5))
        max_atr_mult = float(getattr(risk_config, "max_atr_mult", 4.0))
        fallback_stop_atr_mult = float(getattr(risk_config, "fallback_stop_atr_mult", 2.0))
        fallback_target_atr_mult = float(getattr(risk_config, "fallback_target_atr_mult", 3.0))

        for row in rows:
            # Validate finite positive ATR
            raw_atr = row.get("atr")
            if raw_atr is None or not isinstance(raw_atr, (int, float)) or not math.isfinite(raw_atr) or raw_atr <= 0:
                continue

            matched_explanations: list[str] = []
            for group, cond_details in rule_eval_details:
                for cond, _ in cond_details:
                    actual_val = row.get(cond.field)
                    if actual_val is not None:
                        prec_fmt = (
                            price_precision
                            if cond.field in ("close", "open", "high", "low", "ema_20", "ema_50", "ema_200", "bb_lower", "bb_upper", "donchian_lower", "donchian_upper", "atr")
                            else 2
                        )
                        matched_explanations.append(
                            f"[{group.category.upper()}] {cond.field} ({float(actual_val):.{prec_fmt}f}) {cond.operator} {cond.value}"
                        )

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

            if filter_min_rr and derived_rr < Decimal(str(risk_config.min_risk_reward)):
                continue

            # Snapshot metrics
            metrics_snapshot: dict[str, Any] = {
                "price": round(float(row["close"]), price_precision),
                "rsi": round(float(row["rsi"]), 2) if row.get("rsi") is not None else None,
                "ema_20": round(float(row["ema_20"]), price_precision) if row.get("ema_20") is not None else None,
                "ema_50": round(float(row["ema_50"]), price_precision) if row.get("ema_50") is not None else None,
                "ema_200": round(float(row["ema_200"]), price_precision) if row.get("ema_200") is not None else None,
                "bb_lower": round(float(row["bb_lower"]), price_precision) if row.get("bb_lower") is not None else None,
                "bb_upper": round(float(row["bb_upper"]), price_precision) if row.get("bb_upper") is not None else None,
                "atr": round(float(row["atr"]), price_precision) if row.get("atr") is not None else None,
                "volume_ratio": round(float(row["volume_ratio"]), 2) if row.get("volume_ratio") is not None else None,
                "donchian_lower": round(float(row["donchian_lower"]), price_precision) if row.get("donchian_lower") is not None else None,
                "donchian_upper": round(float(row["donchian_upper"]), price_precision) if row.get("donchian_upper") is not None else None,
                "stop_type": stop_type,
                "target_type": target_type,
                "stop_distance": round(float(risk), price_precision),
                "target_distance": round(float(reward), price_precision),
                "risk_reward_ratio": rr_float,
            }

            from datetime import UTC, datetime
            candidate = SignalCandidateRead(
                id=uuid4(),
                scan_run_id=scan_run_id,
                spec_version_id=spec_version_id,
                instrument_id=instrument_id,
                timeframe=timeframe,
                bar_open_ns=row["bar_open_ns"],
                bar_close_inclusive_ns=row["bar_close_inclusive_ns"],
                interval_end_exclusive_ns=row["interval_end_exclusive_ns"],
                matched_rules=matched_explanations,
                metrics=metrics_snapshot,
                suggested_entry=entry_dec,
                suggested_stop=stop_dec,
                suggested_target=target_dec,
                risk_reward_ratio=derived_rr,
                created_at=datetime.now(tz=UTC),
            )
            candidates.append(candidate)

        return candidates

    deduplicate_candidates = staticmethod(deduplicate_candidates)

    @staticmethod
    def _build_condition_expr(cond: RuleCondition) -> pl.Expr:
        """Construct a Polars boolean expression for a rule condition."""
        left_col = pl.col(cond.field)

        # Right operand can be another column name or a literal value
        if isinstance(cond.value, str):
            right_col = pl.col(cond.value)
        else:
            right_col = pl.lit(float(cond.value))

        is_cross_above = cond.operator in (
            RuleOperator.CROSS_ABOVE,
            getattr(RuleOperator, "CROSSES_ABOVE", "crosses_above"),
        )
        is_cross_below = cond.operator in (
            RuleOperator.CROSS_BELOW,
            getattr(RuleOperator, "CROSSES_BELOW", "crosses_below"),
        )

        if is_cross_above:
            shift_n = cond.lookback if cond.lookback >= 1 else 1
            prev_left = left_col.shift(shift_n)
            prev_right = right_col.shift(shift_n) if isinstance(cond.value, str) else right_col
            return ((prev_left <= prev_right) & (left_col > right_col)).fill_null(False)
        elif is_cross_below:
            shift_n = cond.lookback if cond.lookback >= 1 else 1
            prev_left = left_col.shift(shift_n)
            prev_right = right_col.shift(shift_n) if isinstance(cond.value, str) else right_col
            return ((prev_left >= prev_right) & (left_col < right_col)).fill_null(False)

        if cond.lookback > 0:
            left_col = left_col.shift(cond.lookback)
            if isinstance(cond.value, str):
                right_col = right_col.shift(cond.lookback)

        if cond.operator == RuleOperator.GT:
            return (left_col > right_col).fill_null(False)
        elif cond.operator == RuleOperator.GTE:
            return (left_col >= right_col).fill_null(False)
        elif cond.operator == RuleOperator.LT:
            return (left_col < right_col).fill_null(False)
        elif cond.operator == RuleOperator.LTE:
            return (left_col <= right_col).fill_null(False)
        elif cond.operator == RuleOperator.EQ:
            return (left_col == right_col).fill_null(False)

        return pl.lit(True)
