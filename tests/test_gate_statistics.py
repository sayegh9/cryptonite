"""Statistically honest gates (remediation R3.3, R4.1-R4.3, R4.6, R4.7): decision scoring with a neutral
band, Wilson and stationary-bootstrap intervals, informational gate checks that never change ``passed``."""

import random
from decimal import Decimal as D

import pytest
from cryptonite_execution import calibration, stats
from cryptonite_execution.autonomy import GateCheck, GateMetrics, GateReport, evaluate_gate
from cryptonite_execution.autonomy_metrics import report_view, spot_trade_returns
from cryptonite_schemas.mandate import AutonomyLevel

L1, L2, L3 = AutonomyLevel.L1, AutonomyLevel.L2, AutonomyLevel.L3
L2_READY = GateMetrics(days_at_level=31, closed_trades=30, max_drawdown_pct=D("8"), breaches=0, sortino=1.4, benchmark_sortino=0.9)
L3_READY = GateMetrics(days_at_level=61, calibration_error_pp=D("6"), veto_rate_pct=D("4"), breaches=0)


# --- R3.3: score_decision ------------------------------------------------------------------------

@pytest.mark.parametrize("rec, ret, expected", [
    ("BUY", 0.01, "WIN"), ("BUY", -0.01, "LOSS"), ("BUY", 0.0005, "NEUTRAL"), ("BUY", -0.002, "NEUTRAL"),
    ("SELL", -0.01, "WIN"), ("SELL", 0.01, "LOSS"), ("SELL", 0.0005, "NEUTRAL"), ("SELL", 0.002, "NEUTRAL"),
    ("HOLD", -0.01, "WIN"), ("HOLD", 0.0005, "WIN"), ("HOLD", 0.002, "WIN"), ("HOLD", 0.01, "LOSS"),
    ("buy", 0.01, "WIN"),
])
def test_score_decision_table(rec, ret, expected):
    assert calibration.score_decision(rec, ret) == expected


def test_band_is_a_parameter():
    assert calibration.score_decision("BUY", 0.004) == "WIN"
    assert calibration.score_decision("BUY", 0.004, band=0.005) == "NEUTRAL"


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, *_):
        return self.rows


async def test_calibration_leaves_neutral_points_out_of_the_bins():
    rows = [{"conviction_score": 0.8, "recommendation": "BUY", "return_pct": 0.0001},  # NEUTRAL
            {"conviction_score": 0.8, "recommendation": "BUY", "return_pct": 0.02},  # WIN
            {"conviction_score": 0.7, "recommendation": "SELL", "return_pct": 0.02},  # LOSS
            {"conviction_score": 0.6, "recommendation": "HOLD", "return_pct": 0.001}]  # WIN (no rise beyond the band)
    assert await calibration.verdict_points(_Conn(rows)) == [(0.8, True), (0.7, False), (0.6, True)]


def test_reliability_reports_an_ece_interval_and_equal_count_bins():
    points = [(0.9, i < 10) for i in range(20)] + [(0.3, i < 3) for i in range(10)]
    rel = calibration.reliability(points)
    lo, hi = rel["ece_interval_pp"]
    assert lo <= rel["ece_pp"] <= hi
    assert [b["n"] for b in rel["equal_count_bins"]] == [6, 6, 6, 6, 6]
    assert calibration.reliability(points)["ece_interval_pp"] == [lo, hi]  # seeded
    assert calibration.reliability(points[:calibration.MIN_SAMPLES - 1])["ece_interval_pp"] is None


# --- R4.1: stats -----------------------------------------------------------------------------------

def test_wilson_interval():
    lo, hi = stats.wilson_interval(15, 30)
    assert lo < 0.5 < hi and hi - lo < 0.35
    assert stats.wilson_interval(0, 10)[0] == 0.0 and stats.wilson_interval(10, 10)[1] == 1.0
    assert stats.wilson_interval(0, 0) is None


def test_fair_coin_trades_fail_the_expectancy_bound_in_most_seeds():
    """30 trades of +-1% with a fair coin: the 90% lower bound is above 0 in at most ~5% of samples."""
    failed = 0
    for seed in range(100):
        rng = random.Random(seed)
        trades = [0.01 if rng.random() < 0.5 else -0.01 for _ in range(30)]
        lo, _ = stats.mean_interval(trades, n_boot=500, seed=seed)
        failed += lo <= 0
    assert failed >= 90


