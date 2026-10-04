"""Edge-case tests for financial metrics calculation: no-trades, all-breakeven, and division-by-zero safeguards."""

import pytest
from cryptonite_backtester.metrics import calculate_performance_metrics


def test_no_trades_metrics():
    """Verify performance metrics when zero trades occur in the evaluation window."""
    equity_curve = [
        (1725700000000000000, 10000.0),
        (1725703600000000000, 10000.0),
    ]
    trades = []
    metrics = calculate_performance_metrics(equity_curve, trades, initial_balance=10000.0)

    assert metrics["total_trades"] == 0
    assert metrics["winning_trades"] == 0
    assert metrics["losing_trades"] == 0
    assert metrics["win_rate"] == 0.0
    assert metrics["avg_trade_pnl"] == 0.0
    # Profit factor must be None (unavailable/NULL), NOT infinity or NaN
    assert metrics["profit_factor"] is None
    assert metrics["total_return"] == 0.0
    assert metrics["max_drawdown"] == 0.0


def test_all_breakeven_trades_metrics():
    """Verify profit factor is unavailable (None) when all trades close at exact breakeven."""
    equity_curve = [
        (1725700000000000000, 10000.0),
        (1725703600000000000, 10000.0),
    ]
    trades = [
        {"position_id": "p1", "pnl": 0.0, "realized_return": 0.0},
        {"position_id": "p2", "pnl": 0.0, "realized_return": 0.0},
        {"position_id": "p3", "pnl": 0.0, "realized_return": 0.0},
    ]
    metrics = calculate_performance_metrics(equity_curve, trades, initial_balance=10000.0)

    assert metrics["total_trades"] == 3
    assert metrics["winning_trades"] == 0
    assert metrics["losing_trades"] == 0
    assert metrics["win_rate"] == 0.0
    assert metrics["avg_trade_pnl"] == 0.0
    # Profit factor must be None (unavailable/NULL) since gross loss is zero
    assert metrics["profit_factor"] is None


def test_only_winning_trades_metrics():
    """Verify profit factor is None (preventing float('inf')) when there are only winning trades."""
    equity_curve = [
        (1725700000000000000, 10000.0),
        (1725703600000000000, 10500.0),
    ]
    trades = [
        {"position_id": "p1", "pnl": 250.0, "realized_return": 0.025},
        {"position_id": "p2", "pnl": 250.0, "realized_return": 0.025},
    ]
    metrics = calculate_performance_metrics(equity_curve, trades, initial_balance=10000.0)

    assert metrics["total_trades"] == 2
    assert metrics["winning_trades"] == 2
    assert metrics["losing_trades"] == 0
    assert metrics["win_rate"] == 1.0
    assert metrics["avg_trade_pnl"] == 250.0
    # Zero loss trades -> profit factor must be None
    assert metrics["profit_factor"] is None
    assert metrics["total_return"] == 0.05


def test_equity_values_the_base_asset_instead_of_adding_it():
    """Run e253312a (2026-09): the account report has a USDT and a BTC row per update; every row's total
    was taken as equity, so the curve ended on the BTC row (0.0) and the run showed -100%."""
    from cryptonite_backtester.metrics import account_equity_series

    t0, t1, t2 = 1_000, 2_000, 3_000
    balances = [
        (t0, "USDT", 10_000.0), (t0, "BTC", 0.0),
        (t1, "USDT", 9_499.55), (t1, "BTC", 0.01),  # bought 0.01 BTC at 50,000 (+0.45 fees)
        (t2, "USDT", 9_999.55), (t2, "BTC", 0.0),  # sold it at 50,000
    ]
    fills = [(t1, 50_000.0), (t2, 50_000.0)]
    series = account_equity_series(balances, fills, quote_currency="USDT", start_ns=0)
    assert series == [(t0, 10_000.0), (t1, 9_999.55), (t2, 9_999.55)]
    m = calculate_performance_metrics(series, [{"pnl": -0.45}], initial_balance=10_000.0)
    assert m["total_return"] == pytest.approx(-0.000045)
    assert m["max_drawdown"] < 0.001  # was 1.0 (100%): the BTC row read as an equity of 0

    # rows before the evaluation start set the holdings but are not points of the curve
    assert account_equity_series(balances, fills, quote_currency="USDT", start_ns=t1) == [(t1, 9_999.55), (t2, 9_999.55)]
