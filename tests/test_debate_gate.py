"""Phase 2 debate cost controls (blueprint A2.1, A2.3, A2.4): pre-debate gate, triage, stakes-sized
lite debate with escalation, NO_TRADE for HOLD, merged risk panel. Throwaway DB at Alembic head;
models are FunctionModels (which also count the calls each role makes)."""

import asyncio
import importlib.util
import json
import sys
import time
import uuid
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import EMBED, FLASH, FLASH_LITE, PRO
from cryptonite_schemas.marks import encode_mark
from migrated_db import migrated_database, seed_paper_account
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
        from cryptonite_schemas.marks import quote_tick_key
        if str(key) == quote_tick_key(INSTRUMENT):
            now_ns = time.time_ns()
            return json.dumps({"instrument_id": INSTRUMENT, "bid_price": "65000", "ask_price": "65020",
                               "bid_size": "1.0", "ask_size": "1.0",
                               "arrival_ns": str(now_ns - 2_000_000_000)})
        return self.kv.get(key)

    async def hgetall(self, key):
        if key == "paper:marks":
            now_ns = time.time_ns()
            return {INSTRUMENT: encode_mark(Decimal("65000.0"), now_ns - 2_000_000_000, now_ns)}
        return {}

    async def xrevrange(self, key, *args, **kwargs):
        return []  # B5 prices from the per-instrument snapshot now; the stream is never consulted

    async def incr(self, key):
        self.kv[key] = int(self.kv.get(key, 0)) + 1

    async def delete(self, key):
        self.kv.pop(key, None)

    async def eval(self, script, numkeys, *args):
        """Emulate token-checked lease renewal and release without changing ownership."""
        key, token = args[0], args[1]
        if self.kv.get(key) == token:
            if "'EXPIRE'" in script:
                return 1  # This double has no clock; renewal preserves the live token.
            del self.kv[key]
            return 1
        return 0

    expire = publish = AsyncMock()


def scanner_payload(age_s: float = 60, timeframe: str = "1h", brief: bool = True,
                    candidate_id: str | None = None) -> dict:
    close_ns = int((time.time() - age_s) * 1e9)
    candidate = {"timeframe": timeframe, "bar_close_inclusive_ns": close_ns}
    if candidate_id:
        candidate["candidate_id"] = candidate_id
    return {"instrument_id": INSTRUMENT, "trigger_source": "scanner", "brief_key": "brief:k" if brief else "",
            "candidate_payload": json.dumps(candidate)}


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


async def test_a_failure_before_the_durable_run_leaves_the_retry_passable(main, pool, monkeypatch):
    """D1 review blocker 1: nothing durable exists before the debate_runs row, so a transient failure
    there must not poison the same message's retry (a pre-run dedupe key did exactly that)."""
    cid = str(uuid.uuid4())
    payload = scanner_payload(candidate_id=cid)
    models = Models()
    redis = Redis("MARKET BRIEF BTCUSDT")
    original = main.AntiCorruptionLayer.get_instrument_context
    attempts = {"n": 0}

    async def flaky(self, instrument_id):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient failure before the run row")
        return await original(self, instrument_id)

    monkeypatch.setattr(main.AntiCorruptionLayer, "get_instrument_context", flaky)
    with override_model(FLASH_LITE, models.model()):
        with pytest.raises(RuntimeError, match="transient"):
            await main.process_debate_trigger(pool, redis, payload, Gateway())
    assert await pool.fetchval("SELECT count(*) FROM debate_runs") == 0
    assert not any(k.startswith("debate:cooldown:") for k in redis.kv)  # released for the retry

    run_row = await run(main, pool, payload, models, redis)  # the same message, redelivered
    assert (run_row["status"], str(run_row["candidate_id"])) == ("PENDING_APPROVAL", cid)


