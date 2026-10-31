"""Pure barrier walk for candidate stop/target labels."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "debate_worker"))
from candidate_outcomes import barrier_outcome  # noqa: E402


def bar(h, lo, c):
    return {"high": h, "low": lo, "close": c}


def test_target_first_long():
    r = barrier_outcome([bar(101, 99.5, 100.5), bar(103, 100, 102)], None, 100, 99, 103)
    assert r["outcome"] == "TARGET" and r["direction"] == "LONG" and r["bars_to_exit"] == 2
    assert r["realized_r"] == pytest.approx(3.0) and r["mfe_r"] == pytest.approx(3.0)
    assert r["mae_r"] == pytest.approx(-0.5)


def test_stop_first_short():
    r = barrier_outcome([bar(100.5, 99.5, 100), bar(101, 99, 100.8)], None, 100, 101, 98)
    assert r["outcome"] == "STOP" and r["direction"] == "SHORT" and r["bars_to_exit"] == 2
    assert r["realized_r"] == pytest.approx(-1.0)


def test_same_bar_counts_as_stop():
    r = barrier_outcome([bar(104, 98, 100)], None, 100, 99, 103)
    assert r["outcome"] == "STOP" and r["realized_r"] == pytest.approx(-1.0)


def test_expired_long_and_short():
    bars = [bar(100.5, 99.8, 100.4), bar(100.6, 99.7, 99.5)]
    long = barrier_outcome(bars, None, 100, 99, 103)
    assert long["outcome"] == "EXPIRED" and long["realized_r"] == pytest.approx(-0.5)
    assert long["mfe_r"] == pytest.approx(0.6) and long["mae_r"] == pytest.approx(-0.3)
    short = barrier_outcome(bars, None, 100, 101, 97)
    assert short["outcome"] == "EXPIRED" and short["realized_r"] == pytest.approx(0.5)
    assert short["mfe_r"] == pytest.approx(0.3) and short["mae_r"] == pytest.approx(-0.6)


@pytest.mark.parametrize("entry,stop,target", [(100, 100, 103), (None, 99, 103), (100, None, 103), (100, 99, None)])
def test_invalid_levels(entry, stop, target):
    r = barrier_outcome([bar(101, 99, 100)], None, entry, stop, target)
    assert r["outcome"] == "INVALID" and r["direction"] is None and r["realized_r"] is None
