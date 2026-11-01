"""Phase 2 Track B: funnel trace and the three shadow entry policies (A deterministic, B live, C narrow veto).

Unit tests use fake pools (no model is ever called: the veto persona is stubbed); the SQL tests run on the
throwaway CI database named by DATABASE_URL."""

import asyncio
import datetime as dt
import importlib.util
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from cryptonite_agents.debate_fleet import NARROW_VETO, VetoDecision
from cryptonite_agents.gateway.routes import DEFAULT_ROUTES
from cryptonite_execution.profile_log import record_decision

ROOT = Path(__file__).resolve().parents[1]
WORKER_DIR = ROOT / "services" / "debate_worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

import gate  # noqa: E402
import policies  # noqa: E402

LONG = {"entry": 100.0, "stop": 98.0, "target": 106.0}
SHORT = {"entry": 100.0, "stop": 102.0, "target": 94.0}
LONG_C = {"suggested_entry": 100.0, "suggested_stop": 98.0, "suggested_target": 106.0}  # the candidate payload
SHORT_C = {"suggested_entry": 100.0, "suggested_stop": 102.0, "suggested_target": 94.0}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeConn:
    def __init__(self, count: int = 0, insert_result=1):
        self.count, self.insert_result = count, insert_result
        self.calls: list[tuple[str, tuple]] = []

    @asynccontextmanager
    async def _tx(self):
        yield

    def transaction(self):
        return self._tx()

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self.count if "count(*)" in sql else self.insert_result

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "OK"

    def sql(self, needle: str) -> list[tuple]:
        return [args for sql, args in self.calls if needle in sql]


class FakePool:
    def __init__(self, conn: FakeConn | None = None, broken: bool = False):
        self.conn, self.broken = conn or FakeConn(), broken

    @asynccontextmanager
    async def acquire(self):
        if self.broken:
            raise ConnectionError("pool down")
        yield self.conn


# --- Policy A ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("grade,decision,size", [("A", "TAKE", 1.0), ("B", "TAKE", 1.0), ("C", "TAKE", 0.5),
                                                  ("NO_TRADE", "SKIP", None), (None, "SKIP", None)])
def test_policy_a_grade_rules(grade, decision, size):
    a = policies.policy_a(LONG_C, {"candidate": {**LONG, "reward_to_risk": 3.0}}, grade)
    assert (a.decision, a.size_factor, a.side) == (decision, size, "LONG")


def test_policy_a_hard_rejects_skip_whatever_the_grade():
    data = {"candidate": {**LONG, "reward_to_risk": 1.2}}
    assert gate._rule_rejection(data) == "TRIAGE_RULE_REWARD_TO_RISK"  # the gate's own rule on the same inputs
    a = policies.policy_a(LONG_C, data, "A")
    assert (a.decision, a.reason) == ("SKIP", "TRIAGE_RULE_REWARD_TO_RISK")


def test_policy_a_is_short_aware_and_deterministic():
    data = {"candidate": {**SHORT, "reward_to_risk": 3.0, "timeframe": "15m"},
            "timeframes": [{"timeframe": "4h", "trend": "down"}, {"timeframe": "1d", "trend": "down"}]}
    first = policies.policy_a(SHORT_C, data, "B")
    assert first == policies.policy_a(SHORT_C, data, "B")
    assert (first.decision, first.side) == ("TAKE", "SHORT")  # down trends are with a SHORT, not against it
    assert policies.policy_a({}, data, "A").reason == "NO_DIRECTION"


# --- Policy B ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status,rec,expected", [
    ("PENDING_APPROVAL", "BUY", ("TAKE", "LONG", 1.0)),
    ("COMPLETED", "SELL", ("TAKE", "SHORT", 1.0)),
    ("NO_TRADE", "HOLD", ("SKIP", None, None)),
    ("NO_TRADE", "BUY", ("SKIP", "LONG", None)),  # a route refusal
])
def test_policy_b_route_mapping(status, rec, expected):
    decision, side, size, reason = policies.policy_b_for_route(status, rec)
    assert (decision, side, size) == expected
    assert reason == ("DEBATE_HOLD" if rec == "HOLD" else f"ROUTED_{status}" if decision == "TAKE" else f"ROUTE_{status}")


@pytest.mark.parametrize("reason", ["TRIAGE_REJECTED", "TRIAGE_RULE_CHOP", "PROFILE_DEBATE_DAILY_CAP", "NO_PRICE"])
def test_policy_b_refusals_are_skips(reason):
    assert policies.policy_b_for_refusal(reason) == ("SKIP", None, None, reason)