async def test_a_cancelled_attempt_before_the_run_releases_its_window(main, pool, monkeypatch):
    """D1 review: asyncio cancellation is not an Exception, so the pre-run cleanup must catch it too —
    a cancelled attempt between gate and run INSERT releases the cooldown it holds, and the
    redelivery passes the gate again."""
    cid = str(uuid.uuid4())
    payload = scanner_payload(candidate_id=cid)
    models = Models()
    redis = Redis("MARKET BRIEF BTCUSDT")
    original = main.AntiCorruptionLayer.get_instrument_context
    attempts = {"n": 0}

    async def cancelled_once(self, instrument_id):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise asyncio.CancelledError()
        return await original(self, instrument_id)

    monkeypatch.setattr(main.AntiCorruptionLayer, "get_instrument_context", cancelled_once)
    with override_model(FLASH_LITE, models.model()):
        with pytest.raises(asyncio.CancelledError):
            await main.process_debate_trigger(pool, redis, payload, Gateway())
    assert await pool.fetchval("SELECT count(*) FROM debate_runs") == 0
    assert not any(k.startswith("debate:cooldown:") for k in redis.kv)  # released despite the cancellation

    run_row = await run(main, pool, payload, models, redis)
    assert (run_row["status"], str(run_row["candidate_id"])) == ("PENDING_APPROVAL", cid)


async def test_a_crashed_attempt_leaves_a_window_only_its_own_candidate_passes(gate, pool):
    """D1 review: a hard crash after the gate but before the run row leaves the cooldown key behind (no
    cleanup runs at all). Its value is the candidate's own token: the same candidate's redelivery still
    passes (no durable row exists), while every other candidate waits out the window."""
    redis = Redis()
    cid = str(uuid.uuid4())
    crashed = await gate.pre_debate_gate(pool, redis, scanner_payload(candidate_id=cid), None, "MARKET BRIEF")
    assert crashed.allowed  # the process dies here: no run row, key still set
    same = await gate.pre_debate_gate(pool, redis, scanner_payload(candidate_id=cid), None, "MARKET BRIEF")
    other = await gate.pre_debate_gate(pool, redis, scanner_payload(candidate_id=str(uuid.uuid4())),
                                       None, "MARKET BRIEF")
    assert (same.allowed, other.reason) == (True, "COOLDOWN")
    assert redis.kv.get(crashed.cooldown_key) == f"cid:{cid}"  # still holding the window it owns


async def test_a_stale_owner_never_deletes_another_candidates_window(gate, pool):
    """D1 review: release_cooldown is a single Lua compare-and-delete, so if the window changes hands
    (TTL expiry, then the next candidate's SET) before the failing attempt releases it, the stale owner
    cannot delete the new owner's window."""
    redis = Redis()
    cid1, cid2 = str(uuid.uuid4()), str(uuid.uuid4())
    first = await gate.pre_debate_gate(pool, redis, scanner_payload(candidate_id=cid1), None, "MARKET BRIEF")
    assert first.allowed
    redis.kv[first.cooldown_key] = f"cid:{cid2}"  # the window expired and candidate 2 took it over
    await gate.release_cooldown(redis, first.cooldown_key, first.cooldown_token)
    assert redis.kv[first.cooldown_key] == f"cid:{cid2}"  # the new owner's window survives
    second = await gate.pre_debate_gate(pool, redis, scanner_payload(candidate_id=cid2), None, "MARKET BRIEF")
    blocked = await gate.pre_debate_gate(pool, redis, scanner_payload(candidate_id=str(uuid.uuid4())),
                                         None, "MARKET BRIEF")
    assert (second.allowed, blocked.reason) == (True, "COOLDOWN")


async def test_a_second_delivery_after_the_durable_run_is_dropped(main, pool, gate):
    """D1 review blocker 1: the outbox is at-least-once — a second XADD of the same candidate must never
    create a second durable decision (the run row's candidate_id is the dedupe)."""
    cid = str(uuid.uuid4())
    payload = scanner_payload(candidate_id=cid)
    models = Models()
    first = await run(main, pool, payload, models)
    assert (first["status"], str(first["candidate_id"])) == ("PENDING_APPROVAL", cid)
    spend = Counter(models.calls)

    decision = await gate.pre_debate_gate(pool, Redis(), payload, None, "MARKET BRIEF")
    assert decision.reason == "DUPLICATE_CANDIDATE"
    second = await run(main, pool, payload, models)
    assert second["id"] == first["id"] and models.calls == spend
    assert await pool.fetchval("SELECT count(*) FROM portfolio_decisions") == 1


