"""Test suite validating that indicator warmup creates zero orders or positions prior to evaluation_start_ns."""

from decimal import Decimal
from pathlib import Path
import pyarrow.parquet as pq
import pytest

from cryptonite_backtester.runner import BacktestRunner, BacktestRunRequest
from cryptonite_schemas.identifiers import (
    to_nautilus_bar_type,
    to_nautilus_instrument_id,
    to_spot_instrument_id,
)
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from nautilus_trader.model import (
    Bar,
    Currency,
    CurrencyPair,
    Price,
    Quantity,
)


def _setup_warmup_test_catalog(catalog_dir: Path) -> tuple[str, str, int, int, int]:
    mgr = NautilusCatalogManager(catalog_dir)
    inst_id = to_nautilus_instrument_id(to_spot_instrument_id("BTCUSDT"))
    pair = CurrencyPair(
        instrument_id=inst_id,
        raw_symbol=inst_id.symbol,
        base_currency=Currency.from_str("BTC"),
        quote_currency=Currency.from_str("USDT"),
        price_precision=2,
        size_precision=5,
        price_increment=Price(Decimal("0.01"), 2),
        size_increment=Quantity(Decimal("0.00001"), 5),
        ts_event=1725700000000000000,
        ts_init=1725700000000000000,
    )
    mgr.write_instruments([pair])

    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    step_ns = 60_000_000_000  # 1 minute
    warmup_start_ns = 1725700000000000000

    # 40 warmup bars followed by 60 evaluation bars (total 100 bars)
    warmup_bar_count = 40
    eval_bar_count = 60
    total_bars = warmup_bar_count + eval_bar_count

    eval_start_ns = warmup_start_ns + (warmup_bar_count * step_ns)
    eval_end_ns = warmup_start_ns + ((total_bars - 1) * step_ns)

    bars = []
    # Generate oscillating prices that will cause multiple EMA crossovers during both
    # the warmup window and the evaluation window
    px = 50000.0
    for i in range(total_bars):
        ts = warmup_start_ns + i * step_ns
        # Create an alternating wave with period 8 bars
        delta = 50.0 if (i % 8) < 4 else -50.0
        c = px + delta
        h = max(px, c) + 20.0
        l = min(px, c) - 20.0
        o = px
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price(Decimal(str(o)), 2),
                high=Price(Decimal(str(h)), 2),
                low=Price(Decimal(str(l)), 2),
                close=Price(Decimal(str(c)), 2),
                volume=Quantity(Decimal("1.0"), 5),
                ts_event=ts,
                ts_init=ts,
            )
        )
        px = c

    mgr.write_bars(bars)
    return str(inst_id), str(bar_type), warmup_start_ns, eval_start_ns, eval_end_ns


def test_warmup_no_execution_contract(tmp_path: Path):
    """Verify indicators warm up during [warmup_start_ns, evaluation_start_ns) but zero trades/orders execute."""
    cat_dir = tmp_path / "nautilus_catalog"
    art_dir = tmp_path / "backtest_artifacts"
    inst_id, bar_type, warmup_start, eval_start, eval_end = _setup_warmup_test_catalog(cat_dir)

    runner = BacktestRunner(cat_dir, art_dir)

    req = BacktestRunRequest(
        run_id="run-warmup-validation",
        strategy_slug="ema-cross",
        strategy_version_id="00000000-0000-0000-0000-000000000001",
        instrument_id=inst_id,
        bar_type=bar_type,
        timeframe="1m",
        parameters={"fast_period": 5, "slow_period": 10, "trade_size": 0.01},
        warmup_start_ns=warmup_start,
        evaluation_start_ns=eval_start,
        evaluation_end_ns=eval_end,
        execution_profile="optimistic",
        initial_balance=10000.0,
    )

    result = runner.run(req)

    # Verify execution occurred in evaluation window
    assert result["total_trades"] > 0, "Expected trades to occur within evaluation window"

    # Verify persisted artifacts strictly observe evaluation_start_ns boundary
    run_art_dir = art_dir / "run-warmup-validation"
    assert (run_art_dir / "orders.parquet").exists()
    assert (run_art_dir / "trades.parquet").exists()
    assert (run_art_dir / "equity_curve.parquet").exists()

    orders_tbl = pq.read_table(run_art_dir / "orders.parquet")
    orders_df = orders_tbl.to_pandas()
    assert len(orders_df) > 0

    # Critical invariant: EVERY order must be initialized at or after evaluation_start_ns
    for _, order in orders_df.iterrows():
        assert order["ts_init_ns"] >= eval_start, (
            f"Order {order['client_order_id']} was submitted at {order['ts_init_ns']} "
            f"which is before evaluation_start_ns {eval_start}"
        )

    trades_tbl = pq.read_table(run_art_dir / "trades.parquet")
    trades_df = trades_tbl.to_pandas()
    assert len(trades_df) > 0

    # Critical invariant: EVERY position/trade must be opened at or after evaluation_start_ns
    for _, trade in trades_df.iterrows():
        assert trade["ts_opened_ns"] >= eval_start, (
            f"Trade position {trade['position_id']} was opened at {trade['ts_opened_ns']} "
            f"which is before evaluation_start_ns {eval_start}"
        )

    equity_tbl = pq.read_table(run_art_dir / "equity_curve.parquet")
    equity_df = equity_tbl.to_pandas()
    assert len(equity_df) >= 2
    # Critical invariant: Equity curve starts at evaluation_start_ns
    assert equity_df.iloc[0]["ts_event_ns"] == eval_start
    assert equity_df.iloc[0]["equity"] == 10000.0


def test_warmup_rejection_when_violated(tmp_path: Path):
    """Verify that if evaluation_start_ns <= warmup_start_ns, the runner strictly raises ValueError."""
    cat_dir = tmp_path / "nautilus_catalog"
    art_dir = tmp_path / "backtest_artifacts"
    inst_id, bar_type, warmup_start, eval_start, eval_end = _setup_warmup_test_catalog(cat_dir)

    runner = BacktestRunner(cat_dir, art_dir)

    # Invalid: warmup_start_ns >= evaluation_start_ns
    req_invalid = BacktestRunRequest(
        run_id="run-invalid-bounds",
        strategy_slug="ema-cross",
        strategy_version_id="00000000-0000-0000-0000-000000000001",
        instrument_id=inst_id,
        bar_type=bar_type,
        timeframe="1m",
        parameters={"fast_period": 5, "slow_period": 10, "trade_size": 0.01},
        warmup_start_ns=eval_start,
        evaluation_start_ns=eval_start,
        evaluation_end_ns=eval_end,
        execution_profile="optimistic",
        initial_balance=10000.0,
    )

    with pytest.raises(ValueError, match="warmup_start_ns .* must be strictly less than evaluation_start_ns"):
        runner.run(req_invalid)
