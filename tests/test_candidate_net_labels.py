"""Profile-exit simulation and net-of-cost labels (AI Phase 2, Track A1)."""

import sys
from pathlib import Path

import pytest
from cryptonite_scanner.costs import cost_in_r, round_trip_cost_fraction

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "debate_worker"))
from candidate_outcomes import net_labels, net_r, profile_exit_outcome  # noqa: E402


def bar(o, h, lo, c):
    return {"open": o, "high": h, "low": lo, "close": c}


def test_trail_activates_after_three_percent_and_exits_on_trail():
    bars = [bar(100, 104, 99.5, 103), bar(103, 103.5, 101.5, 102)]
    res = profile_exit_outcome(bars, "LONG", 100.0)
    assert res["outcome"] == "TRAIL"
    assert res["exit_price"] == pytest.approx(104 * 0.98)
    assert res["bars_to_exit"] == 2


def test_trail_not_active_below_three_percent():
    bars = [bar(100, 102.9, 99.5, 102), bar(102, 102.5, 100.5, 101)]
    assert profile_exit_outcome(bars, "LONG", 100.0)["outcome"] == "EXPIRED"


def test_short_trail_mirrors_long():
    bars = [bar(100, 100.5, 96, 97), bar(97, 98.0, 96.5, 97.5)]
    res = profile_exit_outcome(bars, "SHORT", 100.0)
    assert res["outcome"] == "TRAIL"
    assert res["exit_price"] == pytest.approx(96 * 1.02)


def test_max_hold_expires_at_last_close_even_if_a_later_bar_stops():
    bars = [bar(100, 101, 99, 100.5), bar(100.5, 101, 99, 99.5), bar(99.5, 99.5, 90, 91)]
    res = profile_exit_outcome(bars, "LONG", 100.0, max_bars=2)
    assert res == {"outcome": "EXPIRED", "exit_price": 99.5, "bars_to_exit": 2}


def test_stop_checked_before_trail_on_the_same_bar():
    bars = [bar(100, 110, 94, 105)]
    res = profile_exit_outcome(bars, "LONG", 100.0)
    assert res["outcome"] == "STOP"
    assert res["exit_price"] == pytest.approx(95.0)


def test_invalid_and_empty():
    assert profile_exit_outcome([], "LONG", 100.0)["outcome"] == "NO_DATA"
    assert profile_exit_outcome([bar(1, 1, 1, 1)], "LONG", None)["outcome"] == "INVALID"


def test_cost_to_r_conversion_long_spot():
    # Fill at the next open 100, stop 98 (R = 2), target 104 hit on the first 1m bar.
    out = net_labels([bar(100, 104.5, 99.5, 104)], "LONG", 98.0, 104.0, bar_hours=1 / 60)
    assert out["entry_next_open"] == 100.0
    assert out["cost_model"] == "spot-v1"
    # spot round trip = 2 × (0.10% fee + 0.05% slippage) = 0.3% of 100 = 0.3 = 0.15 R.
    assert out["net_r_preset"] == pytest.approx(2.0 - 0.15)
    assert out["preset_bars"] == 1


def test_cost_to_r_conversion_short_perp_carries_funding():
    entry, stop = 100.0, 102.0
    assert net_r("SHORT", entry, stop, 96.0, 8.0) == pytest.approx(
        2.0 - cost_in_r(entry, stop, round_trip_cost_fraction("perp", 8.0)))
    assert net_r("SHORT", entry, stop, 96.0, 8.0) == pytest.approx(2.0 - (0.002 + 0.0001) * 100 / 2)


def test_same_bar_stop_and_target_counts_as_stop_net():
    out = net_labels([bar(100, 105, 97, 100)], "LONG", 98.0, 104.0, bar_hours=1 / 60)
    assert out["net_r_preset"] == pytest.approx(-1.0 - 0.15)


def test_fill_beyond_stop_leaves_preset_net_null():
    out = net_labels([bar(97, 98, 96, 97)], "LONG", 98.0, 104.0, bar_hours=1 / 60)
    assert out["entry_next_open"] == 97.0
    assert out["net_r_preset"] is None and out["net_r_profile"] is None
    assert out["cost_model"] == "spot-v1"


def test_profile_net_uses_preset_r_and_costs():
    # 5% hard stop from a 100 fill with a preset stop at 98 (R = 2): -5 / 2 = -2.5 R gross.
    out = net_labels([bar(100, 100.5, 94, 95)], "LONG", 98.0, 110.0, bar_hours=1 / 60)
    assert out["net_r_profile"] == pytest.approx(-2.5 - 0.15)
    assert out["profile_bars"] == 1


def test_empty_bars_give_empty_labels():
    out = net_labels([], "LONG", 98.0, 104.0, bar_hours=1 / 60)
    assert out["cost_model"] is None and out["entry_next_open"] is None