async def test_a_racing_delivery_that_passes_the_gate_cannot_create_a_second_run(main, pool, monkeypatch):
    """D1: the partial unique index backstops two deliveries that race past the gate — the second insert
    ends the message with no model call and no second durable decision."""
    cid = str(uuid.uuid4())
    payload = scanner_payload(candidate_id=cid)
    models = Models()
    first = await run(main, pool, payload, models)
    spend = Counter(models.calls)
    monkeypatch.setattr(main, "pre_debate_gate",
                        AsyncMock(return_value=SimpleNamespace(allowed=True, cooldown_key=None, cooldown_token=None)))
    await main.process_debate_trigger(pool, Redis("MARKET BRIEF BTCUSDT"), payload, Gateway())
    assert await pool.fetchval("SELECT count(*) FROM debate_runs") == 1
    assert await pool.fetchval("SELECT count(*) FROM portfolio_decisions") == 1
    assert models.calls == spend
    assert first["status"] == "PENDING_APPROVAL"


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


# --- Brain-started debates (WS-1E): the account checks only, no triage -----------------------------

BRAIN = {"instrument_id": INSTRUMENT, "trigger_source": "brain_on_demand"}


async def _seed_account_state(db_url: str, state: str) -> None:
    """One account-level refusal on a seeded paper account (seeded the way the paper trading tests seed it)."""
    admin = await asyncpg.connect(db_url)
    try:
        await seed_paper_account(admin)
        if state == "KILL_SWITCH_LATCHED":
            await admin.execute(
                "INSERT INTO execution_kill_switch (environment, account_id, is_latched) "
                "VALUES ('PAPER', 'PAPER-ACCOUNT-001', true)"
            )
        elif state == "DAILY_LOSS_LATCHED":
            await admin.execute(
                "UPDATE paper_accounts SET daily_loss_latched = true WHERE account_id = 'PAPER-ACCOUNT-001'"
            )
        elif state == "POSITION_AT_CAP":  # 0.5 BTC at 50,000 on 100,000 equity is 25%, above the 24% headroom
            await admin.execute(
                """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
                       quantity, current_price, status)
                   VALUES ($1, 'PAPER-ACCOUNT-001', 'MANUAL_TRADE', $2, 'LONG', 50000, 0.5, 50000, 'OPEN')""",
                uuid.uuid4().hex, INSTRUMENT,
            )
    finally:
        await admin.close()


@pytest.fixture
async def seeded_account(pool, db_url):
    """Clears the positions and instances a test seeds, so the pool fixture can delete the paper account."""
    yield
    admin = await asyncpg.connect(db_url)
    await admin.execute("TRUNCATE paper_positions, paper_strategy_instances CASCADE")
    await admin.close()


@pytest.mark.parametrize("state", ["KILL_SWITCH_LATCHED", "DAILY_LOSS_LATCHED", "POSITION_AT_CAP"])
async def test_brain_debates_are_refused_by_the_account_checks_without_a_model_call(
        gate, pool, db_url, seeded_account, state):
    await _seed_account_state(db_url, state)
    models = Models()
    with override_model(FLASH_LITE, models.model()):
        decision = await gate.pre_debate_gate(pool, Redis(), BRAIN, Gateway(), "MARKET BRIEF")
    assert (decision.allowed, decision.reason) == (False, state)
    assert sum(models.calls.values()) == 0


async def test_scanner_candidates_are_refused_at_the_position_cap(gate, pool, db_url, seeded_account):
    await _seed_account_state(db_url, "POSITION_AT_CAP")
    decision = await gate.pre_debate_gate(pool, Redis(), scanner_payload(), None, "MARKET BRIEF")
    assert decision.reason == "POSITION_AT_CAP"


