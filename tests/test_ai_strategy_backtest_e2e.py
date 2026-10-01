"""Model-written strategy end to end (blueprint Phase 4): API validation -> backtest run -> worker
claims it -> sandbox job (serve loop in a thread) -> COMPLETED with results. Throwaway DB at Alembic head."""

from pathlib import Path

import asyncpg
import pytest
from cryptonite_api.main import app
from cryptonite_backtester_service.worker import BacktestWorker
from httpx import ASGITransport, AsyncClient
from migrated_db import migrated_database
from test_backtest_sandbox import serving
from test_strategy_code_validation import GOOD
from test_warmup_no_execution import _setup_warmup_test_catalog


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_aistrattest") as url:
        yield url


async def test_generated_strategy_is_validated_saved_and_backtested_in_the_sandbox(tmp_path: Path, db_url, operator_auth):
    cat_dir, art_dir = tmp_path / "nautilus_catalog", tmp_path / "artifacts"
    inst, bar_type, warmup, start, end = _setup_warmup_test_catalog(cat_dir)
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    app.state.pool = pool
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            bad = await client.post("/api/v1/strategies", json={"name": "Evil", "description": "x", "python_code": GOOD + "\nimport os\n"})
            assert bad.status_code == 422 and bad.json()["detail"]["error"] == "STRATEGY_CODE_REJECTED"

            await pool.execute("INSERT INTO strategies (id, name, slug, description, author) VALUES (gen_random_uuid(), "
                               "'EMA Crossover Strategy', 'ema-cross', 'core', 'Cryptonite Core') ON CONFLICT (slug) DO NOTHING")
            core = await client.post("/api/v1/strategies", json={"name": "EMA Crossover Strategy", "description": "x", "python_code": GOOD})
            assert core.status_code == 409  # generated code never becomes a version of a core strategy

            saved = await client.post("/api/v1/strategies", json={"name": "AI EMA Demo", "description": "gen", "python_code": GOOD})
            assert saved.status_code in (200, 201), saved.text
            slug = saved.json()["slug"]

            run = await client.post("/api/v1/backtests", json={
                "strategy_slug": slug, "instrument_id": inst, "bar_type": bar_type, "timeframe": "1m",
                "parameters": {"period": 5}, "warmup_start_ns": warmup, "evaluation_start_ns": start,
                "evaluation_end_ns": end, "execution_profile": "optimistic",
            })
            assert run.status_code == 201, run.text
            run_id = run.json()["id"]

            worker = BacktestWorker(database_url=db_url, catalog_path=str(cat_dir), artifacts_path=str(art_dir), worker_id="ai-e2e")
            worker.pool = pool
            worker.sandbox_jobs_path = tmp_path / "jobs"
            claimed = await worker._claim_run()
            assert str(claimed["id"]) == run_id
            with serving(worker.sandbox_jobs_path, cat_dir):
                await worker._execute_claimed_run(claimed)

            info = (await client.get(f"/api/v1/backtests/{run_id}")).json()
            assert info["status"] == "COMPLETED", info
            metrics = (await client.get(f"/api/v1/backtests/{run_id}/results")).json()["metrics"]
            assert metrics["total_trades"] > 0
            assert (art_dir / run_id / "trades.parquet").exists()  # sandbox artifacts were copied and promoted
    finally:
        await pool.close()
