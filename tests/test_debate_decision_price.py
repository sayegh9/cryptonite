"""Bounded, declared decision price (review-plan B5, F-M14): the entry debate prices from the
orchestrator's all-market quote source only — the shared ui_quote_tick stream of Binance book
tickers (same feed as its quote cache): the latest same-instrument tick, mid of a finite positive
bid/ask no older than DECISION_PRICE_MAX_AGE_S. Stale/missing/invalid/wrong-symbol quotes end the
attempt as NO_PRICE with no model call and no order; the worker makes zero outbound HTTP (the direct
api.binance.com fallback is gone). Throwaway DB at Alembic head; FunctionModels; FakeRedis only."""

import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import pytest
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import EMBED, FLASH, FLASH_LITE, PRO
from cryptonite_schemas.marks import DECISION_PRICE_MAX_AGE_S
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


def quote_double(entries: list, brief: str | None = "MARKET BRIEF BTCUSDT") -> AsyncMock:
    """Worker Redis double serving exactly these quote-stream entries (no ticker fallback data)."""
    redis = AsyncMock()
    redis.get.side_effect = lambda key: brief if str(key) == "brief:k" else None
    redis.xrevrange.return_value = entries
    return redis


def scanner_payload() -> dict:
    close_ns = int((time.time() - 60) * 1e9)
    candidate = {"timeframe": "1h", "bar_close_inclusive_ns": close_ns}
    return {"instrument_id": INSTRUMENT, "trigger_source": "scanner", "brief_key": "brief:k",
            "candidate_payload": json.dumps(candidate)}


def live_tick(bid: str, ask: str, age_s: float, instrument: str = INSTRUMENT) -> list:
    """One stream entry in the real ``{"data": <json>}`` envelope the ingest writes."""
    now_ns = time.time_ns()
    ui_record = {"instrument_id": instrument, "bid_price": bid, "ask_price": ask,
                 "bid_size": "1.0", "ask_size": "1.0",
                 "arrival_ns": str(now_ns - int(age_s * 1e9))}
    return [("1700000000000-0", {"data": json.dumps(ui_record)})]


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


async def run_entry(debate_main, pool, redis, calls: dict, payload: dict | None = None,
                    message_ref: str | None = None, consumer: str | None = None):
    model = counting_model(calls)
    with override_model(FLASH, model), override_model(FLASH_LITE, model), override_model(PRO, model), \
         override_model(EMBED, FakeEmbedder()):
        await debate_main.process_debate_trigger(pool, redis, payload or scanner_payload(), Gateway(),
                                                 message_ref=message_ref, consumer=consumer)
    return await pool.fetchrow("SELECT * FROM debate_runs WHERE instrument_id = $1 ORDER BY created_at DESC LIMIT 1", INSTRUMENT)


async def test_fresh_quote_debates_and_declares_the_price(debate_main, pool, monkeypatch):
    """A fresh same-symbol quote runs the debate and the stored decision price equals the tick mid —
    with no open position on the instrument (the first-entry regression: paper:marks is empty)."""
    import httpx

    def no_http(*args, **kwargs):
        raise AssertionError("worker must make zero outbound HTTP")

    monkeypatch.setattr(httpx, "AsyncClient", no_http)
    assert await pool.fetchval("SELECT count(*) FROM paper_positions") == 0
    calls = {"n": 0}
    run = await run_entry(debate_main, pool, quote_double(live_tick("65000", "65020", 2.0)), calls)
    assert calls["n"] > 0 and run["status"] == "PENDING_APPROVAL"
    decision = await pool.fetchrow("SELECT * FROM portfolio_decisions WHERE run_id = $1", run["id"])
    assert float(decision["decision_price"]) == pytest.approx(65010.0)


async def test_no_binance_fallback_remains_in_the_worker():
    """Regression for the prior fallback: no direct price HTTP from the debate worker survives."""
    source = MAIN.read_text()
    assert "api.binance.com" not in source
    assert "httpx" not in source
    assert "market:24h" not in source


