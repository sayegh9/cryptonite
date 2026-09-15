"""Comprehensive matrix tests for BacktestWorker three-way crash recovery:
1. Fully consistent: Reconciled to COMPLETED.
2. Disk only: DB result reconstructed from manifest, run marked COMPLETED.
3. DB only: Integrity incident flagged, run marked INTEGRITY_FAILED, IntegrityIncidentError raised.
4. Corrupt disk artifacts: Returns False to allow safe retry.
"""

import json
from pathlib import Path
from uuid import uuid4

import asyncpg
import pyarrow as pa
import pytest
from cryptonite_backtester.storage import BacktestArtifactStore
from cryptonite_backtester_service.worker import BacktestWorker, IntegrityIncidentError

DB_URL = "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"


def _write_dummy_artifacts(store: BacktestArtifactStore, run_id_str: str, total_return: float = 0.05):
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
        manifest_meta={
            "run_id": run_id_str,
            "total_return": total_return,
            "sharpe_ratio": 1.5,
            "max_drawdown": 0.02,
            "total_trades": 1,
            "win_rate": 1.0,
            "run_config_hash": "hash-valid",
        },
    )


@pytest.mark.asyncio
async def test_recovery_matrix_fully_consistent(tmp_path: Path):
    """Case 1: Both disk artifacts and DB result exist -> marked COMPLETED."""
    pool = await asyncpg.create_pool(DB_URL)
    try:
        run_id = uuid4()
        run_id_str = str(run_id)
        art_dir = tmp_path / "artifacts"
        store = BacktestArtifactStore(art_dir)
        _write_dummy_artifacts(store, run_id_str)

        strat_id = uuid4()
        ver_id = uuid4()
        slug = f"rec-cons-{uuid4().hex[:6]}"

        await pool.execute(
            "INSERT INTO strategies (id, name, slug, description, author) VALUES ($1, $2, $3, 'Desc', 'Auth')",
            strat_id, slug, slug,
        )
        await pool.execute(
            "INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema) VALUES ($1, $2, 1, 'h', 'c', '{}'::jsonb, '{}'::jsonb)",
            ver_id, strat_id,
        )
        await pool.execute(
            """
            INSERT INTO backtest_runs (
                id, strategy_version_id, instrument_id, bar_type, timeframe, parameters,
                warmup_start_ns, evaluation_start_ns, evaluation_end_ns, execution_profile,
                run_config_hash, status, lease_owner, attempt_count
            ) VALUES ($1, $2, 'BTCUSDT.BINANCE', 'BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL', '1m', '{}'::jsonb, 1, 2, 3, 'optimistic', 'hash-valid', 'RUNNING', 'worker-1', 1)
            """,
            run_id, ver_id,
        )
        await pool.execute(
            """
            INSERT INTO backtest_results (
                backtest_run_id, total_return, cagr, sharpe_ratio, sortino_ratio, max_drawdown,
                profit_factor, win_rate, total_trades, winning_trades, losing_trades,
                avg_trade_pnl, max_consecutive_losses, downsampled_equity, monthly_returns,
                catalog_manifest, artifact_manifest
            ) VALUES ($1, 0.05, NULL, 1.5, NULL, 0.02, NULL, 1.0, 1, 1, 0, 100.0, 0, '[]'::jsonb, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb)
            """,
            run_id,
        )

        worker = BacktestWorker(database_url=DB_URL, artifacts_path=str(art_dir))
        worker.pool = pool

        result = await worker._check_three_way_recovery(run_id)
        assert result is True

        row = await pool.fetchrow("SELECT status FROM backtest_runs WHERE id = $1", run_id)
        assert row["status"] == "COMPLETED"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_recovery_matrix_disk_only_reconstruction(tmp_path: Path):
    """Case 2: Disk artifacts exist but DB results missing -> reconstructed from manifest."""
    pool = await asyncpg.create_pool(DB_URL)
    try:
        run_id = uuid4()
        run_id_str = str(run_id)
        art_dir = tmp_path / "artifacts"
        store = BacktestArtifactStore(art_dir)
        _write_dummy_artifacts(store, run_id_str, total_return=0.08)

        strat_id = uuid4()
        ver_id = uuid4()
        slug = f"rec-disk-{uuid4().hex[:6]}"

        await pool.execute(
            "INSERT INTO strategies (id, name, slug, description, author) VALUES ($1, $2, $3, 'Desc', 'Auth')",
            strat_id, slug, slug,
        )
        await pool.execute(
            "INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema) VALUES ($1, $2, 1, 'h', 'c', '{}'::jsonb, '{}'::jsonb)",
            ver_id, strat_id,
        )
        await pool.execute(
            """
            INSERT INTO backtest_runs (
                id, strategy_version_id, instrument_id, bar_type, timeframe, parameters,
                warmup_start_ns, evaluation_start_ns, evaluation_end_ns, execution_profile,
                run_config_hash, status, lease_owner, attempt_count
            ) VALUES ($1, $2, 'BTCUSDT.BINANCE', 'BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL', '1m', '{}'::jsonb, 1, 2, 3, 'optimistic', 'hash-valid', 'RUNNING', 'crashed-worker', 1)
            """,
            run_id, ver_id,
        )

        worker = BacktestWorker(database_url=DB_URL, artifacts_path=str(art_dir))
        worker.pool = pool

        # Must reconstruct DB record from manifest and mark COMPLETED
        result = await worker._check_three_way_recovery(run_id)
        assert result is True

        run_row = await pool.fetchrow("SELECT status FROM backtest_runs WHERE id = $1", run_id)
        assert run_row["status"] == "COMPLETED"

        res_row = await pool.fetchrow("SELECT total_return FROM backtest_results WHERE backtest_run_id = $1", run_id)
        assert res_row is not None
        assert float(res_row["total_return"]) == pytest.approx(0.08)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_recovery_matrix_db_only_integrity_incident(tmp_path: Path):
    """Case 3: DB has results but artifacts are missing on disk -> IntegrityIncidentError & INTEGRITY_FAILED."""
    pool = await asyncpg.create_pool(DB_URL)
    try:
        run_id = uuid4()
        art_dir = tmp_path / "artifacts"

        strat_id = uuid4()
        ver_id = uuid4()
        slug = f"rec-dbo-{uuid4().hex[:6]}"

        await pool.execute(
            "INSERT INTO strategies (id, name, slug, description, author) VALUES ($1, $2, $3, 'Desc', 'Auth')",
            strat_id, slug, slug,
        )
        await pool.execute(
            "INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema) VALUES ($1, $2, 1, 'h', 'c', '{}'::jsonb, '{}'::jsonb)",
            ver_id, strat_id,
        )
        await pool.execute(
            """
            INSERT INTO backtest_runs (
                id, strategy_version_id, instrument_id, bar_type, timeframe, parameters,
                warmup_start_ns, evaluation_start_ns, evaluation_end_ns, execution_profile,
                run_config_hash, status, attempt_count
            ) VALUES ($1, $2, 'BTCUSDT.BINANCE', 'BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL', '1m', '{}'::jsonb, 1, 2, 3, 'optimistic', 'hash-valid', 'COMPLETED', 1)
            """,
            run_id, ver_id,
        )
        await pool.execute(
            """
            INSERT INTO backtest_results (
                backtest_run_id, total_return, cagr, sharpe_ratio, sortino_ratio, max_drawdown,
                profit_factor, win_rate, total_trades, winning_trades, losing_trades,
                avg_trade_pnl, max_consecutive_losses, downsampled_equity, monthly_returns,
                catalog_manifest, artifact_manifest
            ) VALUES ($1, 0.05, NULL, 1.5, NULL, 0.02, NULL, 1.0, 1, 1, 0, 100.0, 0, '[]'::jsonb, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb)
            """,
            run_id,
        )

        worker = BacktestWorker(database_url=DB_URL, artifacts_path=str(art_dir))
        worker.pool = pool

        with pytest.raises(IntegrityIncidentError, match="Missing disk artifacts"):
            await worker._check_three_way_recovery(run_id)

        row = await pool.fetchrow("SELECT status, error_message FROM backtest_runs WHERE id = $1", run_id)
        assert row["status"] == "INTEGRITY_FAILED"
        assert "INTEGRITY INCIDENT" in row["error_message"]
    finally:
        await pool.close()
