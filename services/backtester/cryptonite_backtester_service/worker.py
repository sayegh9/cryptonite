"""Dedicated Backtest Worker Daemon with lease fencing, outbox emission, and 3-way crash recovery."""

import asyncio
import json
import math

def _sanitize_floats(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return 0.0
        return obj
    elif isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_floats(i) for i in obj]
    return obj

import logging
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
from cryptonite_backtester.runner import BacktestRunner, BacktestRunRequest
from cryptonite_backtester.sandbox import DEFAULT_JOBS_DIR, run_in_sandbox
from cryptonite_backtester.storage import BacktestArtifactStore
from cryptonite_strategies.registry import STRATEGY_REGISTRY

logger = logging.getLogger("cryptonite.backtester.worker")

DEFAULT_DB_URL = "postgresql://cryptonite_backtester:backtester_dev_pass@127.0.0.1:5432/cryptonite"
DEFAULT_CATALOG_PATH = "data/nautilus_catalog"
DEFAULT_ARTIFACTS_PATH = "data/backtest_artifacts"


class LeaseFencingError(Exception):
    """Raised when a worker's lease expires or another worker claims the run."""
    pass


class IntegrityIncidentError(Exception):
    """Raised when database records exist for a backtest run but disk artifacts are missing."""
    pass