@pytest.mark.parametrize("raw_ticks", [
    [],  # missing: empty stream
    live_tick("65000", "65020", DECISION_PRICE_MAX_AGE_S + 20.0),  # stale
    [("1700000000000-0", {"data": "not-json"})],  # malformed envelope
    [("1700000000000-0", {"data": json.dumps({"junk": "1"})})],  # malformed tick
    live_tick("-5", "-4", 1.0),  # non-positive quote
    live_tick("3000", "3010", 1.0, instrument="ETHUSDT.BINANCE"),  # wrong symbol
])
async def test_unusable_quote_is_no_price_without_side_effects(debate_main, pool, raw_ticks):
    calls = {"n": 0}
    redis = quote_double(list(raw_ticks))
    run = await run_entry(debate_main, pool, redis, calls)
    # A terminal, durable NO_PRICE outcome: identity, trace, completion time and reason recorded…
    assert run["status"] == "NO_PRICE" and run["failure_reason"] == "NO_PRICE"
    assert run["completed_at"] is not None and run["decision_trace_id"] is not None
    # …with no model call, no decision and no order behind it…
    assert calls["n"] == 0  # no model call, not even triage: the price resolves before the gate
    assert await pool.fetchval("SELECT count(*) FROM portfolio_decisions") == 0
    assert await pool.fetchval("SELECT count(*) FROM debate_runs") == 1
    set_keys = [str(c.args[0]) for c in redis.set.await_args_list or [] if c.args]
    assert not [k for k in set_keys if k.startswith("debate:cooldown:")]  # no cooldown ever taken


async def test_no_price_redelivery_is_idempotent(debate_main, pool, monkeypatch):
    """The same queued message twice records one NO_PRICE row; the redelivery never re-debates."""
    from unittest.mock import AsyncMock as _AsyncMock
    monkeypatch.setattr(debate_main, "is_pel_owner", _AsyncMock(return_value=True))
    ref = "stream:debate_queue/900-0"
    payload = {**scanner_payload(),
               "candidate_payload": json.dumps({"timeframe": "1h",
                                                "bar_close_inclusive_ns": int((time.time() - 60) * 1e9),
                                                "candidate_id": str(uuid.uuid4())})}
    calls = {"n": 0}
    redis = quote_double([])
    first = await run_entry(debate_main, pool, redis, calls, payload, message_ref=ref, consumer="debate-w1")
    assert first["status"] == "NO_PRICE" and first["stream_message_id"] == ref
    second = await run_entry(debate_main, pool, redis, calls, payload, message_ref=ref, consumer="debate-w1")
    assert calls["n"] == 0
    assert await pool.fetchval("SELECT count(*) FROM debate_runs") == 1
    assert second["id"] == first["id"] and second["status"] == "NO_PRICE"


async def test_no_price_refused_without_pel_ownership(debate_main, pool, monkeypatch):
    """A stale queued attempt records nothing: the NO_PRICE insert is owner-guarded too."""
    from unittest.mock import AsyncMock as _AsyncMock
    monkeypatch.setattr(debate_main, "is_pel_owner", _AsyncMock(return_value=False))
    calls = {"n": 0}
    run = await run_entry(debate_main, pool, quote_double([]), calls,
                          message_ref="stream:debate_queue/901-0", consumer="debate-stale")
    assert run is None and calls["n"] == 0
    assert await pool.fetchval("SELECT count(*) FROM debate_runs") == 0


async def test_retry_after_no_price_passes_once_quotes_flow(debate_main, pool):
    """A later attempt with quotes flowing debates normally (direct calls carry no dedupe identity)."""
    calls = {"n": 0}
    redis = quote_double([])
    first = await run_entry(debate_main, pool, redis, calls)
    assert calls["n"] == 0 and first["status"] == "NO_PRICE"
    redis.xrevrange.return_value = live_tick("65090", "65110", 1.0)
    run = await run_entry(debate_main, pool, redis, calls)
    assert calls["n"] > 0 and run["status"] == "PENDING_APPROVAL"
    assert float((await pool.fetchrow("SELECT decision_price FROM portfolio_decisions WHERE run_id = $1",
                                      run["id"]))["decision_price"]) == pytest.approx(65100.0)


async def test_boundary_age_is_accepted(debate_main, pool):
    """A quote just inside the bound still prices the debate."""
    calls = {"n": 0}
    run = await run_entry(debate_main, pool, quote_double(live_tick("64890", "64910", DECISION_PRICE_MAX_AGE_S - 1.0)), calls)
    assert run is not None and run["status"] == "PENDING_APPROVAL"


async def test_stale_quote_at_decision_time_ends_no_price(debate_main, pool, monkeypatch):
    """A quote fresh at debate start but expired by finalization ends terminal NO_PRICE: no decision,
    no route — the persisted price is never minutes stale."""
    fresh, stale = live_tick("65000", "65020", 1.0), live_tick("65000", "65020", DECISION_PRICE_MAX_AGE_S + 30.0)
    redis = quote_double(fresh)
    redis.xrevrange.side_effect = [fresh, stale]  # price-time read, then the finalization re-read
    routed = {"n": 0}
    original_route = debate_main.route_entry

    async def counting_route(*args, **kwargs):
        routed["n"] += 1
        return await original_route(*args, **kwargs)

    monkeypatch.setattr(debate_main, "route_entry", counting_route)
    calls = {"n": 0}
    run = await run_entry(debate_main, pool, redis, calls)
    assert calls["n"] > 0  # the debate itself ran on the fresh quote
    assert routed["n"] == 0  # but nothing routed
    row = await pool.fetchrow("SELECT status, failure_reason, completed_at FROM debate_runs WHERE id = $1", run["id"])
    assert (row["status"], row["failure_reason"]) == ("NO_PRICE", "NO_PRICE")
    assert row["completed_at"] is not None
    assert await pool.fetchval("SELECT count(*) FROM portfolio_decisions WHERE run_id = $1", run["id"]) == 0


