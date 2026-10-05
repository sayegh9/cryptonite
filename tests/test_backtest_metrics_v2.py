"""Backtest accuracy (remediation Phase 2): default profile, mark-to-market equity, daily grid, benchmark, fees."""

import json
from decimal import Decimal

import pyarrow.parquet as pq
import pytest
from cryptonite_backtester.runner import BacktestRunner, BacktestRunRequest

DAY = 86_400 * 10**9
HOUR = 3_600 * 10**9
T0 = 1_725_667_200 * 10**9  # 2024-09-07 00:00 UTC


def test_default_execution_profile_is_conservative():
    """R2.1: a run that names no profile pays fees and slippage, like the paper engine."""
    from cryptonite_api.routes.backtests import CreateBacktestRequest

    run = BacktestRunRequest(run_id="r", strategy_slug="s", strategy_version_id="v", instrument_id="i",
                             bar_type="b", warmup_start_ns=0, evaluation_start_ns=1, evaluation_end_ns=2)
    api = CreateBacktestRequest(strategy_slug="s", warmup_start_ns=0, evaluation_start_ns=1, evaluation_end_ns=2)
    assert run.execution_profile == "conservative"
    assert api.execution_profile == "conservative"


# --- R2.2: mark-to-market equity -----------------------------------------------------------------

def test_one_trade_that_falls_15pct_and_recovers_shows_the_drawdown():
    """Golden: buy all-in at 100, bar closes fall to 85, then recover to 100 before the exit. The
    fill-only curve saw no drawdown at all; the marked curve shows 15%."""
    from cryptonite_backtester.metrics import (
        account_equity_series,
        calculate_performance_metrics,
        marked_equity_series,
    )

    path = [100, 95, 90, 85, 90, 95, 100]
    closes = [(T0 + i * HOUR, float(px)) for i, px in enumerate(path)]
    t_buy, t_sell = closes[0][0], closes[-1][0]
    balances = [
        (T0 - HOUR, "USDT", 10_000.0), (T0 - HOUR, "BTC", 0.0),
        (t_buy, "USDT", 0.0), (t_buy, "BTC", 100.0),
        (t_sell, "USDT", 10_000.0), (t_sell, "BTC", 0.0),
    ]
    fills = [(t_buy, 100.0), (t_sell, 100.0)]

    fill_only = account_equity_series(balances, fills, quote_currency="USDT", start_ns=T0)
    marked = marked_equity_series(balances, fills, closes, quote_currency="USDT", start_ns=T0)
    assert calculate_performance_metrics(fill_only, [], 10_000.0)["max_drawdown"] == 0.0
    assert [eq for _, eq in marked] == [px * 100.0 for px in path]
    m = calculate_performance_metrics(marked, [{"pnl": 0.0}], 10_000.0)
    assert m["max_drawdown"] == pytest.approx(0.15, abs=1e-4)
    assert m["total_return"] == 0.0


def test_marked_series_keeps_rows_before_the_window_as_holdings_only():
    from cryptonite_backtester.metrics import marked_equity_series

    balances = [(T0 - 10, "USDT", 0.0), (T0 - 10, "BTC", 2.0)]
    closes = [(T0 - 5, 10.0), (T0, 11.0), (T0 + 5, 12.0), (T0 + 10, 13.0)]
    series = marked_equity_series(balances, [], closes, quote_currency="USDT", start_ns=T0, end_ns=T0 + 5)
    assert series == [(T0, 22.0), (T0 + 5, 24.0)]


# --- R2.2: full UTC daily grid ---------------------------------------------------------------------

def test_fills_five_days_apart_count_every_calendar_day():
    """Points on day 0 and day 5 only: the old per-point daily map gave 2 returns (one of them the
    whole 5-day move); the grid gives one per calendar day, the quiet days at 0%."""
    from cryptonite_backtester.metrics import calculate_performance_metrics, daily_grid_returns

    curve = [(T0, 10_000.0), (T0 + HOUR, 10_100.0), (T0 + 5 * DAY + HOUR, 10_300.0)]
    rets = daily_grid_returns(curve, 10_000.0)
    assert len(rets) == 6  # days 0..5
    assert rets[0] == pytest.approx(0.01)
    assert rets[1:5] == [0.0, 0.0, 0.0, 0.0]
    assert rets[5] == pytest.approx(200 / 10_100)
    m = calculate_performance_metrics(curve, [], 10_000.0)
    assert m["sharpe_ratio"] is not None and m["sharpe_ratio"] > 0