def test_policy_b_failure_is_error_and_requires_policy_a():
    assert policies.policy_b_for_failure(RuntimeError("x"))[0] == "ERROR"
    conn = FakeConn()
    cid = uuid.uuid4()
    assert asyncio.run(policies.record_policy_b(FakePool(conn), cid, "TAKE", "LONG", 1.0, "ROUTED"))
    sql, args = conn.calls[0]
    assert "WHERE EXISTS" in sql and "policy = 'A'" in sql and args[:3] == (cid, "B", "TAKE")
    assert asyncio.run(policies.record_policy_b(FakePool(broken=True), cid, "SKIP")) is False  # never raises
    assert asyncio.run(policies.record_policy_b(FakePool(conn), None, "SKIP")) is False


# --- Policy C ---------------------------------------------------------------------------------------------

class _Veto:
    role = NARROW_VETO.role

    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, 0

    async def run(self, gateway, prompt, **kw):
        self.calls += 1
        assert kw["context"].mandate_id is None  # never a profile's AI budget
        return await self.behaviour()


async def _capture(pool, veto, monkeypatch, timeout=0.05):
    monkeypatch.setattr(policies, "NARROW_VETO", veto)
    monkeypatch.setattr(policies, "POLICY_C_TIMEOUT_S", timeout)
    cid = uuid.uuid4()
    # The live path awaits only policy A: it must come back at once even while the veto hangs
    await asyncio.wait_for(policies.capture_entry_policies(
        pool, object(), cid, "BTCUSDT.BINANCE", LONG_C, "brief", {"candidate": {**LONG, "reward_to_risk": 3},
                                                               "setup_grade": {"grade": "B"}}), 0.5)
    await asyncio.gather(*list(policies._C_TASKS))
    return cid


def _final_c(conn: FakeConn):
    updates = conn.sql("UPDATE policy_decisions")
    return updates[-1] if updates else None


@pytest.mark.parametrize("behaviour", ["hang", "raise"])
def test_policy_c_never_blocks_and_records_error(behaviour, monkeypatch):
    async def hang():
        await asyncio.Event().wait()

    async def boom():
        raise RuntimeError("model down")

    conn = FakeConn()
    veto = _Veto(hang if behaviour == "hang" else boom)
    asyncio.run(_capture(FakePool(conn), veto, monkeypatch))
    a_args = [a for a in conn.sql("INSERT INTO policy_decisions") if a[1] == "A"][0]
    assert a_args[2:5] == ("TAKE", "LONG", Decimal("1.0"))
    assert veto.calls == 1
    final = _final_c(conn)
    assert final[1:3] == ("C", "ERROR")
    assert ("TimeoutError" if behaviour == "hang" else "RuntimeError") in final[5]


def test_policy_c_records_take_and_red_flag_skip(monkeypatch):
    for flag, decision in (("NONE", "TAKE"), ("CROWDED_FUNDING_OI", "SKIP")):
        async def answer(flag=flag):
            return SimpleNamespace(output=VetoDecision(decision="TAKE" if flag == "NONE" else "SKIP", red_flag=flag,
                                                       reason="r"), model="google:gemini-2.5-flash",
                                   cost_usd=Decimal("0.0004"))
        conn = FakeConn()
        asyncio.run(_capture(FakePool(conn), _Veto(answer), monkeypatch))
        final = _final_c(conn)
        assert final[2] == decision and final[6] == "google:gemini-2.5-flash" and final[7] == Decimal("0.0004")


def test_policy_c_respects_daily_cap(monkeypatch):
    async def never():
        raise AssertionError("no model call at the cap")

    monkeypatch.setenv("POLICY_C_DAILY_CAP", "3")
    conn = FakeConn(count=3)
    veto = _Veto(never)
    asyncio.run(_capture(FakePool(conn), veto, monkeypatch))
    assert veto.calls == 0
    assert not [a for a in conn.sql("INSERT INTO policy_decisions") if a[1] == "C"]
    monkeypatch.setenv("POLICY_C_DAILY_CAP", "nonsense")
    assert policies.policy_c_daily_cap() == policies.DEFAULT_POLICY_C_DAILY_CAP


def test_policy_c_not_started_on_redelivery_or_broken_pool(monkeypatch):
    async def never():
        raise AssertionError("no model call")

    veto = _Veto(never)
    asyncio.run(_capture(FakePool(FakeConn(insert_result=None)), veto, monkeypatch))  # A row already there
    asyncio.run(_capture(FakePool(broken=True), veto, monkeypatch))  # capture never raises
    assert veto.calls == 0


def test_policy_veto_route_and_persona():
    route = DEFAULT_ROUTES["policy.veto"]
    assert route.primary_model.endswith("gemini-2.5-flash") and route.max_output_tokens == 300
    assert route.priority == "optional" and route.daily_cap_usd is not None
    assert NARROW_VETO.output_type is VetoDecision and NARROW_VETO.role == "policy.veto"


# --- B1: GATE rows carry candidate_id ---------------------------------------------------------------------

