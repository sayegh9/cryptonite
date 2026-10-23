"""P1.1 SHORT preset coverage (ADR 0011 profile presets)."""

from __future__ import annotations

from uuid import uuid4

import polars as pl
import pytest
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.models import RuleOperator
from cryptonite_scanner.presets import BUILTIN_PRESETS, SWING_PRESETS
from cryptonite_schemas.profile import PRESETS

SHORT_SLUGS = {
    slug
    for (style, side), slugs in PRESETS.items()
    if side.value == "SHORT"
    for slug in slugs
}


def _frame_for(preset, *, downtrend: bool) -> pl.DataFrame:
    """Build two enriched bars where the spec's crossover fires in its trend direction."""
    last_close = 90.0 if downtrend else 110.0
    prev_close = last_close + 2 if downtrend else last_close - 2
    row0 = {
        "bar_open_ns": 1,
        "bar_close_inclusive_ns": 2,
        "interval_end_exclusive_ns": 3,
        "open": prev_close + (1 if downtrend else -1),
        "high": prev_close + 3,
        "low": prev_close - 3,
        "close": prev_close,
        "volume": 100.0,
        "quote_volume": 10000.0,
        "trades_count": 10,
        "rsi": 61.0 if downtrend else 39.0,
        "ema_20": 100.0 if downtrend else 105.0,
        "ema_50": 105.0 if downtrend else 100.0,
        "ema_200": 110.0 if downtrend else 90.0,
        "htf_close": 90.0 if downtrend else 110.0,
        "htf_ema_50": 100.0,
        "bb_lower": 95.0,
        "bb_upper": 105.0,
        "macd_hist": -1.0 if downtrend else 1.0,
        "atr": 2.0,
        "volume_ratio": 1.2,
        "donchian_lower": 82.0,
        "donchian_upper": 98.0,
    }
    row1 = dict(row0)
    row1.update(
        bar_open_ns=4,
        bar_close_inclusive_ns=5,
        interval_end_exclusive_ns=6,
        open=last_close + 1 if downtrend else last_close - 1,
        high=last_close + 3,
        low=last_close - 3,
        close=last_close,
        rsi=59.0 if downtrend else 41.0,
        macd_hist=1.0 if downtrend else -1.0,
        donchian_lower=last_close - 8,
        donchian_upper=last_close + 8,
    )

    # Make crossover conditions fire on the final row while preserving the
    # independent bearish or bullish trend context in the remaining conditions.
    for group in preset.rules:
        for condition in group.conditions:
            if condition.operator not in (RuleOperator.CROSS_ABOVE, RuleOperator.CROSS_BELOW):
                continue
            left, right = condition.field, condition.value
            right0 = row0[right] if isinstance(right, str) else float(right)
            right1 = row1[right] if isinstance(right, str) else float(right)
            if condition.operator == RuleOperator.CROSS_BELOW:
                row0[left], row1[left] = right0 + 1, right1 - 1
            else:
                row0[left], row1[left] = right0 - 1, right1 + 1

    if not downtrend:
        for group in preset.rules:
            if group.category != "trend":
                continue
            for condition in group.conditions:
                if condition.operator == RuleOperator.LT and isinstance(condition.value, str):
                    row1[condition.value] = row1[condition.field] - 1

    if downtrend and any(
        condition.field == "close" and condition.value == "open" and condition.operator == RuleOperator.LT
        for group in preset.rules
        for condition in group.conditions
    ):
        row1["open"] = row1["close"] + 1

    minimum_volume = max(
        (
            float(condition.value)
            for group in preset.rules
            for condition in group.conditions
            if condition.field == "volume_ratio" and condition.operator == RuleOperator.GTE
        ),
        default=0.0,
    )
    row1["volume_ratio"] = max(row1["volume_ratio"], minimum_volume)

    return pl.DataFrame([row0, row1])


def _evaluate(preset, *, downtrend: bool):
    """Evaluate a preset using deterministic enriched bars."""
    return ScannerEngine.evaluate(
        df=_frame_for(preset, downtrend=downtrend),
        rule_groups=preset.rules,
        risk_config=preset.risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="1h",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        price_precision=2,
    )


def test_all_profile_preset_slugs_resolve():
    """Every P0 contract slug maps to an executable scanner spec."""
    from cryptonite_scanner.presets import preset_by_slug

    expected = {slug for slugs in PRESETS.values() for slug in slugs}
    assert len(expected) == 12
    assert {preset_by_slug(slug).slug for slug in expected} == expected
    with pytest.raises(KeyError):
        preset_by_slug("unknown-profile-preset")


@pytest.mark.parametrize("slug", sorted(SHORT_SLUGS))
def test_short_preset_matches_downtrend_and_has_short_levels(slug: str):
    """SHORT specs emit candidates only with stops above and targets below entry."""
    from cryptonite_scanner.presets import preset_by_slug

    preset = preset_by_slug(slug)
    assert preset.risk.direction == "SHORT"
    candidates = _evaluate(preset, downtrend=True)
    assert candidates, slug
    candidate = candidates[-1]
    assert candidate.suggested_stop > candidate.suggested_entry
    assert candidate.suggested_target < candidate.suggested_entry


@pytest.mark.parametrize("slug", sorted(SHORT_SLUGS))
def test_short_preset_rejects_uptrend(slug: str):
    """SHORT specs never emit candidates against an uptrend."""
    from cryptonite_scanner.presets import preset_by_slug

    assert _evaluate(preset_by_slug(slug), downtrend=False) == []


def test_existing_long_preset_definitions_are_unchanged():
    """The established long specs keep their slugs, timeframes, rules and risk settings."""
    from cryptonite_scanner.presets import preset_by_slug

    long_presets = [*BUILTIN_PRESETS, *SWING_PRESETS]
    assert len(long_presets) == 8
    for preset in long_presets:
        assert preset_by_slug(preset.slug).model_dump(mode="json") == preset.model_dump(mode="json")

