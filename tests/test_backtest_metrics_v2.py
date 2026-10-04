"""Backtest accuracy (remediation Phase 2): default profile, mark-to-market equity, daily grid, benchmark, fees."""

from cryptonite_backtester.runner import BacktestRunRequest


def test_default_execution_profile_is_conservative():
    """R2.1: a run that names no profile pays fees and slippage, like the paper engine."""
    from cryptonite_api.routes.backtests import CreateBacktestRequest

    run = BacktestRunRequest(run_id="r", strategy_slug="s", strategy_version_id="v", instrument_id="i",
                             bar_type="b", warmup_start_ns=0, evaluation_start_ns=1, evaluation_end_ns=2)
    api = CreateBacktestRequest(strategy_slug="s", warmup_start_ns=0, evaluation_start_ns=1, evaluation_end_ns=2)
    assert run.execution_profile == "conservative"
    assert api.execution_profile == "conservative"