async def test_brain_debate_passes_the_account_checks_and_never_asks_triage(gate, pool):
    models = Models(triage=False)  # a triage call would refuse: the Brain path must not make one
    with override_model(FLASH_LITE, models.model()):
        decision = await gate.pre_debate_gate(pool, Redis(), BRAIN, Gateway(), None)
    assert (decision.allowed, decision.reason) == (True, "BRAIN_TRIGGER")
    assert sum(models.calls.values()) == 0
    assert decision.cooldown_key is None  # no cooldown is taken, so a failure has nothing to release


async def test_brain_debates_skip_the_scanner_dedupe_staleness_and_cooldown(main, gate, pool):
    cid = str(uuid.uuid4())
    # 1970 is stale for a scanner candidate, and this candidate already has a durable run
    candidate = {"candidate_id": cid, "timeframe": "1h", "bar_close_inclusive_ns": 1}
    payload = {**BRAIN, "candidate_payload": json.dumps(candidate)}
    redis = Redis("MARKET BRIEF BTCUSDT")
    assert (await run(main, pool, payload, Models(), redis))["candidate_id"] == uuid.UUID(cid)
    decision = await gate.pre_debate_gate(pool, redis, payload, None, None)
    assert decision.reason == "BRAIN_TRIGGER"
    assert not any(k.startswith("debate:cooldown:") for k in redis.kv)


async def test_brain_debate_runs_the_full_fleet_not_the_lite_path(main, pool):
    """A Brain debate has no market brief and the lite persona has no tools, so it never gets a lite call."""
    models = Models()
    run_row = await run(main, pool, BRAIN, models)
    assert run_row["trigger_source"] == "brain_on_demand"
    assert models.calls["lite"] == 0 and models.calls["triage"] == 0
    assert models.calls["pm"] == 1 and models.calls["cro"] == 1


async def test_brain_debate_with_the_kill_switch_latched_is_recorded_as_gate_refused_without_a_model_call(main, pool):
    await pool.execute(
        "INSERT INTO execution_kill_switch (environment, account_id, is_latched) "
        "VALUES ('PAPER', 'PAPER-ACCOUNT-001', true)"
    )
    models = Models()
    row = await run(main, pool, BRAIN, models)
    assert (row["status"], row["failure_reason"]) == ("GATE_REFUSED", "KILL_SWITCH_LATCHED")
    assert sum(models.calls.values()) == 0


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
    # 2 analysts (no On-Chain Analyst, R3.2) + opening + rebuttal (two turns) + (no risk managers) = 4 text calls; PM + merged CRO
    assert (models.calls["text"], models.calls["pm"], models.calls["cro"]) == (4, 1, 1)



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


# --- Rule triage (R3.1): hard rules in code before the model -------------------------------------

def brief_data(rr=2.5, t15="up", t1h="up", vol15=1.3, bands15="above"):
    return {"timeframes": [{"timeframe": "5m", "trend": "up", "volume_ratio": 1.0, "bands": "inside"},
                           {"timeframe": "15m", "trend": t15, "volume_ratio": vol15, "bands": bands15},
                           {"timeframe": "1h", "trend": t1h, "volume_ratio": 1.0, "bands": "inside"}],
            "candidate": {"timeframe": "15m", "reward_to_risk": rr}}


@pytest.mark.parametrize("data, reason", [
    (brief_data(rr=1.2), "TRIAGE_RULE_REWARD_TO_RISK"),
    (brief_data(t15="down", t1h="down"), "TRIAGE_RULE_TIMEFRAMES_AGAINST"),
    (brief_data(vol15=0.6, bands15="inside"), "TRIAGE_RULE_CHOP"),
])
async def test_rule_triage_rejects_without_a_model_call(gate, pool, data, reason):
    models = Models()
    redis = Redis()
    with override_model(FLASH_LITE, models.model()):
        decision = await gate.pre_debate_gate(pool, redis, scanner_payload(timeframe="15m"), Gateway(),
                                              "MARKET BRIEF", brief_data=data)
    assert (decision.allowed, decision.reason) == (False, reason)
    assert sum(models.calls.values()) == 0
    # H4: the free rule checks run before the cooldown is taken, so a rule rejection leaves the window open
    assert not any(k.startswith("debate:cooldown:") for k in redis.kv)
    assert any(k.endswith(f":{reason}") and v == 1 for k, v in redis.kv.items())  # counted like other rejections