def test_a_real_edge_passes_the_expectancy_bound():
    rng = random.Random(1)
    trades = [0.005 + rng.gauss(0, 0.01) for _ in range(200)]  # +0.5% per trade after fees
    lo, hi = stats.mean_interval(trades)
    assert 0 < lo < 0.005 < hi


def test_bootstrap_is_deterministic_with_a_seed():
    rng = random.Random(3)
    r = [rng.gauss(0.001, 0.02) for _ in range(60)]
    b = [rng.gauss(0.0, 0.02) for _ in range(60)]
    assert stats.mean_interval(r, seed=11) == stats.mean_interval(r, seed=11)
    assert stats.mean_interval(r, seed=11) != stats.mean_interval(r, seed=12)
    assert stats.sortino_diff_interval(r, b) == stats.sortino_diff_interval(r, b)
    assert stats.mean_interval([0.1]) is None and stats.sortino_diff_interval(r, b[:-1]) is None


# --- R4.7 / R4.2 / R4.6: informational checks --------------------------------------------------

def test_informational_checks_never_change_passed():
    req = GateCheck("a", "x", "y", True)
    info_fail = GateCheck("b", "x", "y", False, informational=True)
    assert GateReport(L1, L2, (req, info_fail)).passed
    assert not GateReport(L1, L2, (GateCheck("a", "x", "y", False), GateCheck("b", "x", "y", True, informational=True))).passed
    assert not GateReport(L1, L2, (GateCheck("b", "x", "y", True, informational=True),)).passed  # no required check


def test_l2_gate_reports_interval_checks_without_gating_on_them():
    r = evaluate_gate(L1, L2_READY)  # no trade returns measured: interval checks fail, gate still passes
    info = {c.name: c for c in r.checks if c.informational}
    assert set(info) == {"expectancy_lower_bound", "sortino_diff_lower_bound"}
    assert r.passed and not any(c.passed for c in info.values())

    rng = random.Random(5)
    losing = GateMetrics(**{**L2_READY.__dict__, "trade_returns": tuple(-0.004 + rng.gauss(0, 0.01) for _ in range(40))})
    r = evaluate_gate(L1, losing)
    exp = next(c for c in r.checks if c.name == "expectancy_lower_bound")
    assert r.passed and not exp.passed and exp.interval and float(exp.interval[0].rstrip("%")) < 0

    d = r.as_dict()
    by_name = {c["name"]: c for c in d["checks"]}
    assert by_name["expectancy_lower_bound"]["informational"] is True
    assert len(by_name["expectancy_lower_bound"]["interval"]) == 2
    assert all(isinstance(x, str) for x in by_name["expectancy_lower_bound"]["interval"])
    assert by_name["days_at_level"]["informational"] is False and "interval" not in by_name["days_at_level"]


def test_l3_gate_reports_the_calibration_interval_without_gating_on_it():
    wide = GateMetrics(**{**L3_READY.__dict__, "calibration_interval_pp": (D("2"), D("14"))})
    r = evaluate_gate(L2, wide)
    ub = next(c for c in r.checks if c.name == "calibration_error_upper_bound")
    assert r.passed and ub.informational and not ub.passed and ub.interval == ("2", "14")


def test_honest_paper_not_measured_is_shown_but_not_failed():
    r = evaluate_gate(L1, GateMetrics(**{**L2_READY.__dict__, "honest_paper": None, "honest_paper_detail": "no fills yet"}))
    hp = next(c for c in r.checks if c.name == "honest_paper")
    assert r.passed and hp.passed and hp.actual == "not measured (no fills yet)"
    assert not evaluate_gate(L1, GateMetrics(**{**L2_READY.__dict__, "honest_paper": False})).passed


def test_spot_trade_returns_after_fees():
    fills = [
        {"instrument_id": "BTC", "side": "BUY", "price": 100, "quantity": 1, "commission": 0.1},
        {"instrument_id": "BTC", "side": "SELL", "price": 110, "quantity": 1, "commission": 0.11},
        {"instrument_id": "ETH", "side": "SELL", "price": 10, "quantity": 1, "commission": 0.01},  # no entry in window
    ]
    assert spot_trade_returns(fills) == [pytest.approx((110 - 0.11) / 100.1 - 1)]


def test_report_view_shows_win_rate_with_wilson_interval():
    m = GateMetrics(**{**L2_READY.__dict__, "trade_returns": (0.01, -0.01, 0.02, 0.01)})
    v = report_view(evaluate_gate(L1, m), m)
    assert v["metrics"]["win_rate"] == 0.75 and len(v["metrics"]["win_rate_interval"]) == 2
    assert v["passed"] is True
