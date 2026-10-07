"""Bounded, declared decision price (review-plan B5, F-M14): the entry debate prices from the
orchestrator's live marks only — a finite positive same-symbol quote no older than
DECISION_PRICE_MAX_AGE_S. Stale/missing/invalid/wrong-symbol quotes end the attempt as NO_PRICE
with no run row, no model call and no order; the worker makes zero outbound HTTP (the direct
api.binance.com fallback is gone). Throwaway DB at Alembic head; FunctionModels; FakeRedis only."""

import importlib.util
import json
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import pytest
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import EMBED, FLASH, FLASH_LITE, PRO
from cryptonite_schemas.marks import DECISION_PRICE_MAX_AGE_S, encode_mark
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
    spec = importlib.util.spec_from_file_location("debate_worker_main_b5", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_b5_price") as url:
        yield url


@pytest.fixture
async def pool(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    yield pool
    await pool.execute("TRUNCATE debate_turns, portfolio_decisions, exit_decisions, debate_runs CASCADE")
    await pool.close()


def marks_double(raw_marks: dict, brief: str | None = "MARKET BRIEF BTCUSDT") -> AsyncMock:
    """Worker Redis double serving exactly these live marks (and no 24h-ticker fallback data)."""
    redis = AsyncMock()
    redis.get.side_effect = lambda key: brief if str(key) == "brief:k" else None
    redis.hgetall.return_value = raw_marks
    return redis


def scanner_payload() -> dict:
    close_ns = int((time.time() - 60) * 1e9)
    candidate = {"timeframe": "1h", "bar_close_inclusive_ns": close_ns}
    return {"instrument_id": INSTRUMENT, "trigger_source": "scanner", "brief_key": "brief:k",
            "candidate_payload": json.dumps(candidate)}


def live_mark(price: str, age_s: float) -> dict:
    now_ns = time.time_ns()
    return {INSTRUMENT: encode_mark(Decimal(price), now_ns - int(age_s * 1e9), now_ns)}


class FakeEmbedder:
    async def embed_texts(self, texts):
        return [[0.0] * 768 for _ in texts], 1


def counting_model(calls: dict) -> FunctionModel:
    def fn(messages, info: AgentInfo):
        calls["n"] += 1
        if not info.output_tools:
            return ModelResponse(parts=[TextPart("argument")])
        tool = info.output_tools[0]
        props = tool.parameters_json_schema.get("properties", {})
        if "worth_debate" in props:
            args = {"worth_debate": True, "confidence": 0.8, "reason": "t"}
        elif "bull_case" in props:  # lite verdict schema
            args = {"bull_case": "b", "bear_case": "r", "recommendation": "BUY",
                    "conviction_score": 0.8, "allocation_pct": 8.0, "reasoning": "small"}
        else:
            key = next(k for k in VERDICTS if k in props)
            args = VERDICTS[key]
        return ModelResponse(parts=[ToolCallPart(tool.name, args)])

    return FunctionModel(fn)


async def run_entry(debate_main, pool, redis, calls: dict, payload: dict | None = None):
    model = counting_model(calls)
    with override_model(FLASH, model), override_model(FLASH_LITE, model), override_model(PRO, model), \
         override_model(EMBED, FakeEmbedder()):
        await debate_main.process_debate_trigger(pool, redis, payload or scanner_payload(), Gateway())
    return await pool.fetchrow("SELECT * FROM debate_runs WHERE instrument_id = $1", INSTRUMENT)


async def test_fresh_quote_debates_and_declares_the_price(debate_main, pool, monkeypatch):
    """A fresh same-symbol quote runs the debate and the stored decision price equals the quote."""
    import httpx

    def no_http(*args, **kwargs):
        raise AssertionError("worker must make zero outbound HTTP")

    monkeypatch.setattr(httpx, "AsyncClient", no_http)
    calls = {"n": 0}
    run = await run_entry(debate_main, pool, marks_double(live_mark("65000.5", 2.0)), calls)
    assert calls["n"] > 0 and run["status"] == "PENDING_APPROVAL"
    decision = await pool.fetchrow("SELECT * FROM portfolio_decisions WHERE run_id = $1", run["id"])
    assert float(decision["decision_price"]) == pytest.approx(65000.5)


async def test_no_binance_fallback_remains_in_the_worker():
    """Regression for the prior fallback: no direct price HTTP from the debate worker survives."""
    source = MAIN.read_text()
    assert "api.binance.com" not in source
    assert "httpx" not in source
    assert "market:24h" not in source


@pytest.mark.parametrize("raw_marks", [
    {},  # missing
    live_mark("65000.0", DECISION_PRICE_MAX_AGE_S + 20.0),  # stale
    {INSTRUMENT: "not-json"},  # invalid payload
    {INSTRUMENT: encode_mark(Decimal("-5"), 1, 1)},  # non-positive
    {"ETHUSDT.BINANCE": encode_mark(Decimal("3000"), time.time_ns() - 1_000_000_000, time.time_ns())},  # wrong symbol
])
async def test_unusable_quote_is_no_price_without_side_effects(debate_main, pool, raw_marks):
    calls = {"n": 0}
    redis = marks_double(dict(raw_marks))
    run = await run_entry(debate_main, pool, redis, calls)
    assert run is None  # no run row opened
    assert calls["n"] == 0  # no model call, not even triage: the price resolves before the gate
    assert await pool.fetchval("SELECT count(*) FROM portfolio_decisions") == 0  # no order input staged
    assert await pool.fetchval("SELECT count(*) FROM debate_runs") == 0
    set_keys = [str(c.args[0]) for c in redis.set.await_args_list or [] if c.args]
    assert not [k for k in set_keys if k.startswith("debate:cooldown:")]  # no cooldown ever taken


async def test_retry_after_no_price_passes_once_quotes_flow(debate_main, pool):
    """No cooldown was ever taken, so the retry passes the gate once quotes flow again."""
    calls = {"n": 0}
    redis = marks_double({})
    await run_entry(debate_main, pool, redis, calls)
    assert calls["n"] == 0
    redis.hgetall.return_value = live_mark("65100.0", 1.0)
    run = await run_entry(debate_main, pool, redis, calls)
    assert calls["n"] > 0 and run["status"] == "PENDING_APPROVAL"
    assert float((await pool.fetchrow("SELECT decision_price FROM portfolio_decisions WHERE run_id = $1",
                                      run["id"]))["decision_price"]) == pytest.approx(65100.0)


async def test_boundary_age_is_accepted(debate_main, pool):
    """A quote just inside the bound still prices the debate."""
    calls = {"n": 0}
    run = await run_entry(debate_main, pool, marks_double(live_mark("64900.0", DECISION_PRICE_MAX_AGE_S - 1.0)), calls)
    assert run is not None and run["status"] == "PENDING_APPROVAL"


async def test_exit_debate_needs_no_decision_price(debate_main, pool):
    """Exit behavior preserved: the exit path never priced from the ticker and still runs on marks."""
    run_id = uuid.uuid4()
    await pool.execute(
        "INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at) VALUES ($1, $2, 'RUNNING', 'exit_monitor', now())",
        run_id, INSTRUMENT)
    await debate_main.process_exit_debate_trigger(
        pool, marks_double({}), {"instrument_id": INSTRUMENT, "run_id": str(run_id)}, Gateway())
    row = await pool.fetchrow("SELECT status FROM debate_runs WHERE id = $1", run_id)
    assert row["status"] == "NO_TRADE"  # no open position in the empty ACL view; priced nothing, fenced nothing
