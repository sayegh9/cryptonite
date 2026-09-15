"""Test suite for BacktestWorker two-phase lease claims and three-way crash recovery."""

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import asyncpg
import pyarrow as pa
import pytest

from cryptonite_backtester.storage import BacktestArtifactStore
from cryptonite_backtester_service.worker import BacktestWorker

DB_URL = "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"


@pytest.mark.asyncio
async def test_two_phase_lease_claim_and_expiry(tmp_path: Path):
    pool = await asyncpg.create_pool(DB_URL)
    try:
        strat_id = uuid4()
        ver_id = uuid4()
        run_id = uuid4()
        slug = f"lease-test-{uuid4().hex[:8]}"

        # Clean up any leftover test runs from earlier tests
        await pool.execute("DELETE FROM backtest_results")
        await pool.execute("DELETE FROM backtest_runs")

        # Setup strategy & version
        await pool.execute(
            """
            INSERT INTO strategies (id, name, slug, description, author)
            VALUES ($1, $2, $3, 'Lease Test Strategy', 'Tester')
            """,
            strat_id,
            slug,
            slug,
        )
        await pool.execute(
            """
            INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema)
            VALUES ($1, $2, 1, 'code-hash-1', 'commit-1', '{}'::jsonb, '{}'::jsonb)
            """,
            ver_id,
            strat_id,
        )

        # Setup pending run
        await pool.execute(
            """
            INSERT INTO backtest_runs (
                id, strategy_version_id, instrument_id, bar_type, timeframe, parameters,
                warmup_start_ns, evaluation_start_ns, evaluation_end_ns, execution_profile,
                run_config_hash, status, attempt_count
            ) VALUES (
                $1, $2, 'BTCUSDT.BINANCE', 'BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL', '1m', '{}'::jsonb,
                1725700000000000000, 1725700600000000000, 1725701200000000000, 'optimistic',
                'hash-pending', 'PENDING', 0
            )
            """,
            run_id,
            ver_id,
        )

        worker_1 = BacktestWorker(
            database_url=DB_URL,
            catalog_path=str(tmp_path / "catalog"),
            artifacts_path=str(tmp_path / "artifacts"),
            worker_id="worker-alpha",
            lease_seconds=60,
        )
        worker_1.pool = pool

        worker_2 = BacktestWorker(
            database_url=DB_URL,
            catalog_path=str(tmp_path / "catalog"),
            artifacts_path=str(tmp_path / "artifacts"),
            worker_id="worker-beta",
            lease_seconds=60,
        )
        worker_2.pool = pool

        # 1. Worker 1 claims the pending run
        claimed_1 = await worker_1._claim_run()
        assert claimed_1 is not None
        assert claimed_1["id"] == run_id

        # Verify DB state
        row = await pool.fetchrow("SELECT status, lease_owner, attempt_count FROM backtest_runs WHERE id = $1", run_id)
        assert row["status"] == "RUNNING"
        assert row["lease_owner"] == "worker-alpha"
        assert row["attempt_count"] == 1

        # 2. Worker 2 attempts to claim concurrently -> must get None
        claimed_2 = await worker_2._claim_run()
        assert claimed_2 is None

        # 3. Simulate lease expiry on Worker 1
        await pool.execute(
            "UPDATE backtest_runs SET lease_expires_at = now() - INTERVAL '10 seconds' WHERE id = $1",
            run_id,
        )

        # 4. Worker 2 now claims expired run
        claimed_reclaim = await worker_2._claim_run()
        assert claimed_reclaim is not None
        assert claimed_reclaim["id"] == run_id

        # Verify DB state after reclamation
        row_after = await pool.fetchrow("SELECT status, lease_owner, attempt_count FROM backtest_runs WHERE id = $1", run_id)
        assert row_after["status"] == "RUNNING"
        assert row_after["lease_owner"] == "worker-beta"
        assert row_after["attempt_count"] == 2

    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_three_way_crash_recovery_reconciliation(tmp_path: Path):
    pool = await asyncpg.create_pool(DB_URL)
    try:
        strat_id = uuid4()
        ver_id = uuid4()
        run_id = uuid4()
        slug = f"recover-test-{uuid4().hex[:8]}"
        run_id_str = str(run_id)

        artifacts_dir = tmp_path / "artifacts"
        store = BacktestArtifactStore(artifacts_dir)

        # 1. Write valid artifacts to disk
        eq_table = pa.Table.from_arrays(
            [pa.array([1725700000000000000], pa.int64()), pa.array([10000.0], pa.float64()), pa.array([0.0], pa.float64())],
            names=["ts_event_ns", "equity", "drawdown"],
        )
        trades_table = pa.Table.from_arrays(
            [
                pa.array(["pos-1"], pa.string()),
                pa.array(["BTCUSDT.BINANCE"], pa.string()),
                pa.array(["BUY"], pa.string()),
                pa.array([1725700000000000000], pa.int64()),
                pa.array([1725700060000000000], pa.int64()),
                pa.array([50000.0], pa.float64()),
                pa.array([50100.0], pa.float64()),
                pa.array([1.0], pa.float64()),
                pa.array([100.0], pa.float64()),
                pa.array([0.002], pa.float64()),
            ],
            names=[
                "position_id", "instrument_id", "side", "ts_opened_ns", "ts_closed_ns",
                "entry_price", "exit_price", "quantity", "realized_pnl", "realized_return",
            ],
        )
        orders_table = pa.Table.from_arrays(
            [
                pa.array(["ord-1"], pa.string()),
                pa.array(["v-1"], pa.string()),
                pa.array(["BTCUSDT.BINANCE"], pa.string()),
                pa.array(["BUY"], pa.string()),
                pa.array(["MARKET"], pa.string()),
                pa.array(["FILLED"], pa.string()),
                pa.array([1725700000000000000], pa.int64()),
                pa.array([1725700000000000000], pa.int64()),
                pa.array([50000.0], pa.float64()),
                pa.array([1.0], pa.float64()),
                pa.array([1.0], pa.float64()),
                pa.array([50000.0], pa.float64()),
            ],
            names=[
                "client_order_id", "venue_order_id", "instrument_id", "side", "order_type",
                "status", "ts_init_ns", "ts_closed_ns", "price", "quantity", "filled_qty", "avg_px",
            ],
        )
        store.persist_artifacts(
            run_id=run_id_str,
            equity_table=eq_table,
            trades_table=trades_table,
            orders_table=orders_table,
            manifest_meta={"run_id": run_id_str},
        )
        assert store.verify_artifacts(run_id_str) is True

        # 2. Insert DB records: backtest_runs (still in RUNNING status) and backtest_results
        await pool.execute(
            """
            INSERT INTO strategies (id, name, slug, description, author)
            VALUES ($1, $2, $3, 'Recovery Test Strategy', 'Tester')
            """,
            strat_id,
            slug,
            slug,
        )
        await pool.execute(
            """
            INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema)
            VALUES ($1, $2, 1, 'code-hash-2', 'commit-2', '{}'::jsonb, '{}'::jsonb)
            """,
            ver_id,
            strat_id,
        )
        await pool.execute(
            """
            INSERT INTO backtest_runs (
                id, strategy_version_id, instrument_id, bar_type, timeframe, parameters,
                warmup_start_ns, evaluation_start_ns, evaluation_end_ns, execution_profile,
                run_config_hash, status, lease_owner, attempt_count
            ) VALUES (
                $1, $2, 'BTCUSDT.BINANCE', 'BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL', '1m', '{}'::jsonb,
                1725700000000000000, 1725700600000000000, 1725701200000000000, 'optimistic',
                'hash-crash', 'RUNNING', 'dead-worker', 1
            )
            """,
            run_id,
            ver_id,
        )
        await pool.execute(
            """
            INSERT INTO backtest_results (
                backtest_run_id, total_return, cagr, sharpe_ratio, sortino_ratio, max_drawdown,
                profit_factor, win_rate, total_trades, winning_trades, losing_trades,
                avg_trade_pnl, max_consecutive_losses, downsampled_equity, monthly_returns,
                catalog_manifest, artifact_manifest
            ) VALUES (
                $1, 0.01, NULL, 1.5, 2.0, 0.02, 2.0, 1.0, 1, 1, 0, 100.0, 0,
                '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '{}'::jsonb
            )
            """,
            run_id,
        )

        worker = BacktestWorker(
            database_url=DB_URL,
            catalog_path=str(tmp_path / "catalog"),
            artifacts_path=str(artifacts_dir),
            worker_id="recovery-worker",
        )
        worker.pool = pool

        # 3. Check three-way recovery -> returns True and marks run COMPLETED
        recovered = await worker._check_three_way_recovery(run_id)
        assert recovered is True

        row = await pool.fetchrow("SELECT status, lease_owner, completed_at FROM backtest_runs WHERE id = $1", run_id)
        assert row["status"] == "COMPLETED"
        assert row["lease_owner"] is None
        assert row["completed_at"] is not None

        # 4. Verify negative case: missing disk artifacts fails recovery
        missing_run_id = uuid4()
        recovered_missing = await worker._check_three_way_recovery(missing_run_id)
        assert recovered_missing is False

    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_worker_execute_claimed_run_end_to_end(tmp_path: Path):
    """Verify that BacktestWorker executes a claimed run, persists artifacts, and records DB results."""
    from cryptonite_schemas.identifiers import to_nautilus_bar_type, to_nautilus_instrument_id, to_spot_instrument_id
    from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
    from nautilus_trader.model import Bar, Currency, CurrencyPair, Price, Quantity

    cat_dir = tmp_path / "nautilus_catalog"
    art_dir = tmp_path / "backtest_artifacts"
    mgr = NautilusCatalogManager(cat_dir)

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
    for i in range(50):
        ts = base_ts + i * step_ns
        c = px + (15.0 if (i % 6) < 3 else -15.0)
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price(Decimal(str(px)), 2),
                high=Price(Decimal(str(max(px, c) + 5.0)), 2),
                low=Price(Decimal(str(min(px, c) - 5.0)), 2),
                close=Price(Decimal(str(c)), 2),
                volume=Quantity(Decimal("1.0"), 5),
                ts_event=ts,
                ts_init=ts,
            )
        )
        px = c
    mgr.write_bars(bars)

    pool = await asyncpg.create_pool(DB_URL)
    try:
        strat_id = uuid4()
        ver_id = uuid4()
        run_id = uuid4()
        slug = "ema-cross"

        # Insert strategy and version
        await pool.execute(
            """
            INSERT INTO strategies (id, name, slug, description, author)
            VALUES ($1, $2, $3, 'Worker Exec Strategy', 'Tester')
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            """,
            strat_id,
            "EMA Cross Worker Test",
            slug,
        )
        strat_row = await pool.fetchrow("SELECT id FROM strategies WHERE slug = $1", slug)
        actual_strat_id = strat_row["id"]

        await pool.execute(
            """
            INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema)
            VALUES ($1, $2, 999, 'code-hash-worker', 'commit-worker', '{\"fast_period\": 5, \"slow_period\": 10, \"trade_size\": 0.01}'::jsonb, '{}'::jsonb)
            ON CONFLICT DO NOTHING
            """,
            ver_id,
            actual_strat_id,
        )
        ver_row = await pool.fetchrow("SELECT id FROM strategy_versions WHERE strategy_id = $1 ORDER BY version_num DESC LIMIT 1", actual_strat_id)
        actual_ver_id = ver_row["id"]

        eval_start = base_ts + (15 * step_ns)
        eval_end = base_ts + (49 * step_ns)

        attempt_id = uuid4()
        run_dict = {
            "id": run_id,
            "strategy_version_id": actual_ver_id,
            "instrument_id": str(inst_id),
            "bar_type": str(bar_type),
            "timeframe": "1m",
            "parameters": {"fast_period": 5, "slow_period": 10, "trade_size": 0.01},
            "warmup_start_ns": base_ts,
            "evaluation_start_ns": eval_start,
            "evaluation_end_ns": eval_end,
            "execution_profile": "optimistic",
            "run_config_hash": "hash-worker-test",
            "status": "RUNNING",
            "lease_owner": "worker-unit-test",
            "attempt_id": attempt_id,
            "attempt_count": 1,
        }

        await pool.execute(
            """
            INSERT INTO backtest_runs (
                id, strategy_version_id, instrument_id, bar_type, timeframe, parameters,
                warmup_start_ns, evaluation_start_ns, evaluation_end_ns, execution_profile,
                run_config_hash, status, lease_owner, attempt_id, attempt_count, lease_expires_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6::jsonb,
                $7, $8, $9, $10,
                $11, $12, $13, $14, $15, now() + INTERVAL '300 seconds'
            )
            """,
            run_dict["id"],
            run_dict["strategy_version_id"],
            run_dict["instrument_id"],
            run_dict["bar_type"],
            run_dict["timeframe"],
            json.dumps(run_dict["parameters"]),
            run_dict["warmup_start_ns"],
            run_dict["evaluation_start_ns"],
            run_dict["evaluation_end_ns"],
            run_dict["execution_profile"],
            run_dict["run_config_hash"],
            run_dict["status"],
            run_dict["lease_owner"],
            run_dict["attempt_id"],
            run_dict["attempt_count"],
        )

        worker = BacktestWorker(
            database_url=DB_URL,
            catalog_path=str(cat_dir),
            artifacts_path=str(art_dir),
            worker_id="worker-unit-test",
        )
        worker.pool = pool

        # Execute run via worker
        await worker._execute_claimed_run(run_dict)

        # Assert status updated to COMPLETED in database
        updated_run = await pool.fetchrow("SELECT status, lease_owner, completed_at FROM backtest_runs WHERE id = $1", run_id)
        assert updated_run["status"] == "COMPLETED"
        assert updated_run["lease_owner"] is None
        assert updated_run["completed_at"] is not None

        # Assert results record created
        res = await pool.fetchrow("SELECT total_trades, total_return FROM backtest_results WHERE backtest_run_id = $1", run_id)
        assert res is not None
        assert res["total_trades"] is not None

        # Assert artifacts exist on disk
        run_artifacts = art_dir / str(run_id)
        assert (run_artifacts / "equity_curve.parquet").exists()
        assert (run_artifacts / "trades.parquet").exists()
        assert (run_artifacts / "orders.parquet").exists()
        assert (run_artifacts / "manifest.json").exists()

        # Test mark failed error handling
        fail_id = uuid4()
        await pool.execute(
            """
            INSERT INTO backtest_runs (
                id, strategy_version_id, instrument_id, bar_type, timeframe, parameters,
                warmup_start_ns, evaluation_start_ns, evaluation_end_ns, execution_profile,
                run_config_hash, status, attempt_count
            ) VALUES ($1, $2, $3, $4, '1m', '{}'::jsonb, $5, $6, $7, 'optimistic', 'fail-hash', 'RUNNING', 1)
            """,
            fail_id, actual_ver_id, str(inst_id), str(bar_type), base_ts, eval_start, eval_end,
        )
        await worker._mark_failed(fail_id, "Test error message")
        failed_row = await pool.fetchrow("SELECT status, error_message FROM backtest_runs WHERE id = $1", fail_id)
        assert failed_row["status"] == "FAILED"
        assert failed_row["error_message"] == "Test error message"

    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_worker_lease_fencing_expired_attempt_rejected(tmp_path: Path):
    """Verify that a worker whose lease expired or was superseded cannot commit results or overwrite active attempt."""
    from cryptonite_schemas.identifiers import to_nautilus_bar_type, to_nautilus_instrument_id, to_spot_instrument_id
    from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
    from nautilus_trader.model import Bar, Currency, CurrencyPair, Price, Quantity

    cat_dir = tmp_path / "nautilus_catalog"
    art_dir = tmp_path / "backtest_artifacts"
    mgr = NautilusCatalogManager(cat_dir)

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
    for i in range(40):
        ts = base_ts + i * step_ns
        c = px + (10.0 if (i % 4) < 2 else -10.0)
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price(Decimal(str(px)), 2),
                high=Price(Decimal(str(max(px, c) + 5.0)), 2),
                low=Price(Decimal(str(min(px, c) - 5.0)), 2),
                close=Price(Decimal(str(c)), 2),
                volume=Quantity(Decimal("1.0"), 5),
                ts_event=ts,
                ts_init=ts,
            )
        )
        px = c
    mgr.write_bars(bars)

    pool = await asyncpg.create_pool(DB_URL)
    try:
        strat_id = uuid4()
        ver_id = uuid4()
        run_id = uuid4()
        attempt_1 = uuid4()
        attempt_2 = uuid4()
        slug = "ema-cross"

        await pool.execute(
            """
            INSERT INTO strategies (id, name, slug, description, author)
            VALUES ($1, $2, $3, 'Fence Test', 'Tester')
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            """,
            strat_id, "EMA Cross Fence", slug,
        )
        strat_row = await pool.fetchrow("SELECT id FROM strategies WHERE slug = $1", slug)
        actual_strat_id = strat_row["id"]

        await pool.execute(
            """
            INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema)
            VALUES ($1, $2, 998, 'h', 'c', '{\"fast_period\": 5, \"slow_period\": 10, \"trade_size\": 0.01}'::jsonb, '{}'::jsonb)
            ON CONFLICT DO NOTHING
            """,
            ver_id, actual_strat_id,
        )
        ver_row = await pool.fetchrow("SELECT id FROM strategy_versions WHERE strategy_id = $1 ORDER BY version_num DESC LIMIT 1", actual_strat_id)
        actual_ver_id = ver_row["id"]

        eval_start = base_ts + (10 * step_ns)
        eval_end = base_ts + (39 * step_ns)

        # Worker 1 claim
        run_dict_1 = {
            "id": run_id,
            "strategy_version_id": actual_ver_id,
            "instrument_id": str(inst_id),
            "bar_type": str(bar_type),
            "timeframe": "1m",
            "parameters": {"fast_period": 5, "slow_period": 10, "trade_size": 0.01},
            "warmup_start_ns": base_ts,
            "evaluation_start_ns": eval_start,
            "evaluation_end_ns": eval_end,
            "execution_profile": "optimistic",
            "run_config_hash": "hash-fence-test",
            "status": "RUNNING",
            "lease_owner": "worker-stale-1",
            "attempt_id": attempt_1,
            "attempt_count": 1,
        }

        # Simulate that Worker 2 has already reclaimed the run with attempt_2 and updated lease_owner
        await pool.execute(
            """
            INSERT INTO backtest_runs (
                id, strategy_version_id, instrument_id, bar_type, timeframe, parameters,
                warmup_start_ns, evaluation_start_ns, evaluation_end_ns, execution_profile,
                run_config_hash, status, lease_owner, attempt_id, attempt_count, lease_expires_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6::jsonb,
                $7, $8, $9, $10,
                $11, $12, 'worker-active-2', $13, 2, now() + INTERVAL '300 seconds'
            )
            """,
            run_id, actual_ver_id, str(inst_id), str(bar_type), "1m",
            json.dumps(run_dict_1["parameters"]),
            base_ts, eval_start, eval_end, "optimistic", "hash-fence-test",
            "RUNNING", attempt_2,
        )

        worker_stale = BacktestWorker(
            database_url=DB_URL,
            catalog_path=str(cat_dir),
            artifacts_path=str(art_dir),
            worker_id="worker-stale-1",
        )
        worker_stale.pool = pool

        # Worker 1 attempts to execute with superseded attempt_1
        await worker_stale._execute_claimed_run(run_dict_1)

        # Invariant 1: DB status was NOT updated to COMPLETED by stale worker
        row = await pool.fetchrow("SELECT status, lease_owner, attempt_id FROM backtest_runs WHERE id = $1", run_id)
        assert row["status"] == "RUNNING"
        assert row["lease_owner"] == "worker-active-2"
        assert row["attempt_id"] == attempt_2

        # Invariant 2: No backtest_results or outbox events committed by stale worker
        res_count = await pool.fetchval("SELECT count(*) FROM backtest_results WHERE backtest_run_id = $1", run_id)
        assert res_count == 0

        # Invariant 3: Artifacts on disk are retained (not silently deleted)
        assert (art_dir / str(run_id) / "manifest.json").exists() or (art_dir / f".tmp_{run_id}_{attempt_1}" / "manifest.json").exists()

    finally:
        await pool.close()

