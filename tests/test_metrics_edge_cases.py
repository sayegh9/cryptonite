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
