"""Leverage plan Stage 5: the perp paper gate before any testnet step (cryptonite_execution.perp_gate)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from cryptonite_execution.perp_gate import STOP_OVERRUN_R, PerpTrade, perp_gate

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def trade(i: int, pnl: str, planned: str | None = "100", grade: str | None = "A", day: float | None = None) -> PerpTrade:
    opened = T0 + timedelta(days=day if day is not None else i)
    return PerpTrade(f"p{i}", "BTCUSDT-PERP.BINANCE", opened, opened + timedelta(hours=6), D(pnl),
                     D(planned) if planned is not None else None, grade)


def checks(report) -> dict:
    return {c.name: c for c in report.checks}


def winning_record(n: int = 30) -> list[PerpTrade]:
    # 12 wins of +2 R, 18 losses of -1 R over 31 days: mean +0.2 R
    return [trade(i, "200" if i % 5 < 2 else "-100", day=i * 31 / (n - 1)) for i in range(n)]


def test_a_positive_record_with_no_failures_passes():
    report = perp_gate(winning_record(), liquidations=0, short_closes=[], now=T0 + timedelta(days=31))
    assert report.passed, [c for c in report.checks if not c.passed]
    assert checks(report)["mean_r"].actual == "0.200"


def test_too_few_trades_or_days_fail():
    few = winning_record()[:29]
    assert not checks(perp_gate(few, liquidations=0, short_closes=[], now=T0 + timedelta(days=31)))["closed_perp_trades"].passed
    short = [trade(i, "200" if i % 5 < 2 else "-100", day=i * 0.5) for i in range(30)]
    report = perp_gate(short, liquidations=0, short_closes=[], now=T0 + timedelta(days=15))
    assert not checks(report)["days_since_first_perp"].passed and not report.passed


def test_a_negative_expectancy_fails_even_with_working_stops():
    losing = [trade(i, "150" if i % 3 == 0 else "-100", day=i * 31 / 29) for i in range(30)]  # mean -0.17 R
    report = perp_gate(losing, liquidations=0, short_closes=[], now=T0 + timedelta(days=31))
    assert not checks(report)["mean_r"].passed and not report.passed


def test_one_liquidation_fails_the_gate():
    report = perp_gate(winning_record(), liquidations=1, short_closes=[], now=T0 + timedelta(days=31))
    assert not checks(report)["liquidations"].passed and not report.passed


def test_a_loss_beyond_the_overrun_threshold_is_a_stop_failure():
    record = winning_record()
    record[3] = trade(3, str(-(STOP_OVERRUN_R * 100 + 1)), day=3)  # -1.26 R: the stop did not hold
    report = perp_gate(record, liquidations=0, short_closes=[], now=T0 + timedelta(days=31))
    assert report.stop_failures == ("p3",) and not report.passed
    held = winning_record()
    held[3] = trade(3, "-120", day=3)  # -1.2 R: slippage inside the allowance, the stop held
    assert perp_gate(held, liquidations=0, short_closes=[], now=T0 + timedelta(days=31)).stop_failures == ()


def test_a_close_short_alert_is_a_stop_failure():
    report = perp_gate(winning_record(), liquidations=0, short_closes=["close-short:abc"], now=T0 + timedelta(days=31))
    assert report.stop_failures == ("close-short:abc",) and not report.passed


def test_a_trade_without_a_plan_cannot_be_measured_and_fails():
    record = winning_record()
    record[0] = trade(0, "200", planned=None, day=0)
    report = perp_gate(record, liquidations=0, short_closes=[], now=T0 + timedelta(days=31))
    assert not checks(report)["trades_without_plan"].passed and not report.passed


def test_the_interval_and_grade_order_are_informational():
    record = winning_record()
    for t_i in range(30):  # grade C wins, grade A loses: shown, never deciding
        record[t_i] = trade(t_i, record[t_i].realized_pnl.to_eng_string(), grade="C" if record[t_i].realized_pnl > 0 else "A",
                            day=t_i * 31 / 29)
    report = perp_gate(record, liquidations=0, short_closes=[], now=T0 + timedelta(days=31), spot_mean_return_pct=-0.4)
    c = checks(report)
    assert c["grades_ordered"].informational and not c["grades_ordered"].passed
    assert c["mean_r_interval"].informational and c["mean_r_interval"].interval is not None
    assert c["spot_mean_return_pct_same_window"].actual == "-0.40"
    assert report.passed


def test_no_trades_fails_cleanly():
    report = perp_gate([], liquidations=0, short_closes=[], now=T0)
    assert not report.passed and checks(report)["mean_r"].actual == "n/a"
