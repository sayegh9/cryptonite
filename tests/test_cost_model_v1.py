import pytest
from cryptonite_scanner.costs import (
    cost_in_r,
    cost_model_name,
    default_market,
    round_trip_cost_fraction,
)


def test_round_trip_fractions_per_market():
    assert round_trip_cost_fraction("spot") == pytest.approx(0.003)
    assert round_trip_cost_fraction("margin", 10, borrow_rate_per_hour=0.00001) == pytest.approx(0.0031)
    # perp: 0.2% plus default funding 0.01% per 8 h against the side
    assert round_trip_cost_fraction("perp", 24) == pytest.approx(0.002 + 0.0003)
    assert round_trip_cost_fraction("perp", 8, funding_rate_per_8h=-0.0002) == pytest.approx(0.0018)


def test_cost_in_r_uses_entry_to_stop_distance():
    # 0.56% stop: a 0.2% round trip costs about 0.36R
    assert cost_in_r(100.0, 99.44, 0.002) == pytest.approx(0.002 * 100 / 0.56)
    assert cost_in_r(100.0, 100.0, 0.002) is None


def test_default_market_and_name():
    assert default_market("LONG") == "spot" and default_market("SHORT") == "perp"
    assert cost_model_name("perp") == "perp-v1"
