"""Debate worker on the AI Gateway (blueprint A1.4): typed verdicts end to end, and an invalid
verdict fails the run closed instead of staging a HOLD decision. Throwaway DB at Alembic head;
models are pydantic-ai FunctionModels."""

import importlib.util
import sys
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import pytest
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import EMBED, FLASH, PRO
from migrated_db import migrated_database
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

MAIN = Path(__file__).resolve().parents[1] / "services" / "debate_worker" / "main.py"
sys.path.insert(0, str(MAIN.parent))  # main.py imports its sibling gate module
INSTRUMENT = "BTCUSDT.BINANCE"

VERDICTS = {
    "recommendation": {"recommendation": "BUY", "conviction_score": 0.72, "allocation_pct": 8.0, "reasoning": "trend"},
    "final_allocation_pct": {"final_allocation_pct": 6.0, "conviction_level": "Overweight", "risk_reasoning": "size down"},
}


@pytest.fixture(scope="module")
def debate_main():
    spec = importlib.util.spec_from_file_location("debate_worker_main_gw", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_debatetest") as url:
        yield url


@pytest.fixture
async def pool(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    yield pool
    await pool.execute("TRUNCATE debate_turns, portfolio_decisions, exit_decisions, debate_runs CASCADE")
    await pool.close()


def redis_double():
    redis = AsyncMock()
    redis.get.return_value = json.dumps({"last_price": 65000.0})
    return redis


class FakeEmbedder:
    async def embed_texts(self, texts):
        return [[0.0] * 768 for _ in texts], 1


def debate_model(pm_valid: bool = True) -> FunctionModel:
    """Free text for analysts/sides; the verdict schema's fields for PM/CRO output tools."""

    def fn(messages, info: AgentInfo):
        if not info.output_tools:
            return ModelResponse(parts=[TextPart("argument")])
        tool = info.output_tools[0]
        fields = tool.parameters_json_schema.get("properties", {})
        key = next(k for k in VERDICTS if k in fields)
        if key == "recommendation" and not pm_valid:
            return ModelResponse(parts=[ToolCallPart(tool.name, {"recommendation": "YOLO", "allocation_pct": 400})])
        return ModelResponse(parts=[ToolCallPart(tool.name, VERDICTS[key])])

    return FunctionModel(fn)


async def run_debate(debate_main, pool, pm_valid=True):
    model = debate_model(pm_valid)
    with override_model(FLASH, model), override_model(PRO, model), override_model(EMBED, FakeEmbedder()):
        await debate_main.process_debate_trigger(
            pool, redis_double(), {"instrument_id": INSTRUMENT, "trigger_source": "test"}, Gateway()
        )
    return await pool.fetchrow("SELECT * FROM debate_runs WHERE instrument_id = $1", INSTRUMENT)


async def test_debate_stages_the_typed_verdict_for_approval(debate_main, pool):
    run = await run_debate(debate_main, pool)
    assert run["status"] == "PENDING_APPROVAL"

    decision = await pool.fetchrow("SELECT * FROM portfolio_decisions WHERE run_id = $1", run["id"])
    assert decision["recommendation"] == "BUY"  # B2: normalized at write time (F-C2, F-M6)
    assert decision["conviction_level"] == "Overweight"  # the CRO's own wording, kept for sizing
    assert float(decision["allocation_pct"]) == 6.0  # CRO sizing overrides the PM's 8%
    assert float(decision["conviction_score"]) == pytest.approx(0.72)
    assert "trend" in decision["reasoning"] and "size down" in decision["reasoning"]

    roles = [r["agent_role"] for r in await pool.fetch("SELECT agent_role FROM debate_turns WHERE run_id = $1", run["id"])]
    assert {"Market Analyst", "Bull Researcher", "Bear Researcher", "Portfolio Manager", "Chief Risk Officer"} <= set(roles)
    pm_turn = await pool.fetchval(
        "SELECT content FROM debate_turns WHERE run_id = $1 AND agent_role = 'Portfolio Manager'", run["id"]
    )
    assert json.loads(pm_turn)["recommendation"] == "BUY"


async def test_invalid_verdict_fails_the_run_instead_of_defaulting_to_hold(debate_main, pool):
    run = await run_debate(debate_main, pool, pm_valid=False)
    assert run["status"] == "FAILED"
    assert await pool.fetchval("SELECT count(*) FROM portfolio_decisions WHERE run_id = $1", run["id"]) == 0


async def test_exit_debate_stages_a_typed_exit_decision(debate_main, pool, monkeypatch):
    async def context(self, instrument_id):
        return {"instrument_id": instrument_id, "current_position": {"size": 0.1, "unrealized_pnl_pct": -3.0}}

    monkeypatch.setattr(debate_main.AntiCorruptionLayer, "get_instrument_context", context)
    run_id = uuid.uuid4()
    await pool.execute(
        "INSERT INTO debate_runs (id, instrument_id, status, trigger_source) VALUES ($1, $2, 'RUNNING', 'test')",
        run_id, INSTRUMENT,
    )

    def fn(messages, info: AgentInfo):
        if not info.output_tools:
            return ModelResponse(parts=[TextPart("argument")])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "recommendation": "REDUCE", "conviction_score": 0.6, "target_stop_price": 61000.0, "reasoning": "weak",
        })])

    with override_model(FLASH, FunctionModel(fn)), override_model(PRO, FunctionModel(fn)):
        await debate_main.process_exit_debate_trigger(
            pool, redis_double(), {"instrument_id": INSTRUMENT, "run_id": str(run_id)}, Gateway()
        )

    row = await pool.fetchrow("SELECT * FROM exit_decisions WHERE run_id = $1", run_id)
    assert (row["recommendation"], float(row["target_stop_price"])) == ("REDUCE", 61000.0)
    assert await pool.fetchval("SELECT status FROM debate_runs WHERE id = $1", run_id) == "PENDING_APPROVAL"


async def test_debate_inside_an_l3_mandate_queues_its_order(debate_main, pool):
    """A5.7: no global execution mode; an L3 mandate that allows the instrument gets the order queued."""
    mid = await pool.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, autonomy_level)
           VALUES ($1, 10000, ARRAY['BTCUSDT.BINANCE'], 5, 3) RETURNING id""", f"l3-{uuid.uuid4().hex[:6]}")
    try:
        run = await run_debate(debate_main, pool)
        assert (run["status"], run["mandate_id"]) == ("COMPLETED", mid)
        cmd = await pool.fetchrow("SELECT * FROM execution_commands WHERE idempotency_key = $1", f"debate:{run['id']}")
        payload = json.loads(cmd["payload"])
        assert (cmd["command_type"], cmd["requester_id"], cmd["mandate_id"]) == ("SUBMIT_ORDER", "ai:debate_worker", mid)
        assert (payload["side"], payload["allocation_pct"]) == ("BUY", 5.0)  # the CRO's 6% capped at the mandate's 5%
    finally:
        await pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED' WHERE id = $1", mid)