def test_record_decision_writes_candidate_id_column():
    conn, cid, mandate = FakeConn(), uuid.uuid4(), uuid.uuid4()
    assert asyncio.run(record_decision(conn, mandate, "GATE", "REFUSED", "COOLDOWN", candidate_id=str(cid)))
    sql, args = conn.calls[0]
    assert "candidate_id" in sql and args[-1] == cid and "candidate_id" not in args[5]
    assert asyncio.run(record_decision(conn, mandate, "GATE", "REFUSED", "x", candidate_id="not-a-uuid"))
    assert "candidate_id" not in conn.calls[1][0]  # malformed id dropped, the row itself still written


def test_pre_debate_gate_passes_candidate_id(monkeypatch):
    cid, mandate, seen = uuid.uuid4(), uuid.uuid4(), {}

    async def resolve(conn, instrument, mandate_id):
        return SimpleNamespace(id=mandate)

    async def evaluate(*a, **kw):
        return gate.GateDecision(False, "COOLDOWN")

    async def record(conn, *args, **kw):
        seen.update(kw)
        return True

    async def count(*a):
        return None

    monkeypatch.setattr(gate, "resolve_profile", resolve)
    monkeypatch.setattr(gate, "_evaluate", evaluate)
    monkeypatch.setattr(gate, "record_decision", record)
    monkeypatch.setattr(gate, "_count", count)
    payload = {"instrument_id": "BTCUSDT.BINANCE", "candidate_payload": json.dumps({"candidate_id": str(cid)})}
    asyncio.run(gate.pre_debate_gate(FakePool(), None, payload, None, "brief"))
    assert seen["candidate_id"] == cid
    assert gate.payload_candidate_id({"candidate_payload": "{bad"}) is None


def test_worker_gate_refusal_passes_candidate_id(monkeypatch):
    main = _load("debate_worker_main_p2b", WORKER_DIR / "main.py")
    cid, mandate, seen = uuid.uuid4(), uuid.uuid4(), {}

    async def record(conn, *args, **kw):
        seen.update(kw, stage=args[1])
        return True

    monkeypatch.setattr(main, "record_decision", record)
    asyncio.run(main.record_gate_refusal(FakePool(), None, "BTCUSDT.BINANCE", "scanner", "COOLDOWN",
                                         str(uuid.uuid4()), None, None, candidate_id=cid,
                                         gate=SimpleNamespace(profile=SimpleNamespace(id=mandate))))
    assert seen["stage"] == "GATE" and seen["candidate_id"] == cid


# --- SQL on the CI database -------------------------------------------------------------------------------

DB_URL = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif("cryptonite_ci" not in DB_URL, reason="needs the throwaway CI database")


@needs_db
def test_policy_sql_runs_on_the_schema():
    async def go():
        conn = await asyncpg.connect(DB_URL)
        try:
            # B/C without an A row insert nothing (and so never hit the candidate FK)
            assert await policies.record_policy(conn, uuid.uuid4(), "B", "SKIP", require_a=True) is False
            assert await conn.fetchval(policies._C_TODAY) >= 0
        finally:
            await conn.close()
    asyncio.run(go())


@needs_db
def test_funnel_report_renders_markdown():
    report = _load("funnel_report_p2b", ROOT / "scripts" / "funnel_report.py")
    today = dt.datetime.now(dt.UTC).date()
    text = asyncio.run(report.report(DB_URL, today - dt.timedelta(days=30), today + dt.timedelta(days=1)))
    assert "## Stages" in text and "## Cooldown impact" in text and "## By hour_utc" in text
    assert report.classify({"run_id": 1, "status": "GATE_REFUSED", "failure_reason": "COOLDOWN"}) == (0, "COOLDOWN")
    assert report.classify({"run_id": 1, "status": "PENDING_APPROVAL", "recommendation": "BUY",
                            "command_ids": [1], "filled": True}) == (6, None)


@needs_db
def test_recall_lessons_fallback_excludes_hold():
    main = _load("debate_worker_main_p2b_lessons", WORKER_DIR / "main.py")

    class NoEmbed:
        async def embed_tagged(self, *a, **kw):
            raise RuntimeError("embedder down")

    async def go():
        pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
        marker = f"p2b HOLD lesson {uuid.uuid4()}"
        try:
            async with pool.acquire() as conn:
                run = await conn.fetchval("INSERT INTO debate_runs (instrument_id, trigger_source) "
                                          "VALUES ('BTCUSDT.BINANCE', 'test') RETURNING id")
                await conn.execute("INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, "
                                   "conviction_score, allocation_pct, reasoning) "
                                   "VALUES ($1, 'BTCUSDT.BINANCE', 'HOLD', 0.5, 0, 'x')", run)
                await conn.execute("INSERT INTO reflections (run_id, actual_outcome_pnl, alpha_vs_benchmark, "
                                   "lessons_learned) VALUES ($1, 0, 0, $2)", run, marker)
            assert marker not in await main.recall_lessons(pool, NoEmbed(), "brief")
        finally:
            await pool.close()
    asyncio.run(go())