def test_daily_grid_of_a_flat_or_single_day_curve():
    from cryptonite_backtester.metrics import daily_grid_returns, sharpe_sortino

    assert daily_grid_returns([], 1.0) == []
    assert daily_grid_returns([(T0, 1.0), (T0 + 60, 1.0)], 1.0) == [0.0]
    assert sharpe_sortino([0.0]) == (None, None)
    assert sharpe_sortino([0.0, 0.0, 0.0]) == (None, None)


# --- R2.3: benchmark, fees, turnover ---------------------------------------------------------------

def test_benchmark_is_buy_and_hold_over_the_window():
    from cryptonite_backtester.metrics import benchmark_metrics

    closes = [(T0, 100.0), (T0 + DAY, 80.0), (T0 + 2 * DAY, 120.0)]
    b = benchmark_metrics(closes, 10_000.0, strategy_total_return=0.05)
    assert b["total_return"] == pytest.approx(0.2)
    assert b["max_drawdown"] == pytest.approx(0.2)
    assert b["alpha_vs_hold"] == pytest.approx(-0.15)
    assert set(b) == {"total_return", "sharpe_ratio", "max_drawdown", "alpha_vs_hold"}
    assert benchmark_metrics([], 10_000.0, 0.0) is None


def test_fees_and_turnover_value_base_asset_commissions_at_the_fill_price():
    from cryptonite_backtester.metrics import fees_and_turnover

    fees, turnover = fees_and_turnover(
        [(0.1, 50_000.0, [(5.0, "USDT")]), (0.1, 50_000.0, [(0.0001, "BTC")])],
        quote_currency="USDT", initial_balance=10_000.0,
    )
    assert fees == pytest.approx(10.0)
    assert turnover == pytest.approx(1.0)


