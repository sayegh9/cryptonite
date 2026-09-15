"""Golden test verifying identical input catalog + parameters + profile yields bitwise identical results.

Validates:
1. Deterministic run_config_hash across identical run requests.
2. Identical performance metrics and downsampled equity curves.
3. Bitwise/tabular equivalence of persisted Parquet artifacts (equity_curve, trades, orders).
4. Identical input catalog file checksum manifests.
"""

from decimal import Decimal
from pathlib import Path
import pyarrow.parquet as pq

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


def _setup_catalog_fixture(catalog_dir: Path) -> tuple[str, str, int, int, int]:
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
    step_ns = 60_000_000_000
    base_ts = 1725700000000000000

    bars = []
    px = 50000.0
    for i in range(80):
        ts = base_ts + i * step_ns
        o = px
        h = px + 60.0
        l = px - 60.0
        c = px + (30.0 if (i // 5) % 2 == 0 else -30.0)
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price(Decimal(str(o)), 2),
                high=Price(Decimal(str(h)), 2),
                low=Price(Decimal(str(l)), 2),
                close=Price(Decimal(str(c)), 2),
                volume=Quantity(Decimal("1.5"), 5),
                ts_event=ts,
                ts_init=ts,
            )
        )
        px = c
    mgr.write_bars(bars)

    warmup_start = base_ts
    eval_start = base_ts + 25 * step_ns
    eval_end = base_ts + 79 * step_ns
    return str(inst_id), str(bar_type), warmup_start, eval_start, eval_end


def test_identical_run_golden_reproducibility(tmp_path):
    cat_dir = tmp_path / "nautilus_catalog"
    art_dir = tmp_path / "backtest_artifacts"
    inst_id, bar_type, warmup_start, eval_start, eval_end = _setup_catalog_fixture(cat_dir)

    runner = BacktestRunner(cat_dir, art_dir)

    req1 = BacktestRunRequest(
        run_id="run-golden-001",
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
        random_seed=42,
    )

    req2 = BacktestRunRequest(
        run_id="run-golden-002",
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
        random_seed=42,
    )

    res1 = runner.run(req1)
    res2 = runner.run(req2)

    # 1. Deterministic run_config_hash
    assert res1["run_config_hash"] == res2["run_config_hash"]
    assert len(res1["run_config_hash"]) == 64

    # 2. Performance metrics parity
    metric_keys = [
        "total_return",
        "cagr",
        "sharpe_ratio",
        "sortino_ratio",
        "max_drawdown",
        "profit_factor",
        "win_rate",
        "total_trades",
        "winning_trades",
        "losing_trades",
        "avg_trade_pnl",
        "max_consecutive_losses",
    ]
    for key in metric_keys:
        assert res1[key] == res2[key], f"Metric mismatch on {key}: {res1[key]} vs {res2[key]}"

    # 3. Downsampled equity and monthly returns parity
    assert res1["downsampled_equity"] == res2["downsampled_equity"]
    assert res1["monthly_returns"] == res2["monthly_returns"]

    # 4. Catalog manifest parity
    assert res1["catalog_manifest"] == res2["catalog_manifest"]
    assert len(res1["catalog_manifest"]) > 0
    for entry in res1["catalog_manifest"]:
        assert "sha256" in entry
        assert len(entry["sha256"]) == 64

    # 5. Parquet tabular data parity
    run1_dir = art_dir / "run-golden-001"
    run2_dir = art_dir / "run-golden-002"

    for artifact_name in ["equity_curve.parquet", "trades.parquet", "orders.parquet"]:
        t1 = pq.read_table(run1_dir / artifact_name)
        t2 = pq.read_table(run2_dir / artifact_name)
        assert t1.equals(t2), f"Parquet artifact table mismatch for {artifact_name}"