@pytest.mark.parametrize("data", [
    None,  # no structured brief (scanner older than the debate worker)
    {"timeframes": [], "candidate": None},
    brief_data(rr=None),
    {"timeframes": [{"timeframe": "1h", "trend": "down"}], "candidate": {"timeframe": "15m"}},
    {"timeframes": [{"timeframe": "15m", "trend": "unknown"}, {"timeframe": "1h", "trend": "down"}],
     "candidate": {"timeframe": "15m", "reward_to_risk": 2.0}},
    "not a dict",
])
async def test_missing_rule_inputs_fall_through_to_the_model(gate, pool, data):
    models = Models(triage=True)
    with override_model(FLASH_LITE, models.model()):
        decision = await gate.pre_debate_gate(pool, Redis(), scanner_payload(timeframe="15m"), Gateway(),
                                              "MARKET BRIEF", brief_data=data)
    assert decision.reason == "TRIAGE_ACCEPTED"
    assert models.calls == Counter({"triage": 1})


async def test_worker_reads_the_scanner_brief_data_and_skips_the_model(main, pool):
    redis = Redis("MARKET BRIEF BTCUSDT")
    redis.kv["brief:k:data"] = json.dumps(brief_data(rr=0.9))
    models = Models()
    assert await run(main, pool, scanner_payload(timeframe="15m"), models, redis) is None
    assert sum(models.calls.values()) == 0


async def test_worker_with_clean_brief_data_still_asks_the_model(main, pool):
    redis = Redis("MARKET BRIEF BTCUSDT")
    redis.kv["brief:k:data"] = json.dumps(brief_data())
    models = Models(triage=False)
    assert await run(main, pool, scanner_payload(timeframe="15m"), models, redis) is None
    assert models.calls == Counter({"triage": 1})


# --- R3.2 / R7.4 / R7.5 ---------------------------------------------------------------------------

def test_full_debate_analysts_have_no_onchain_analyst(main):
    from cryptonite_agents import debate_fleet
    assert main.FULL_DEBATE_ANALYSTS == (debate_fleet.MARKET_ANALYST, debate_fleet.SENTIMENT_ANALYST)
    assert not hasattr(debate_fleet, "ONCHAIN_ANALYST")
    assert "NO on-chain data feed" in debate_fleet.ONCHAIN_CHAT.instructions


async def test_decision_trace_id_follows_the_candidate_into_the_run(main, pool):
    trace = "7d4f3f2e-9a0b-4c1d-8e2f-3a4b5c6d7e8f"
    payload = scanner_payload()
    payload["candidate_payload"] = json.dumps({**json.loads(payload["candidate_payload"]), "decision_trace_id": trace})
    run_row = await run(main, pool, payload, Models())
    assert str(run_row["decision_trace_id"]) == trace


async def test_operator_debate_gets_its_own_trace_id(main, pool):
    run_row = await run(main, pool, {"instrument_id": INSTRUMENT, "trigger_source": "user_on_demand"}, Models())
    assert run_row["decision_trace_id"] is not None


async def test_sentiment_last_ok_is_published_after_a_tagging_success(main, monkeypatch):
    from cryptonite_agents import news
    redis = Redis()
    monkeypatch.setattr(news, "_last_tag_ok", [])
    await main.publish_sentiment_ok(redis)
    assert "sentiment:last_ok" not in redis.kv  # unknown until the service has answered once
    monkeypatch.setattr(news, "_last_tag_ok", [1_790_000_000.5])
    await main.publish_sentiment_ok(redis)
    assert redis.kv["sentiment:last_ok"] == 1_790_000_000