def _fee_catalog(cat_dir, n_bars: int = 1_500):
    """BTCUSDT with Binance-like fees (maker 5 bps, taker 10 bps) and a zig-zag that makes ema-cross trade often."""
    from cryptonite_schemas.identifiers import (
        to_nautilus_bar_type,
        to_nautilus_instrument_id,
        to_spot_instrument_id,
    )
    from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
    from nautilus_trader.model import Bar, Currency, CurrencyPair, Price, Quantity

    mgr = NautilusCatalogManager(cat_dir)
    inst_id = to_nautilus_instrument_id(to_spot_instrument_id("BTCUSDT"))
    mgr.write_instruments([CurrencyPair(
        instrument_id=inst_id, raw_symbol=inst_id.symbol,
        base_currency=Currency.from_str("BTC"), quote_currency=Currency.from_str("USDT"),
        price_precision=2, size_precision=5,
        price_increment=Price(Decimal("0.01"), 2), size_increment=Quantity(Decimal("0.00001"), 5),
        maker_fee=Decimal("0.0005"), taker_fee=Decimal("0.001"),
        ts_event=T0, ts_init=T0,
    )])
    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    step = 60 * 10**9
    bars, px = [], 50_000.0
    for i in range(n_bars):
        c = px + (40.0 if (i // 6) % 2 == 0 else -40.0)
        bars.append(Bar(
            bar_type=bar_type, open=Price(Decimal(f"{px:.2f}"), 2), high=Price(Decimal(f"{max(px, c) + 20:.2f}"), 2),
            low=Price(Decimal(f"{min(px, c) - 20:.2f}"), 2), close=Price(Decimal(f"{c:.2f}"), 2),
            volume=Quantity(Decimal("5"), 5), ts_event=T0 + i * step, ts_init=T0 + i * step,
        ))
        px = c
    mgr.write_bars(bars)
    return str(inst_id), str(bar_type), T0, T0 + 30 * step, T0 + (n_bars - 1) * step


def test_conservative_run_pays_taker_fees_on_every_fill_and_reports_v2_metrics(tmp_path):
    """R2.3 fees: fees_paid equals the traded notional x 10 bps (market orders are takers). Also checks
    the v2 fields reach the result, the manifest and the DB artifact manifest, and stay out of the hash."""
    from cryptonite_backtester.execution_profiles import resolve_execution_profile
    from cryptonite_backtester.runner import compute_run_config_hash

    inst, bar_type, warm, start, end = _fee_catalog(tmp_path / "cat")
    runner = BacktestRunner(tmp_path / "cat", tmp_path / "art")
    req = BacktestRunRequest(
        run_id="fees-1", strategy_slug="ema-cross", strategy_version_id="00000000-0000-0000-0000-000000000001",
        instrument_id=inst, bar_type=bar_type, timeframe="1m",
        parameters={"fast_period": 3, "slow_period": 6, "trade_size": 0.01},
        warmup_start_ns=warm, evaluation_start_ns=start, evaluation_end_ns=end,
        execution_profile="conservative",
    )
    res = runner.run(req)
    art = tmp_path / "art" / "fees-1"

    orders = pq.read_table(art / "orders.parquet").to_pylist()
    notional = sum(o["filled_qty"] * o["avg_px"] for o in orders)
    assert len([o for o in orders if o["filled_qty"] > 0]) >= 100  # NETTING: one position id, many round trips
    assert res["fees_paid"] == pytest.approx(notional * 0.001, rel=1e-6)
    assert res["turnover"] == pytest.approx(notional / req.initial_balance, rel=1e-5)
    assert res["metrics_version"] == 2
    assert set(res["benchmark"]) == {"total_return", "sharpe_ratio", "max_drawdown", "alpha_vs_hold"}

    manifest = json.loads((art / "manifest.json").read_text())
    assert manifest["metrics_version"] == 2
    assert manifest["artifacts"]["metrics"]["fees_paid"] == res["fees_paid"]
    assert res["artifact_manifest"]["metrics"]["metrics_version"] == 2

    # marked at every bar close of the window, not only at fills
    equity = pq.read_table(art / "equity_curve.parquet").to_pylist()
    assert len(equity) >= (end - start) // (60 * 10**9)

    # metrics_version describes outputs: the input hash does not contain it
    _, meta = resolve_execution_profile("conservative", starting_balance="10000.00 USDT", random_seed=42)
    assert res["run_config_hash"] == compute_run_config_hash(req, meta)


# --- R2.4: re-run legacy runs ----------------------------------------------------------------------

async def test_rerun_script_queues_a_copy_of_legacy_runs_only():
    import importlib.util
    from pathlib import Path

    import asyncpg
    from connections import DB_URL

    spec = importlib.util.spec_from_file_location(
        "rerun_backtests", Path(__file__).resolve().parents[1] / "scripts" / "rerun_backtests.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    conn = await asyncpg.connect(DB_URL)
    tx = conn.transaction()
    await tx.start()
    try:
        strategy = await conn.fetchval("INSERT INTO strategies (name, slug, description, author) "
                                       "VALUES ('r', 'rerun-test', 'd', 'a') RETURNING id")
        version = await conn.fetchval(
            "INSERT INTO strategy_versions (strategy_id, version_num, code_hash, git_commit, default_parameters, "
            "parameter_schema) VALUES ($1, 1, 'h', 'c', '{}'::jsonb, '{}'::jsonb) RETURNING id", strategy)
        runs = {}
        for name, metrics in (("legacy", {}), ("v2", {"metrics": {"metrics_version": 2}})):
            runs[name] = await conn.fetchval(
                "INSERT INTO backtest_runs (strategy_version_id, instrument_id, bar_type, timeframe, parameters, "
                "warmup_start_ns, evaluation_start_ns, evaluation_end_ns, execution_profile, run_config_hash, status) "
                "VALUES ($1, 'BTCUSDT.BINANCE', 'BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL', '1m', '{\"fast_period\": 5}', "
                "0, 1, 2, 'optimistic', 'h', 'COMPLETED') RETURNING id", version)
            await conn.execute(
                "INSERT INTO backtest_results (backtest_run_id, total_return, max_drawdown, win_rate, total_trades, "
                "winning_trades, losing_trades, avg_trade_pnl, max_consecutive_losses, downsampled_equity, "
                "monthly_returns, catalog_manifest, artifact_manifest) "
                "VALUES ($1, 0, 0, 0, 0, 0, 0, 0, 0, '[]', '{}', '[]', $2::jsonb)", runs[name], json.dumps(metrics))

        dry = await mod.rerun(conn, list(runs.values()), None, dry_run=True)
        assert dry == [(runs["legacy"], None)]
        queued = await mod.rerun(conn, list(runs.values()), None)
        assert [old for old, _ in queued] == [runs["legacy"]]
        new = await conn.fetchrow("SELECT * FROM backtest_runs WHERE id = $1", queued[0][1])
        assert new["status"] == "PENDING" and new["execution_profile"] == "optimistic"
        assert json.loads(new["parameters"]) == {"fast_period": 5} and len(new["run_config_hash"]) == 64
        assert len(await mod.rerun(conn, list(runs.values()), None, include_v2=True, dry_run=True)) == 2
    finally:
        await tx.rollback()
        await conn.close()
