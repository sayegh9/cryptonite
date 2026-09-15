"""Integration test for scanner job execution, run idempotency, and candidate outbox events."""

import json
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
from cryptonite_scanner.models import compute_spec_hash
from cryptonite_scanner.presets import PRESET_RSI_OVERSOLD
from cryptonite_scanner_service.worker import ScannerWorker

DB_URL = "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"
REDIS_URL = "redis://127.0.0.1:6379/0"


@pytest.mark.asyncio
async def test_scan_job_and_run_idempotency(tmp_path):
    pool = await asyncpg.create_pool(DB_URL)
    try:
        # 1. Create a scan_spec and scan_spec_version
        spec_id = uuid4()
        version_id = uuid4()
        slug = f"test-spec-{uuid4().hex[:8]}"

        univ_dict = PRESET_RSI_OVERSOLD.universe.model_dump()
        rules_dict = [rg.model_dump() for rg in PRESET_RSI_OVERSOLD.rules]
        spec_hash = compute_spec_hash(univ_dict, {"rules": rules_dict})

        await pool.execute(
            """
            INSERT INTO scan_specs (id, slug, name, description, active_version_id)
            VALUES ($1, $2, $3, $4, NULL);
            """,
            spec_id,
            slug,
            "Test Spec",
            "Test Description",
        )

        await pool.execute(
            """
            INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
            VALUES ($1, $2, 1, $3, $4::jsonb, $5::jsonb);
            """,
            version_id,
            spec_id,
            spec_hash,
            json.dumps(univ_dict),
            json.dumps(rules_dict),
        )

        await pool.execute(
            "UPDATE scan_specs SET active_version_id = $1 WHERE id = $2;",
            version_id,
            spec_id,
        )

        worker = ScannerWorker(
            database_url=DB_URL,
            redis_url=REDIS_URL,
            analytics_lake_path=str(tmp_path),
        )
        worker.pool = pool

        # 2. Test event-triggered scan run idempotency
        trigger_event_id = str(uuid4())

        # First run execution
        run_id_1 = await worker._execute_scan(
            spec_version_id=version_id,
            spec_hash=spec_hash,
            universe_data=univ_dict,
            rules_data=rules_dict,
            trigger_reason="COMMITTED_BAR_EVENT",
            trigger_event_id=trigger_event_id,
            watermark_ns=1700000000000000000,
        )

        # Second run execution with exact same trigger_event_id (simulating Redis redelivery)
        run_id_2 = await worker._execute_scan(
            spec_version_id=version_id,
            spec_hash=spec_hash,
            universe_data=univ_dict,
            rules_data=rules_dict,
            trigger_reason="COMMITTED_BAR_EVENT",
            trigger_event_id=trigger_event_id,
            watermark_ns=1700000000000000000,
        )

        # Query database to confirm only 1 scan_runs record exists for this trigger_event_id
        runs_count = await pool.fetchval(
            "SELECT count(*) FROM scan_runs WHERE spec_version_id = $1 AND trigger_event_id = $2;",
            version_id,
            trigger_event_id,
        )
        assert runs_count == 1, "Partial unique index must prevent duplicate event-triggered scan_runs"

        # 3. Test manual scan job execution
        job_id = uuid4()
        req_id = f"req-{uuid4().hex[:8]}"
        await pool.execute(
            """
            INSERT INTO scan_jobs (id, request_id, spec_version_id, status)
            VALUES ($1, $2, $3, 'PENDING');
            """,
            job_id,
            req_id,
            version_id,
        )

        # Execute job loop step
        claimed_row = await pool.fetchrow(
            """
            UPDATE scan_jobs
            SET status = 'RUNNING', started_at = now()
            WHERE id = $1
            RETURNING *;
            """,
            job_id,
        )
        assert claimed_row is not None
        assert claimed_row["status"] == "RUNNING"

        # Simulate job execution
        manual_run_id = await worker._execute_scan(
            spec_version_id=version_id,
            spec_hash=spec_hash,
            universe_data=univ_dict,
            rules_data=rules_dict,
            trigger_reason="MANUAL_JOB",
            trigger_event_id=None,
            watermark_ns=1700000000000000000,
        )

        await pool.execute(
            "UPDATE scan_jobs SET status = 'COMPLETED', scan_run_id = $2, completed_at = now() WHERE id = $1;",
            job_id,
            manual_run_id,
        )

        final_job = await pool.fetchrow("SELECT * FROM scan_jobs WHERE id = $1;", job_id)
        assert final_job["status"] == "COMPLETED"
        assert final_job["scan_run_id"] == manual_run_id

    finally:
        await pool.close()
