"""Regression tests for the ATR fallback's minimum reward-to-risk ratio."""

from decimal import Decimal

import pytest
from cryptonite_scanner.levels import derive_levels
from cryptonite_scanner.models import RiskConfig
from cryptonite_schemas.reward_risk import meets_min_rr


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_fallback_target_keeps_intended_minimum_after_price_rounding(direction):
    """A 3 ATR target and 2 ATR stop must survive quantization on both sides."""
    row = {"close": 2352.76, "atr": 10.034, "interval_end_exclusive_ns": 1}
    result = derive_levels(row, RiskConfig(), direction, 2)
    assert result is not None
    entry, stop, target, stop_type, target_type, _, ratio = result
    assert (stop_type, target_type) == ("atr_fallback", "atr_fallback")
    assert meets_min_rr(ratio)
    assert abs(target - entry) >= Decimal("1.5") * abs(entry - stop)


def test_fallback_does_not_promote_a_genuinely_subminimum_ratio():
    """Rounding must not change a deliberately smaller target into a qualifying one."""
    row = {"close": 100.0, "atr": 1.0, "interval_end_exclusive_ns": 1}
    config = RiskConfig(fallback_target_atr_mult=2.8)
    result = derive_levels(row, config, "LONG", 2)
    assert result is not None
    assert not meets_min_rr(result[-1])
