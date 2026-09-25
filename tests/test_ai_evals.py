"""Eval harness (blueprint A1.6), offline: FunctionModels instead of Gemini, stub tools instead of
live data, results stored in a throwaway DB at Alembic head."""

import asyncpg
import pytest
from cryptonite_agents.evals import runner
from cryptonite_agents.gateway import Gateway, RiskClass
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.registry import ToolSpec
from cryptonite_agents.persona import Persona
from migrated_db import migrated_database
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

MODEL = "test:eval"
GOOD = {
    "recommendation": {"recommendation": "HOLD", "conviction_score": 0.4, "allocation_pct": 0.0, "reasoning": "r"},
    "final_allocation_pct": {"final_allocation_pct": 6.0, "conviction_level": "Hold", "risk_reasoning": "r"},
    "worth_debate": {"worth_debate": False, "confidence": 0.8, "reason": "chop"},
}


def scripted_model() -> FunctionModel:
    """Chat: mention funding. Tools: call every offered tool once, then answer. Structured: valid output."""

    def fn(messages, info: AgentInfo):
        if info.output_tools:
            tool = info.output_tools[0]
            props = tool.parameters_json_schema.get("properties", {})
            if "target_stop_price" in props:
                return ModelResponse(parts=[ToolCallPart(tool.name, {"recommendation": "CLOSE", "conviction_score": 0.7,
                                                                      "target_stop_price": None, "reasoning": "r"})])
            key = next(k for k in GOOD if k in props)
            return ModelResponse(parts=[ToolCallPart(tool.name, GOOD[key])])
        returned = any(isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, "parts", []))
        if info.function_tools and not returned:
            return ModelResponse(parts=[ToolCallPart(t.name, {"symbols": "BTCUSDT"}) for t in info.function_tools[:1]])
        return ModelResponse(parts=[TextPart("funding and drawdown and cross isolated downside open interest short")])

    return FunctionModel(fn)


def stub_market_data(symbols: str = "BTCUSDT") -> str:
    """Live 24h market data."""
    return '[{"symbol": "BTCUSDT", "last_price": 65000}]'


async def test_harness_scores_every_kind(monkeypatch):
    stub = ToolSpec("get_live_market_data", stub_market_data, RiskClass.READ, "Live 24h market data.")
    for name in ("research", "hunter", "explainer"):
        monkeypatch.setitem(runner.PERSONAS, name, Persona(name, "chat.research", "test", (stub,)))

    with override_model(MODEL, scripted_model()):
        results, metrics = await runner.run_set(Gateway(), "v1", MODEL)

    assert metrics["cases"] == 20 and metrics["errors"] == 0
    assert metrics["structured_valid_rate"] == 1.0
    by_id = {r.case_id: r for r in results}
    assert by_id["tool-1"].passed  # called get_live_market_data, then answered
    assert not by_id["tool-3"].passed  # required query_postgres_readonly was never called
    assert by_id["verdict-3"].checks == {"agrees": True}  # HOLD is the expected answer
    assert by_id["verdict-1"].checks == {"agrees": False}  # expected BUY
    assert by_id["triage-2"].passed and not by_id["triage-1"].passed


async def test_invalid_structured_output_counts_against_validity(monkeypatch):
    def fn(messages, info: AgentInfo):
        return ModelResponse(parts=[TextPart("I think maybe buy?")])

    with override_model(MODEL, FunctionModel(fn)):
        results, metrics = await runner.run_set(Gateway(), "v1", MODEL, kinds={"triage"})
    assert metrics["structured_valid_rate"] == 0.0 and metrics["errors"] == 5


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_evaltest") as url:
        yield url


async def test_runs_are_stored(db_url):
    with override_model(MODEL, scripted_model()):
        results, metrics = await runner.run_set(Gateway(), "v1", MODEL, kinds={"triage", "verdict"})
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        run_id = await runner.store_run(pool, "v1", MODEL, results, metrics, passed=True, notes="offline")
        assert await pool.fetchval("SELECT count(*) FROM ai_eval_results WHERE run_id = $1", run_id) == 10
        stored = await pool.fetchval("SELECT metrics->>'structured_valid_rate' FROM ai_eval_runs WHERE id = $1", run_id)
        assert float(stored) == 1.0
    finally:
        await pool.close()