class BacktestWorker:
    """Consumes pending backtest_runs and executes them via BacktestNode with lease fencing."""

    def __init__(
        self,
        database_url: str | None = None,
        catalog_path: str | None = None,
        artifacts_path: str | None = None,
        lease_seconds: int = 300,
        max_attempts: int = 3,
        poll_interval_s: float = 1.0,
        worker_id: str | None = None,
    ) -> None:
        self.database_url = database_url or os.getenv("DATABASE_URL", DEFAULT_DB_URL)
        self.catalog_path = Path(catalog_path or os.getenv("NAUTILUS_CATALOG_PATH", DEFAULT_CATALOG_PATH)).resolve()
        self.artifacts_path = Path(artifacts_path or os.getenv("BACKTEST_ARTIFACTS_PATH", DEFAULT_ARTIFACTS_PATH)).resolve()
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.poll_interval_s = poll_interval_s
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{time.time_ns()}"

        self.pool: asyncpg.Pool | None = None
        self.runner = BacktestRunner(self.catalog_path, self.artifacts_path)
        self.artifact_store = BacktestArtifactStore(self.artifacts_path)
        self.sandbox_jobs_path = Path(os.getenv("STRATEGY_SANDBOX_JOBS_PATH", DEFAULT_JOBS_DIR))
        self._running = False
        self._task: asyncio.Task[None] | None = None

        self.completed_count = 0
        self.failed_count = 0

    async def start(self) -> None:
        """Initialize DB connection pool and start polling loop."""
        logger.info("Starting BacktestWorker %s", self.worker_id)
        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5)
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop worker cleanly."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self.pool:
            await self.pool.close()
            self.pool = None
        logger.info("BacktestWorker stopped")

    async def _claim_run(self) -> dict[str, Any] | None:
        """Claim one pending or expired run using FOR UPDATE SKIP LOCKED with fresh attempt_id."""
        if not self.pool:
            return None

        query = f"""
        WITH claimed AS (
            SELECT id
            FROM backtest_runs
            WHERE status = 'PENDING'
               OR (status IN ('CLAIMED', 'RUNNING') AND lease_expires_at < now())
            ORDER BY created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        UPDATE backtest_runs
        SET status = 'RUNNING',
            lease_owner = $1,
            lease_expires_at = now() + INTERVAL '{self.lease_seconds} seconds',
            attempt_count = attempt_count + 1,
            attempt_id = gen_random_uuid(),
            started_at = COALESCE(started_at, now())
        FROM claimed
        WHERE backtest_runs.id = claimed.id
        RETURNING backtest_runs.id, backtest_runs.attempt_id, backtest_runs.strategy_version_id,
                  backtest_runs.instrument_id, backtest_runs.bar_type,
                  backtest_runs.timeframe, backtest_runs.parameters,
                  backtest_runs.warmup_start_ns, backtest_runs.evaluation_start_ns,
                  backtest_runs.evaluation_end_ns, backtest_runs.execution_profile,
                  backtest_runs.run_config_hash, backtest_runs.attempt_count;
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(query, self.worker_id)
                return dict(row) if row else None

    async def _check_three_way_recovery(self, run_id: str | UUID) -> bool:
        """Three-way crash recovery check handling all 4 recovery states explicitly.
        
        1. Fully consistent: Both disk artifacts and DB results exist -> reconciled to COMPLETED.
        2. Disk only: Valid sealed artifacts exist -> reconstruct DB result from manifest, mark COMPLETED.
        3. DB only: DB result exists but disk artifacts missing -> INTEGRITY_FAILED incident.
        4. Neither: Normal execution path.
        """
        if not self.pool:
            return False

        run_id_str = str(run_id)
        uid = UUID(run_id_str)
        has_disk = self.artifact_store.verify_artifacts(run_id_str)
        manifest = self.artifact_store.load_manifest(run_id_str) if has_disk else None

        async with self.pool.acquire() as conn:
            res_row = await conn.fetchrow(
                "SELECT id FROM backtest_results WHERE backtest_run_id = $1::uuid",
                uid,
            )
            run_row = await conn.fetchrow(
                "SELECT status, run_config_hash FROM backtest_runs WHERE id = $1::uuid",
                uid,
            )

        has_db = res_row is not None
        is_completed = run_row is not None and run_row["status"] == "COMPLETED"

        # Case 1: Fully consistent
        if has_disk and has_db:
            if not is_completed:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE backtest_runs
                        SET status = 'COMPLETED',
                            completed_at = COALESCE(completed_at, now()),
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            error_message = NULL
                        WHERE id = $1::uuid
                        """,
                        uid,
                    )
            logger.info("Three-way recovery: run %s is fully consistent and COMPLETED", run_id_str)
            self.completed_count += 1
            return True

        # Case 2: Disk only (sealed artifacts on disk, but worker died before DB commit)
        if has_disk and not has_db:
            if manifest and "artifacts" in manifest:
                logger.info("Three-way recovery: reconstructing DB result for run %s from disk manifest", run_id_str)
                # Reconstruct DB result record from manifest data
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            """
                            INSERT INTO backtest_results (
                                backtest_run_id, total_return, cagr, sharpe_ratio, sortino_ratio,
                                max_drawdown, profit_factor, win_rate, total_trades, winning_trades,
                                losing_trades, avg_trade_pnl, max_consecutive_losses, downsampled_equity,
                                monthly_returns, catalog_manifest, artifact_manifest
                            ) VALUES (
                                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                                $14::jsonb, $15::jsonb, $16::jsonb, $17::jsonb
                            ) ON CONFLICT (backtest_run_id) DO NOTHING
                            """,
                            uid,
                            manifest.get("total_return", 0.0),
                            manifest.get("cagr"),
                            manifest.get("sharpe_ratio"),
                            manifest.get("sortino_ratio"),
                            manifest.get("max_drawdown", 0.0),
                            manifest.get("profit_factor"),
                            manifest.get("win_rate", 0.0),
                            manifest.get("total_trades", 0),
                            manifest.get("winning_trades", 0),
                            manifest.get("losing_trades", 0),
                            manifest.get("avg_trade_pnl", 0.0),
                            manifest.get("max_consecutive_losses", 0),
                            json.dumps(manifest.get("downsampled_equity", [])),
                            json.dumps(manifest.get("monthly_returns", {})),
                            json.dumps(manifest.get("catalog_manifest", [])),
                            json.dumps(manifest.get("artifacts", {})),
                        )
                        await conn.execute(
                            """
                            UPDATE backtest_runs
                            SET status = 'COMPLETED',
                                completed_at = now(),
                                lease_owner = NULL,
                                lease_expires_at = NULL,
                                error_message = NULL
                            WHERE id = $1::uuid
                            """,
                            uid,
                        )
                self.completed_count += 1
                return True
            else:
                logger.warning("Three-way recovery: disk artifacts corrupt for run %s, allowing retry", run_id_str)
                return False

        # Case 3: DB only (DB has results/completed status, but artifacts missing on disk)
        if not has_disk and (has_db or is_completed):
            logger.error("INTEGRITY INCIDENT: run %s has database results but missing disk artifacts!", run_id_str)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE backtest_runs
                    SET status = 'INTEGRITY_FAILED',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        error_message = 'INTEGRITY INCIDENT: Missing disk artifacts for completed run'
                    WHERE id = $1::uuid
                    """,
                    uid,
                )
            raise IntegrityIncidentError(f"Missing disk artifacts for completed run {run_id_str}")

        return False

    async def _execute_claimed_run(self, run_dict: dict[str, Any]) -> None:
        """Execute the claimed run with attempt-specific staging, atomic promotion, and lease fencing."""
        run_id = run_dict["id"]
        run_id_str = str(run_id)
        attempt_id = str(run_dict.get("attempt_id") or uuid.uuid4())

        # First check three-way recovery in case this run crashed post-write
        if await self._check_three_way_recovery(run_id):
            return

        # Fetch strategy slug from strategy_version
        async with self.pool.acquire() as conn:
            strat_row = await conn.fetchrow(
                """
                SELECT s.slug, sv.python_code
                FROM strategy_versions sv
                JOIN strategies s ON s.id = sv.strategy_id
                WHERE sv.id = $1
                """,
                run_dict["strategy_version_id"],
            )
            if not strat_row:
                await self._mark_failed(run_id, f"Strategy version {run_dict['strategy_version_id']} not found.")
                return
            strategy_slug = strat_row["slug"]
            python_code = strat_row["python_code"]

        raw_params = run_dict["parameters"]
        if isinstance(raw_params, str):
            params = json.loads(raw_params)
        else:
            params = dict(raw_params)

        request = BacktestRunRequest(
            run_id=run_id_str,
            strategy_slug=strategy_slug,
            strategy_version_id=str(run_dict["strategy_version_id"]),
            instrument_id=run_dict["instrument_id"],
            bar_type=run_dict["bar_type"],
            timeframe=run_dict["timeframe"],
            parameters=params,
            warmup_start_ns=run_dict["warmup_start_ns"],
            evaluation_start_ns=run_dict["evaluation_start_ns"],
            evaluation_end_ns=run_dict["evaluation_end_ns"],
            execution_profile=run_dict["execution_profile"],
            attempt_id=attempt_id,
            run_config_hash=run_dict.get("run_config_hash"),
            python_code=python_code,
        )

        try:
            # 1. Start lease heartbeat to avoid expiration
            async def lease_heartbeat():
                try:
                    while True:
                        await asyncio.sleep(self.lease_seconds / 2)
                        async with self.pool.acquire() as h_conn:
                            await h_conn.execute(
                                "UPDATE backtest_runs SET lease_expires_at = now() + INTERVAL '300 seconds' WHERE id = $1 AND lease_owner = $2 AND attempt_id = $3::uuid",
                                UUID(run_id_str), self.worker_id, UUID(attempt_id)
                            )
                except asyncio.CancelledError:
                    pass

            heartbeat_task = asyncio.create_task(lease_heartbeat())
            try:
                if request.strategy_slug not in STRATEGY_REGISTRY and request.python_code:
                    # Model-written code never runs in this process (it holds DB credentials)
                    result_dict = await run_in_sandbox(request, self.artifact_store, self.sandbox_jobs_path)
                    # The sandbox's own catalog manifest is untrusted; rebuild it from the worker's catalog
                    result_dict["catalog_manifest"] = await asyncio.to_thread(
                        self.runner.build_catalog_manifest, request.bar_type,
                        request.warmup_start_ns, request.evaluation_end_ns,
                    )
                else:
                    # Execute securely with context propagation
                    result_dict = await asyncio.to_thread(self.runner.run, request)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

            # 2. Conditionally commit in a single PostgreSQL transaction with lease fencing predicate
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    # Strict lease fencing predicate: owner, attempt_id, and unexpired lease
                    updated_row = await conn.fetchrow(
                        """
                        UPDATE backtest_runs
                        SET status = 'COMPLETED',
                            completed_at = now(),
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            error_message = NULL
                        WHERE id = $1
                          AND status = 'RUNNING'
                          AND lease_owner = $2
                          AND attempt_id = $3::uuid
                          AND lease_expires_at > now()
                        RETURNING id;
                        """,
                        UUID(run_id_str),
                        self.worker_id,
                        UUID(attempt_id),
                    )
                    if not updated_row:
                        raise LeaseFencingError(
                            f"Fencing predicate failed: lease expired or claimed by another worker. "
                            f"Run {run_id_str}, attempt {attempt_id}."
                        )

                    # Insert backtest results
                    await conn.execute(
                        """
                        INSERT INTO backtest_results (
                            backtest_run_id, total_return, cagr, sharpe_ratio, sortino_ratio,
                            max_drawdown, profit_factor, win_rate, total_trades, winning_trades,
                            losing_trades, avg_trade_pnl, max_consecutive_losses, downsampled_equity,
                            monthly_returns, catalog_manifest, artifact_manifest
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                            $14::jsonb, $15::jsonb, $16::jsonb, $17::jsonb
                        )
                        """,
                        UUID(run_id_str),
                        _sanitize_floats(result_dict["total_return"]),
                        _sanitize_floats(result_dict["cagr"]),
                        _sanitize_floats(result_dict["sharpe_ratio"]),
                        _sanitize_floats(result_dict["sortino_ratio"]),
                        _sanitize_floats(result_dict["max_drawdown"]),
                        _sanitize_floats(result_dict["profit_factor"]),
                        _sanitize_floats(result_dict["win_rate"]),
                        result_dict["total_trades"],
                        result_dict["winning_trades"],
                        result_dict["losing_trades"],
                        _sanitize_floats(result_dict["avg_trade_pnl"]),
                        result_dict["max_consecutive_losses"],
                        json.dumps(_sanitize_floats(result_dict["downsampled_equity"])),
                        json.dumps(_sanitize_floats(result_dict["monthly_returns"])),
                        json.dumps(_sanitize_floats(result_dict["catalog_manifest"])),
                        json.dumps(_sanitize_floats(result_dict["artifact_manifest"])),
                    )

                    # Insert deterministic outbox event in the same transaction
                    outbox_event_id = uuid.uuid5(uuid.NAMESPACE_URL, f"backtest_completed:{run_id_str}:{attempt_id}").hex
                    outbox_payload = {
                        "backtest_run_id": run_id_str,
                        "attempt_id": attempt_id,
                        "strategy_slug": strategy_slug,
                        "instrument_id": run_dict["instrument_id"],
                        "execution_profile": run_dict["execution_profile"],
                        "total_return": result_dict["total_return"],
                        "sharpe_ratio": result_dict["sharpe_ratio"],
                        "max_drawdown": result_dict["max_drawdown"],
                        "artifact_manifest": result_dict["artifact_manifest"],
                        "sandboxed": bool(result_dict.get("sandboxed")),
                    }
                    await conn.execute(
                        """
                        INSERT INTO outbox_events (
                            event_id, stream_name, event_type, producer, payload,
                            status, attempt_count, next_attempt_at, created_at
                        ) VALUES ($1, $2, $3, $4, $5::jsonb, 'PENDING', 0, now(), now())
                        ON CONFLICT (event_id) DO NOTHING;
                        """,
                        outbox_event_id,
                        "cryptonite:stream:backtest_completed",
                        "backtest_completed",
                        "cryptonite-backtester",
                        json.dumps(outbox_payload),
                    )

            # Promote staged artifacts after successful DB commit
            self.artifact_store.promote_staged_artifacts(run_id_str, attempt_id)

            self.completed_count += 1
            logger.info("Backtest %s completed successfully (attempt %s)", run_id_str, attempt_id)

        except LeaseFencingError as lfe:
            logger.warning("Backtest %s lease fencing failure: %s (artifacts retained for recovery)", run_id_str, lfe)
            # Staged artifacts are retained for recovery, never silently deleted
        except IntegrityIncidentError as iie:
            logger.error("Backtest %s integrity incident: %s", run_id_str, iie)
            self.failed_count += 1
        except Exception as exc:
            logger.exception("Backtest %s execution failed: %s", run_id_str, exc)
            attempt_count = run_dict.get("attempt_count", 1)
            if attempt_count >= self.max_attempts:
                await self._mark_failed(run_id, str(exc))
            else:
                # Release lease to allow retry
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE backtest_runs
                        SET status = 'PENDING',
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            error_message = $2
                        WHERE id = $1
                        """,
                        run_id,
                        f"Attempt {attempt_count} failed: {exc}",
                    )

    async def _mark_failed(self, run_id: UUID | str, error_msg: str) -> None:
        """Mark run as permanently FAILED (dead-letter)."""
        if not self.pool:
            return
        uid = UUID(str(run_id))
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE backtest_runs
                SET status = 'FAILED',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    error_message = $2,
                    completed_at = now()
                WHERE id = $1
                """,
                uid,
                error_msg,
            )
        self.failed_count += 1

    async def _run_loop(self) -> None:
        """Background loop continuously claiming and processing runs."""
        while self._running:
            try:
                run_dict = await self._claim_run()
                if run_dict:
                    await self._execute_claimed_run(run_dict)
                else:
                    await asyncio.sleep(self.poll_interval_s)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in backtest worker loop: %s", e)
                await asyncio.sleep(self.poll_interval_s)