async def test_final_quote_is_the_persisted_price(debate_main, pool):
    """The decision-time quote — not the debate-start one — is stored and used for mandate room."""
    first, second = live_tick("65000", "65020", 1.0), live_tick("65200", "65220", 1.0)
    redis = quote_double(first)
    redis.xrevrange.side_effect = [first, second]
    calls = {"n": 0}
    run = await run_entry(debate_main, pool, redis, calls)
    assert run["status"] == "PENDING_APPROVAL"
    assert float((await pool.fetchrow("SELECT decision_price FROM portfolio_decisions WHERE run_id = $1",
                                      run["id"]))["decision_price"]) == pytest.approx(65210.0)


def test_parse_quote_tick_accepts_bytes_envelope():
    """The ingest envelope parses with bytes keys/values too (no decode_responses)."""
    from cryptonite_schemas.marks import parse_quote_tick
    now_ns = time.time_ns()
    ui = {"instrument_id": INSTRUMENT, "bid_price": "65000", "ask_price": "65020",
          "bid_size": "1.0", "ask_size": "1.0", "arrival_ns": str(now_ns - 1_000_000_000)}
    tick = parse_quote_tick({b"data": json.dumps(ui).encode()})
    assert tick is not None and tick[0] == INSTRUMENT and tick[3] == int(ui["arrival_ns"])
    assert parse_quote_tick({"data": "not-json"}) is None


async def test_high_volume_stream_still_finds_deep_tick(debate_main, pool):
    """Retention-complete scan: the target tick buried under 1100 newer other-symbol ticks is still
    found — no scan-window false NO_PRICE. A stream without the symbol stays NO_PRICE."""
    now_ns = time.time_ns()

    def other(i):
        ui = {"instrument_id": f"SYM{i % 40}.BINANCE",
              "bid_price": "10", "ask_price": "11", "bid_size": "1", "ask_size": "1",
              "arrival_ns": str(now_ns - 1_000_000_000)}
        return (f"170000000{i:04d}-0", {"data": json.dumps(ui)})

    deep = live_tick("65000", "65020", 2.0)  # oldest entry: last in reverse order
    calls = {"n": 0}
    run = await run_entry(debate_main, pool, quote_double([other(i) for i in range(1100)] + deep), calls)
    assert run["status"] == "PENDING_APPROVAL"
    assert float((await pool.fetchrow("SELECT decision_price FROM portfolio_decisions WHERE run_id = $1",
                                      run["id"]))["decision_price"]) == pytest.approx(65010.0)

    absent = await run_entry(debate_main, pool, quote_double([other(i) for i in range(1100)]), {"n": 0})
    assert absent["status"] == "NO_PRICE"


async def test_missing_instrument_id_is_refused(debate_main, pool):
    """No fallback symbol: a trigger without instrument_id records and debates nothing."""
    calls = {"n": 0}
    model = counting_model(calls)
    with override_model(FLASH, model), override_model(FLASH_LITE, model), override_model(PRO, model), \
         override_model(EMBED, FakeEmbedder()):
        await debate_main.process_debate_trigger(pool, quote_double(live_tick("65000", "65020", 1.0)),
                                                 {"trigger_source": "test"}, Gateway())
    assert calls["n"] == 0
    assert await pool.fetchval("SELECT count(*) FROM debate_runs") == 0


async def test_exit_debate_needs_no_decision_price(debate_main, pool):
    """Exit behavior preserved: the exit path never priced from the ticker and still runs on marks."""
    run_id = uuid.uuid4()
    await pool.execute(
        "INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at) VALUES ($1, $2, 'RUNNING', 'exit_monitor', now())",
        run_id, INSTRUMENT)
    await debate_main.process_exit_debate_trigger(
        pool, quote_double([]), {"instrument_id": INSTRUMENT, "run_id": str(run_id)}, Gateway())
    row = await pool.fetchrow("SELECT status FROM debate_runs WHERE id = $1", run_id)
    assert row["status"] == "NO_TRADE"  # no open position in the empty ACL view; priced nothing, fenced nothing
