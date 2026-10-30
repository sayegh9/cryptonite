"""Prove the real exit-debate worker persists its decision as cryptonite_api."""

import asyncio
import importlib.util
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from connections import DB_URL
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import FLASH, PRO
from migrated_db import migrated_database
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

ROOT = Path(__file__).resolve().parents[1]
WORKER_DIR = ROOT / "services" / "debate_worker"
sys.path.insert(0, str(WORKER_DIR))
INSTRUMENT = "BTCUSDT.BINANCE"


def _api_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"cryptonite_api:api_dev_pass@{host}"))


def _ensure_api_role():
    """Postgres roles are cluster-wide; create the test role before migrations if this cluster lacks it."""
    async def ensure():
        conn = await asyncpg.connect(DB_URL)
        try:
            exists = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api')")
            if not exists:
                await conn.execute("CREATE ROLE cryptonite_api LOGIN PASSWORD 'api_dev_pass'")
        finally:
            await conn.close()

    asyncio.run(ensure())


def _load_worker():
    path = WORKER_DIR / "main.py"
    spec = importlib.util.spec_from_file_location(f"h1_debate_worker_{uuid.uuid4().hex}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def db_url():
    _ensure_api_role()
    with migrated_database("cryptonite_h1_exit") as url:
        yield url


@pytest.fixture
async def pools(db_url):
    owner = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    api = await asyncpg.create_pool(_api_url(db_url), min_size=1, max_size=2)
    try:
        await owner.execute("TRUNCATE debate_turns, portfolio_decisions, exit_decisions, debate_runs CASCADE")
        yield owner, api
    finally:
        await owner.execute("TRUNCATE debate_turns, portfolio_decisions, exit_decisions, debate_runs CASCADE")
        await api.close()
        await owner.close()


def _exit_model():
    verdict = {
        "recommendation": "HOLD",
        "conviction_score": 0.8,
        "target_stop_price": 50000.0,
        "reasoning": "Keep the position and preserve the existing stop.",
    }

    def respond(_messages, info: AgentInfo):
        if not info.output_tools:
            return ModelResponse(parts=[TextPart("The position can remain open." )])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, verdict)])

    return FunctionModel(respond)


@pytest.mark.asyncio
async def test_exit_debate_worker_persists_as_cryptonite_api(pools, monkeypatch):
    owner, api = pools
    worker = _load_worker()
    run_id = uuid.uuid4()
    await owner.execute(
        """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, claim_gen)
           VALUES ($1, $2, 'RUNNING', 'exit_monitor', 0)""",
        run_id, INSTRUMENT,
    )

    async def position_context(_self, _instrument_id):
        return {"current_position": {"instrument_id": INSTRUMENT, "quantity": 0.25}}

    monkeypatch.setattr(worker.AntiCorruptionLayer, "get_instrument_context", position_context)
    model = _exit_model()
    redis = SimpleNamespace(publish=AsyncMock(return_value=1))

    with override_model(FLASH, model), override_model(PRO, model):
        await worker.process_exit_debate_trigger(
            api, redis, {"instrument_id": INSTRUMENT, "run_id": str(run_id)}, Gateway(),
        )

    assert await api.fetchval("SELECT current_user") == "cryptonite_api"
    decision = await api.fetchrow(
        "SELECT recommendation, target_stop_price, reasoning FROM exit_decisions WHERE run_id = $1", run_id,
    )
    assert decision is not None
    assert decision["recommendation"] == "HOLD"
    assert decision["target_stop_price"] == 50000
    assert "existing stop" in decision["reasoning"]
