"""Vectorized Polars Scanner Rule Evaluation Engine.

Evaluates multi-factor rule groups over resampled OHLCV DataFrames and returns
strictly analytical SignalCandidate records. Completely isolated from execution.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import polars as pl
from cryptonite_scanner.indicators import (
    calc_atr,
    calc_bollinger_bands,
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
)


class ScannerEngine:
    """Vectorized rule evaluator over OHLCV DataFrames."""

    @staticmethod
    def enrich_indicators(df: pl.DataFrame) -> pl.DataFrame:
        """Enrich OHLCV DataFrame with standard technical indicator columns."""
        if df.is_empty():
            return df

        bb_lower, bb_mid, bb_upper = calc_bollinger_bands("close", 20, 2.0)
        macd, macd_sig, macd_hist = calc_macd("close", 12, 26, 9)

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
        price_precision: int = 2,
    ) -> list[SignalCandidateRead]:
        """Evaluate rule groups against the DataFrame and produce SignalCandidate records."""
        if df.is_empty():
            return []

        enriched = cls.enrich_indicators(df)

        # Drop warm-up rows where indicators are null
        valid_df = enriched.filter(
            pl.col("rsi").is_not_null()
            & pl.col("ema_200").is_not_null()
            & pl.col("atr").is_not_null()
        )

        if valid_df.is_empty():
            return []

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

        for row in rows:
            matched_explanations: list[str] = []
            for group, cond_details in rule_eval_details:
                for cond, _ in cond_details:
                    actual_val = row.get(cond.field)
                    target_val = row.get(cond.value) if isinstance(cond.value, str) else cond.value
                    if actual_val is not None:
                        matched_explanations.append(
                            f"[{group.category.upper()}] {cond.field} ({float(actual_val):.2f}) {cond.operator} {cond.value}"
                        )

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
            }

            # Calculate quantized suggested risk levels
            entry_dec = round(Decimal(str(row["close"])), price_precision)
            atr_val = Decimal(str(round(row.get("atr", 0.0), price_precision)))
            stop_dec = round(entry_dec - (atr_val * Decimal(str(risk_config.stop_loss_atr_mult))), price_precision)
            target_dec = round(entry_dec + (atr_val * Decimal(str(risk_config.take_profit_atr_mult))), price_precision)

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
                created_at=datetime.now(tz=UTC),
            )
            candidates.append(candidate)

        return candidates

    @staticmethod
    def _build_condition_expr(cond: RuleCondition) -> pl.Expr:
        """Construct a Polars boolean expression for a rule condition."""
        left_col = pl.col(cond.field)

        # Right operand can be another column name or a literal value
        if isinstance(cond.value, str):
            right_col = pl.col(cond.value)
        else:
            right_col = pl.lit(float(cond.value))

        if cond.operator == RuleOperator.GT:
            return left_col > right_col
        elif cond.operator == RuleOperator.GTE:
            return left_col >= right_col
        elif cond.operator == RuleOperator.LT:
            return left_col < right_col
        elif cond.operator == RuleOperator.LTE:
            return left_col <= right_col
        elif cond.operator == RuleOperator.EQ:
            return left_col == right_col
        elif cond.operator == RuleOperator.CROSS_ABOVE:
            prev_left = left_col.shift(1)
            prev_right = right_col.shift(1) if isinstance(cond.value, str) else right_col
            return (prev_left <= prev_right) & (left_col > right_col)
        elif cond.operator == RuleOperator.CROSS_BELOW:
            prev_left = left_col.shift(1)
            prev_right = right_col.shift(1) if isinstance(cond.value, str) else right_col
            return (prev_left >= prev_right) & (left_col < right_col)

        return pl.lit(True)
