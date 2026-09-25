"""Phase 2 debate cost controls (blueprint A2.1, A2.3, A2.4): pre-debate gate, triage, stakes-sized
lite debate with escalation, NO_TRADE for HOLD, merged risk panel. Throwaway DB at Alembic head;
models are FunctionModels (which also count the calls each role makes)."""

import importlib.util
import json
import sys
import time
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import pytest
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import EMBED, FLASH, FLASH_LITE, PRO
from migrated_db import migrated_database
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

WORKER_DIR = Path(__file__).resolve().parents[1] / "services" / "debate_worker"
INSTRUMENT = "BTCUSDT.BINANCE"


def load(name: str, file: str):
    if str(WORKER_DIR) not in sys.path:
        sys.path.insert(0, str(WORKER_DIR))
    spec = importlib.util.spec_from_file_location(name, WORKER_DIR / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def main():
    return load("debate_worker_main_phase2", "main.py")


@pytest.fixture(scope="module")
def gate():
    return load("debate_worker_gate_phase2", "gate.py")


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_gatetest") as url:
        yield url


def as_api(url: str) -> str:
    """The debate worker's real role (cryptonite_api), so missing grants fail here too."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"cryptonite_api:api_dev_pass@{parts.netloc.rsplit('@', 1)[-1]}"))


@pytest.fixture
async def pool(db_url):
    admin = await asyncpg.connect(db_url)  # the api role cannot write the kill switch; tests seed via admin
    await admin.execute("GRANT INSERT, DELETE, TRUNCATE ON execution_kill_switch TO cryptonite_api")
    await admin.close()
    pool = await asyncpg.create_pool(as_api(db_url), min_size=1, max_size=4)
    yield pool
    await pool.close()
    admin = await asyncpg.connect(db_url)
    await admin.execute("TRUNCATE debate_turns, portfolio_decisions, debate_runs, execution_kill_switch CASCADE")
    await admin.execute("DELETE FROM paper_accounts")
    await admin.close()


class Redis:
    """Just enough Redis for the gate and worker: SET NX, GET, INCR, EXPIRE, PUBLISH."""

    def __init__(self, brief: str | None = None):
        self.kv = {"brief:k": brief} if brief else {}
        self.kv["market:24h:BTCUSDT"] = json.dumps({"last_price": 65000.0})

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def incr(self, key):
        self.kv[key] = int(self.kv.get(key, 0)) + 1

    async def delete(self, key):
        self.kv.pop(key, None)

    expire = publish = AsyncMock()


def scanner_payload(age_s: float = 60, timeframe: str = "1h", brief: bool = True) -> dict:
    close_ns = int((time.time() - age_s) * 1e9)
    return {"instrument_id": INSTRUMENT, "trigger_source": "scanner", "brief_key": "brief:k" if brief else "",
            "candidate_payload": json.dumps({"timeframe": timeframe, "bar_close_inclusive_ns": close_ns})}


class Models:
    """Scripted FunctionModel per model id; records which output contract each call asked for."""

    def __init__(self, triage=True, lite=None):
        self.calls = Counter()
        self.triage = triage
        self.lite = lite or {"bull_case": "b", "bear_case": "r", "recommendation": "BUY",
                             "conviction_score": 0.8, "allocation_pct": 3.0, "reasoning": "small"}

    def model(self) -> FunctionModel:
        def fn(messages, info: AgentInfo):
            if not info.output_tools:
                self.calls["text"] += 1
                return ModelResponse(parts=[TextPart("argument")])
            tool = info.output_tools[0]
            props = tool.parameters_json_schema.get("properties", {})
            if "worth_debate" in props:
                self.calls["triage"] += 1
                args = {"worth_debate": self.triage, "confidence": 0.8, "reason": "r"}
            elif "bull_case" in props:
                self.calls["lite"] += 1
                args = self.lite
            elif "final_allocation_pct" in props:
                self.calls["cro"] += 1
                args = {"final_allocation_pct": 6.0, "conviction_level": "Overweight", "risk_reasoning": "ok"}
            else:
                self.calls["pm"] += 1
                args = {"recommendation": "BUY", "conviction_score": 0.7, "allocation_pct": 8.0, "reasoning": "pm"}
            return ModelResponse(parts=[ToolCallPart(tool.name, args)])
        return FunctionModel(fn)


class FakeEmbedder:
    async def embed_texts(self, texts):
        return [[0.0] * 768 for _ in texts], 1


async def run(main, pool, payload, models, redis=None):
    m = models.model()
    with override_model(FLASH, m), override_model(PRO, m), override_model(FLASH_LITE, m), \
         override_model(EMBED, FakeEmbedder()):
        await main.process_debate_trigger(pool, redis or Redis("MARKET BRIEF BTCUSDT"), payload, Gateway())
    return await pool.fetchrow("SELECT * FROM debate_runs WHERE instrument_id = $1 ORDER BY created_at DESC LIMIT 1", INSTRUMENT)


# --- Gate (A2.1) ------------------------------------------------------------------------------

async def test_latched_kill_switch_skips_scanner_debates_without_any_model_call(main, pool):
    await pool.execute(
        "INSERT INTO execution_kill_switch (environment, account_id, is_latched) VALUES ('PAPER', 'PAPER-ACCOUNT-001', true)"
    )
    models = Models()
    assert await run(main, pool, scanner_payload(), models) is None
    assert sum(models.calls.values()) == 0


@pytest.mark.parametrize("payload, reason", [
    (scanner_payload(age_s=4 * 3600, timeframe="1h"), "STALE_CANDIDATE"),
    (scanner_payload(age_s=60), "TRIAGE_ACCEPTED"),
])
async def test_gate_reasons(gate, pool, payload, reason):
    m = Models().model()
    with override_model(FLASH_LITE, m):
        decision = await gate.pre_debate_gate(pool, Redis(), payload, Gateway(), "MARKET BRIEF")
    assert decision.reason == reason


async def test_cooldown_allows_one_debate_per_setup_window(gate, pool):
    redis = Redis()
    first = await gate.pre_debate_gate(pool, redis, scanner_payload(), None, "MARKET BRIEF")
    second = await gate.pre_debate_gate(pool, redis, scanner_payload(), None, "MARKET BRIEF")
    assert (first.allowed, second.reason) == (True, "COOLDOWN")


async def test_daily_loss_latch_skips(gate, pool, db_url):
    admin = await asyncpg.connect(db_url)
    await admin.execute(
        """INSERT INTO paper_accounts (account_id, venue, currency, starting_balance, cash_balance, total_equity,
               realized_pnl, daily_realized_pnl, daily_loss_latched, updated_at)
           VALUES ('PAPER-ACCOUNT-001', 'BINANCE', 'USDT', 100000, 100000, 100000, 0, 0, true, now())"""
    )
    await admin.close()
    assert (await gate.pre_debate_gate(pool, Redis(), scanner_payload(), None, None)).reason == "DAILY_LOSS_LATCHED"


async def test_operator_triggers_bypass_the_gate(gate, pool):
    await pool.execute(
        "INSERT INTO execution_kill_switch (environment, account_id, is_latched) VALUES ('PAPER', 'PAPER-ACCOUNT-001', true)"
    )
    decision = await gate.pre_debate_gate(pool, Redis(), {"instrument_id": INSTRUMENT, "trigger_source": "user_on_demand"}, None, None)
    assert (decision.allowed, decision.reason) == (True, "OPERATOR_TRIGGER")


# --- Triage (A2.3) and stakes-sized debates (A2.4) ----------------------------------------------

async def test_triage_rejection_costs_one_cheap_call(main, pool):
    models = Models(triage=False)
    assert await run(main, pool, scanner_payload(), models) is None
    assert models.calls == Counter({"triage": 1})


async def test_low_stakes_lite_verdict_is_staged_without_the_full_fleet(main, pool):
    models = Models()
    run_row = await run(main, pool, scanner_payload(), models)
    assert run_row["status"] == "PENDING_APPROVAL"
    assert models.calls == Counter({"triage": 1, "lite": 1})  # 2 calls instead of 12
    decision = await pool.fetchrow("SELECT * FROM portfolio_decisions WHERE run_id = $1", run_row["id"])
    assert (decision["recommendation"], float(decision["allocation_pct"])) == ("BUY", 3.0)


async def test_high_stakes_lite_verdict_escalates_to_the_full_debate(main, pool):
    models = Models(lite={"bull_case": "b", "bear_case": "r", "recommendation": "BUY",
                          "conviction_score": 0.8, "allocation_pct": 12.0, "reasoning": "big"})
    run_row = await run(main, pool, scanner_payload(), models)
    assert run_row["status"] == "PENDING_APPROVAL"
    assert models.calls["pm"] == 1 and models.calls["cro"] == 1


async def test_lite_hold_is_recorded_as_no_trade(main, pool):
    models = Models(lite={"bull_case": "b", "bear_case": "r", "recommendation": "HOLD",
                          "conviction_score": 0.5, "allocation_pct": 0.0, "reasoning": "chop"})
    run_row = await run(main, pool, scanner_payload(), models)
    assert run_row["status"] == "NO_TRADE"


async def test_merged_risk_panel_replaces_four_calls_with_one(main, pool, monkeypatch):
    monkeypatch.setattr(main, "RISK_PANEL_MODE", "merged")
    models = Models()
    await run(main, pool, {"instrument_id": INSTRUMENT, "trigger_source": "user_on_demand"}, models)
    # 3 analysts + bull/bear/bull + (no risk managers) = 6 text calls; PM + merged CRO
    assert (models.calls["text"], models.calls["pm"], models.calls["cro"]) == (6, 1, 1)



async def test_large_allocation_escalates_even_at_low_conviction(main, pool):
    models = Models(lite={"bull_case": "b", "bear_case": "r", "recommendation": "BUY",
                          "conviction_score": 0.34, "allocation_pct": 25.0, "reasoning": "big but unsure"})
    await run(main, pool, scanner_payload(), models)
    assert models.calls["pm"] == 1 and models.calls["cro"] == 1  # full review ran


async def test_weak_small_lite_verdict_is_no_trade(main, pool):
    models = Models(lite={"bull_case": "b", "bear_case": "r", "recommendation": "BUY",
                          "conviction_score": 0.2, "allocation_pct": 2.0, "reasoning": "meh"})
    run_row = await run(main, pool, scanner_payload(), models)
    assert run_row["status"] == "NO_TRADE" and models.calls["pm"] == 0


async def test_scanner_candidate_without_a_brief_is_skipped(gate, pool):
    decision = await gate.pre_debate_gate(pool, Redis(), scanner_payload(brief=False), Gateway(), None)
    assert decision.reason == "NO_BRIEF"


async def test_triage_outage_releases_the_cooldown(gate, pool):
    redis = Redis()  # no model override and no API key: the triage call fails
    first = await gate.pre_debate_gate(pool, redis, scanner_payload(), Gateway(), "MARKET BRIEF")
    assert first.reason == "TRIAGE_UNAVAILABLE"
    assert not any(k.startswith("debate:cooldown:") for k in redis.kv)
