"""WS-3C (python): a Brain debate card follows its run to the verdict, and a safety-gate refusal of a Brain debate is
recorded instead of silently dropped.

The debate worker writes one terminal GATE_REFUSED run for a refused ``brain_on_demand`` trigger (never for a scanner
one). The card reads its run by the stream message id the Brain's tool returned, and ``sync_session_cards`` writes a
final state into the conversation once. Throwaway DB at Alembic head; the refusals make no model call, and the Redis
worker double serves only the live quote the worker reads first."""

import importlib.util
import json
import sys
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest
from cryptonite_agents.brain.widgets import describe_widget, widget_for_tool
from cryptonite_agents.gateway import Gateway
from cryptonite_api.brain_outcomes import (
    debate_line,
    debate_outcome,
    debate_patch,
    debate_read,
    followed_debates,
)
from cryptonite_api.main import create_app
from cryptonite_api.routes import brain as brain_routes
from cryptonite_api.routes.auth import COOKIE_NAME
from cryptonite_schemas.marks import quote_tick_key
from fastapi.testclient import TestClient
from migrated_db import migrated_database, seed_paper_account
from test_agent_worker_and_fleet import _generate_hmac_headers, worker_server
from test_brain_api import _valid_signature
from test_route_auth_guards import CSRF, OPERATOR_SID, _FakeAuthManager

MAIN = Path(__file__).resolve().parents[1] / "services" / "debate_worker" / "main.py"
sys.path.insert(0, str(MAIN.parent))  # main.py imports its sibling gate module
INSTRUMENT = "BTCUSDT.BINANCE"
REF = "1700000000000-0"  # the id the Brain's tool returns; the worker records it as "<stream>/<id>"
STREAM_REF = f"stream:debate_queue/{REF}"
RUN = uuid.UUID("5a6e0b7c-1111-4c7d-9e11-000000000001")


# --- The debate worker: a refused Brain debate is recorded as GATE_REFUSED ---------------------------------------

@pytest.fixture(scope="module")
def debate_main():
    spec = importlib.util.spec_from_file_location("debate_worker_main_ws3c", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ws3c_followthrough") as url:
        yield url


@pytest.fixture
async def pool(db_url):
    admin = await asyncpg.connect(db_url)
    await seed_paper_account(admin)
    await admin.close()
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    yield pool
    await pool.execute("TRUNCATE debate_turns, portfolio_decisions, debate_runs, execution_kill_switch CASCADE")
    await pool.execute("TRUNCATE paper_positions, paper_strategy_instances CASCADE")
    await pool.execute("DELETE FROM paper_accounts")
    await pool.close()


class QuoteRedis:
    """The worker's Redis: a live quote and the candidate's single-owner lease."""

    def __init__(self) -> None:
        now_ns = time.time_ns()
        self.kv = {quote_tick_key(INSTRUMENT): json.dumps({
            "instrument_id": INSTRUMENT, "bid_price": "65000", "ask_price": "65020", "bid_size": "1.0",
            "ask_size": "1.0", "arrival_ns": str(now_ns - 2_000_000_000)})}

    async def get(self, key):
        return self.kv.get(str(key))

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and str(key) in self.kv:
            return None
        self.kv[str(key)] = value
        return True

    async def eval(self, script, numkeys, key, token, *args):
        if self.kv.get(str(key)) != token:
            return 0
        if "EXPIRE" in script:
            return 1
        self.kv.pop(str(key), None)
        return 1


def brain_payload() -> dict:
    return {"instrument_id": INSTRUMENT, "trigger_source": "brain_on_demand", "timeframe": "1h"}


def scanner_payload() -> dict:
    candidate = {"timeframe": "1h", "candidate_id": str(uuid.uuid4()), "bar_close_inclusive_ns": time.time_ns()}
    return {"instrument_id": INSTRUMENT, "trigger_source": "scanner", "candidate_payload": json.dumps(candidate)}


async def latch_kill_switch(db_url: str) -> None:
    admin = await asyncpg.connect(db_url)
    await admin.execute(
        "INSERT INTO execution_kill_switch (environment, account_id, is_latched) "
        "VALUES ('PAPER', 'PAPER-ACCOUNT-001', true)")
    await admin.close()


async def run_trigger(debate_main, pool, payload: dict, consumer: str = "debate-w1", ref: str = STREAM_REF) -> None:
    await debate_main.process_debate_trigger(pool, QuoteRedis(), payload, Gateway(), message_ref=ref, consumer=consumer)


async def test_a_refused_brain_debate_is_recorded_as_gate_refused(debate_main, pool, db_url, monkeypatch):
    monkeypatch.setattr(debate_main, "is_pel_owner", AsyncMock(return_value=True))
    await latch_kill_switch(db_url)
    await run_trigger(debate_main, pool, brain_payload())
    row = await pool.fetchrow("SELECT * FROM debate_runs WHERE stream_message_id = $1", STREAM_REF)
    assert row["status"] == "GATE_REFUSED" and row["failure_reason"] == "KILL_SWITCH_LATCHED"
    assert row["trigger_source"] == "brain_on_demand" and row["completed_at"] is not None
    assert row["decision_trace_id"] is not None and row["instrument_id"] == INSTRUMENT
    assert await pool.fetchval("SELECT count(*) FROM portfolio_decisions") == 0  # no model, no decision


async def test_a_refused_scanner_candidate_records_no_run(debate_main, pool, db_url, monkeypatch):
    monkeypatch.setattr(debate_main, "is_pel_owner", AsyncMock(return_value=True))
    await latch_kill_switch(db_url)
    await run_trigger(debate_main, pool, scanner_payload(), ref="stream:debate_queue/1700000000001-0")
    assert await pool.fetchval("SELECT count(*) FROM debate_runs") == 0


async def test_a_redelivered_gate_refusal_is_recorded_once(debate_main, pool, db_url, monkeypatch):
    monkeypatch.setattr(debate_main, "is_pel_owner", AsyncMock(return_value=True))
    await latch_kill_switch(db_url)
    await run_trigger(debate_main, pool, brain_payload())
    await run_trigger(debate_main, pool, brain_payload())  # the same queued message, delivered again
    assert await pool.fetchval("SELECT count(*) FROM debate_runs WHERE stream_message_id = $1", STREAM_REF) == 1


async def test_a_second_refusal_write_for_the_same_message_is_a_no_op(debate_main, pool, monkeypatch):
    """The unique message ref backstops a racing redelivery: the second write hits it and changes nothing."""
    monkeypatch.setattr(debate_main, "is_pel_owner", AsyncMock(return_value=True))
    args = (pool, QuoteRedis(), INSTRUMENT, "brain_on_demand", "KILL_SWITCH_LATCHED", str(uuid.uuid4()), STREAM_REF,
            "debate-w1")
    await debate_main.record_gate_refusal(*args)
    await debate_main.record_gate_refusal(*args)  # must not raise
    rows = await pool.fetch("SELECT status, failure_reason FROM debate_runs WHERE stream_message_id = $1", STREAM_REF)
    assert [(r["status"], r["failure_reason"]) for r in rows] == [("GATE_REFUSED", "KILL_SWITCH_LATCHED")]


async def test_a_refusal_from_a_stale_attempt_records_nothing(debate_main, pool, db_url, monkeypatch):
    """A worker that no longer owns the message must not write a run that would block the owner's debate."""
    monkeypatch.setattr(debate_main, "is_pel_owner", AsyncMock(return_value=False))
    await latch_kill_switch(db_url)
    await run_trigger(debate_main, pool, brain_payload(), consumer="debate-stale")
    assert await pool.fetchval("SELECT count(*) FROM debate_runs") == 0


async def test_a_failed_refusal_write_never_raises_out_of_the_handler(debate_main, pool, db_url, monkeypatch):
    monkeypatch.setattr(debate_main, "is_pel_owner", AsyncMock(side_effect=RuntimeError("redis down")))
    await latch_kill_switch(db_url)
    await run_trigger(debate_main, pool, brain_payload())  # must return normally: the refusal stands
    assert await pool.fetchval("SELECT count(*) FROM debate_runs") == 0


# --- The Brain's card: the tool's message id, the widget, and its description -----------------------------------

def stage(content: str, **args):
    return widget_for_tool("stage_debate_proposal",
                           {"instrument_id": "btcusdt", "timeframe": "1h", "recommendation": "BUY",
                            "conviction_score": 0.7, "reasoning": "x", **args},
                           call_id="deb-1", status="OK", content=content)


def test_a_staged_debate_card_carries_the_message_id_it_follows():
    card = stage(f"Success: Proposal staged to Action Inbox (Msg ID: {REF})")
    assert card["status"] == "QUEUED" and card["message_ref"] == REF
    assert card["instrument_id"] == INSTRUMENT and card["run_id"] is None and card["verdict"] is None


def test_a_failed_stage_has_no_message_id():
    card = stage("Error staging proposal: Server returned 429 - cap reached")
    assert card["status"] == "FAILED" and card["message_ref"] is None


@pytest.mark.parametrize("content", ["Success: Proposal staged to Action Inbox (Msg ID: None)",
                                     "Success: Proposal staged to Action Inbox"])
def test_a_message_id_that_is_not_a_stream_id_is_not_followed(content):
    assert stage(content)["message_ref"] is None


def test_a_debate_card_for_an_invalid_instrument_is_no_widget():
    assert stage(f"Success: Proposal staged to Action Inbox (Msg ID: {REF})", instrument_id="DROP TABLE;") is None


def test_debate_card_descriptions_name_the_status_and_any_verdict():
    card = {"kind": "debate_summary", "id": "deb-1", "instrument_id": INSTRUMENT, "status": "QUEUED"}
    assert describe_widget(card) == f"[debate queued for {INSTRUMENT}]"
    done = {**card, "status": "VERDICT", "verdict": {"recommendation": "BUY", "conviction_score": 0.62,
                                                     "allocation_pct": 5.0, "approval_status": None}}
    assert describe_widget(done) == f"[debate verdict for {INSTRUMENT}, verdict BUY]"


# --- The read: a run found by its message id, mapped to the card's status -----------------------------------------

class _DebateConn:
    """The three reads debate_outcome makes: the run by its message id, its latest decision, its approval."""

    def __init__(self, run=None, decision=None, approval=None):
        self.run, self.decision, self.approval = run, decision, approval
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "FROM debate_runs" in sql:
            return self.run
        if "FROM portfolio_decisions" in sql:
            return self.decision
        if "FROM ai_approvals" in sql:
            return self.approval
        raise AssertionError(f"unexpected read: {sql}")


def _run(status: str, failure_reason: str | None = None):
    return {"id": RUN, "status": status, "failure_reason": failure_reason}


DECISION = {"recommendation": "BUY", "conviction_score": Decimal("0.62"), "allocation_pct": Decimal("5.00")}


def _verdict(approval_status: str | None) -> dict:
    return {"recommendation": "BUY", "conviction_score": 0.62, "allocation_pct": 5.0,
            "approval_status": approval_status}


@pytest.mark.parametrize("run, decision, approval, expected", [
    (None, None, None, {"status": "QUEUED", "run_id": None, "verdict": None, "reason": None}),
    (_run("RUNNING"), None, None, {"status": "RUNNING", "run_id": str(RUN), "verdict": None, "reason": None}),
    (_run("PENDING_APPROVAL"), DECISION, {"status": "PENDING"},
     {"status": "VERDICT", "run_id": str(RUN), "verdict": _verdict("PENDING"), "reason": None}),
    (_run("COMPLETED"), DECISION, None,
     {"status": "VERDICT", "run_id": str(RUN), "verdict": _verdict(None), "reason": None}),
    (_run("REJECTED"), DECISION, {"status": "REJECTED"},
     {"status": "VERDICT", "run_id": str(RUN), "verdict": _verdict("REJECTED"), "reason": None}),
    (_run("PENDING_APPROVAL"), None, None,  # a decided-looking run with no decision cannot be followed to a verdict
     {"status": "FAILED", "run_id": str(RUN), "verdict": None, "reason": "NO_DECISION"}),
    (_run("NO_TRADE"), None, None, {"status": "NO_TRADE", "run_id": str(RUN), "verdict": None, "reason": "NO_TRADE"}),
    (_run("NO_PRICE"), None, None, {"status": "NO_TRADE", "run_id": str(RUN), "verdict": None, "reason": "NO_PRICE"}),
    (_run("GATE_REFUSED", "DAILY_LOSS_LATCHED"), None, None,
     {"status": "GATE_REFUSED", "run_id": str(RUN), "verdict": None, "reason": "DAILY_LOSS_LATCHED"}),
    (_run("FAILED", "WORKER_LOST"), None, None,
     {"status": "FAILED", "run_id": str(RUN), "verdict": None, "reason": "WORKER_LOST"}),
])
async def test_each_run_status_maps_to_one_card_status(run, decision, approval, expected):
    conn = _DebateConn(run, decision, approval)
    assert await debate_outcome(conn, REF) == expected


async def test_the_run_is_found_by_the_bare_id_the_tool_returned():
    conn = _DebateConn(run=None)
    await debate_outcome(conn, REF)
    assert conn.calls[0][1] == (REF, STREAM_REF)  # the worker records "<stream>/<id>"; the tool returns "<id>"


async def test_the_approval_is_the_debate_approval_of_this_run():
    conn = _DebateConn(_run("PENDING_APPROVAL"), DECISION, {"status": "APPROVED"})
    await debate_outcome(conn, REF)
    approval_call = next(args for sql, args in conn.calls if "FROM ai_approvals" in sql)
    assert approval_call == (str(RUN),)  # approve writes source='debate', source_ref=<run id>


# --- The route: read only, and the conversation's own sync --------------------------------------------------------

class _DebateDB:
    def __init__(self, conn: _DebateConn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _card(**extra) -> dict:
    return {"id": "deb-1", "kind": "debate_summary", "instrument_id": INSTRUMENT, "timeframe": "1h",
            "status": "QUEUED", "message_ref": REF, "message": f"Success: Proposal staged (Msg ID: {REF})", **extra}


@pytest.fixture
def debate_worker(monkeypatch):
    """A worker holding one session with one debate card. Notes honour expect_status like the real store; with
    ``flags["reject_notes"]`` they answer 422, as the worker does for a status it does not accept."""
    card = _card()
    notes: list[dict] = []
    flags = {"reject_notes": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not _valid_signature(request):
            return httpx.Response(401)
        if request.method == "GET" and "/sessions/" in request.url.path:
            return httpx.Response(200, json={"session": {}, "messages": [{"role": "assistant", "widgets": [card]}]})
        if "/notes" in request.url.path:
            body = json.loads(request.content)
            notes.append(body)
            if flags["reject_notes"]:
                return httpx.Response(422, json={"detail": "status not accepted"})
            if body.get("expect_status") and card["status"] != body["expect_status"]:
                return httpx.Response(409, json={"detail": "not queued"})
            if body.get("status"):
                card["status"] = body["status"]
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(brain_routes.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return card, notes, flags


@pytest.fixture
def client():
    app = create_app()
    app.state.auth_manager = _FakeAuthManager()
    return TestClient(app), app


def _open(c, app, conn: _DebateConn, sid: str):
    app.state.pool = _DebateDB(conn)
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    return c.get(f"/api/v1/agents/brain/sessions/{sid}")


def test_a_debate_card_moves_to_its_verdict_once(client, debate_worker):
    c, app = client
    card, notes, _ = debate_worker
    sid = str(uuid.uuid4())
    assert _open(c, app, _DebateConn(_run("PENDING_APPROVAL"), DECISION, {"status": "PENDING"}), sid).status_code == 200
    assert card["status"] == "VERDICT"
    (note,) = notes
    assert note["status"] == "VERDICT" and note["expect_status"] == "QUEUED" and note["widget_id"] == "deb-1"
    assert note["content"] == (f"Debate for {INSTRUMENT}: verdict BUY 5% (conviction 0.62), waiting for your approval.")
    _open(c, app, _DebateConn(_run("PENDING_APPROVAL"), DECISION, {"status": "PENDING"}), sid)
    assert len(notes) == 1  # a final state is written once


@pytest.mark.parametrize("run, card_status, reason_line", [
    (_run("GATE_REFUSED", "KILL_SWITCH_LATCHED"), "GATE_REFUSED",
     f"Debate for {INSTRUMENT} was not run: the kill switch is latched (KILL_SWITCH_LATCHED)."),
    (_run("GATE_REFUSED", "POSITION_AT_CAP"), "GATE_REFUSED",
     f"Debate for {INSTRUMENT} was not run: the position is at its cap (POSITION_AT_CAP)."),
    (_run("NO_PRICE"), "NO_TRADE", f"Debate for {INSTRUMENT} ended with no trade (NO_PRICE)."),
    (_run("FAILED", "WORKER_LOST"), "FAILED", f"Debate for {INSTRUMENT} failed (WORKER_LOST)."),
])
def test_each_final_state_writes_its_own_system_line(client, debate_worker, run, card_status, reason_line):
    c, app = client
    card, notes, _ = debate_worker
    _open(c, app, _DebateConn(run), str(uuid.uuid4()))
    assert card["status"] == card_status
    assert [n["content"] for n in notes] == [reason_line]


def test_a_running_debate_writes_nothing(client, debate_worker):
    c, app = client
    card, notes, _ = debate_worker
    assert _open(c, app, _DebateConn(_run("RUNNING")), str(uuid.uuid4())).status_code == 200
    assert notes == [] and card["status"] == "QUEUED"


def test_a_refused_debate_note_does_not_break_the_conversation(client, debate_worker):
    c, app = client
    card, notes, flags = debate_worker
    flags["reject_notes"] = True
    response = _open(c, app, _DebateConn(_run("GATE_REFUSED", "KILL_SWITCH_LATCHED")), str(uuid.uuid4()))
    assert response.status_code == 200 and len(notes) == 1
    assert card["status"] == "QUEUED"  # the worker kept it; the next load tries again


def test_the_debate_outcome_route_reports_the_run_and_writes_nothing(client, debate_worker):
    c, app = client
    card, notes, _ = debate_worker
    app.state.pool = _DebateDB(_DebateConn(_run("PENDING_APPROVAL"), DECISION, {"status": "APPROVED"}))
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    sid = str(uuid.uuid4())
    response = c.get(f"/api/v1/agents/brain/sessions/{sid}/cards/deb-1/debate")
    assert response.status_code == 200
    assert response.json() == {"status": "VERDICT", "run_id": str(RUN), "reason": None,
                               "verdict": _verdict("APPROVED")}
    assert notes == [] and card["status"] == "QUEUED"  # read only: the sync, not this route, writes the line


def test_a_debate_card_without_a_message_id_is_untracked(client, debate_worker):
    """A card queued before Wave 3 has no message id: it is UNTRACKED (not a failure), and the route reads no run."""
    c, app = client
    card, _, _ = debate_worker
    card.update(message_ref=None)
    conn = _DebateConn(_run("GATE_REFUSED", "KILL_SWITCH_LATCHED"))
    app.state.pool = _DebateDB(conn)
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    response = c.get(f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/deb-1/debate")
    assert response.json() == {"status": "UNTRACKED", "run_id": None, "verdict": None, "reason": None}
    assert conn.calls == []


def test_the_sync_skips_an_untracked_debate_card(client, debate_worker):
    """UNTRACKED is not a final state: the sync reads no run for the card and writes no system line for it."""
    c, app = client
    card, notes, _ = debate_worker
    card.update(message_ref=None)
    conn = _DebateConn(_run("GATE_REFUSED", "KILL_SWITCH_LATCHED"))
    assert _open(c, app, conn, str(uuid.uuid4())).status_code == 200
    assert notes == [] and card["status"] == "QUEUED"
    assert not any("debate_runs" in sql for sql, _ in conn.calls)


def test_an_untracked_card_is_not_followed():
    old = {"kind": "debate_summary", "id": "deb-old", "status": "QUEUED", "message_ref": None}
    assert followed_debates({"messages": [{"widgets": [old]}]}) == []
    assert debate_patch(old, {"status": "UNTRACKED", "reason": None}) is None


def test_an_unknown_debate_card_is_not_found(client, debate_worker):
    c, app = client
    app.state.pool = _DebateDB(_DebateConn())
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    assert c.get(f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/nope/debate").status_code == 404


@pytest.mark.parametrize("run_status, approval, tail", [
    ("PENDING_APPROVAL", None, "waiting for approval"),  # the operator decides: no approval row exists yet
    ("PENDING_APPROVAL", "PENDING", "waiting for your approval"),
    ("PENDING_APPROVAL", "APPROVED", "approved by the operator"),
    ("PENDING_APPROVAL", "VETOED", "vetoed"),
    ("PENDING_APPROVAL", "EXPIRED", "expired with no order"),
    ("PENDING_APPROVAL", "EXPIRED_PROCEEDED", "expired and proceeded"),
    ("PENDING_APPROVAL", "CANCELLED", "cancelled"),
    ("COMPLETED", None, "no approval needed"),  # sent under a mandate: nothing waits for the operator
    ("REJECTED", None, "rejected"),
])
def test_the_verdict_line_says_what_the_operator_must_do(run_status, approval, tail):
    card = {"id": "deb-1", "instrument_id": INSTRUMENT, "status": "QUEUED"}
    out = {"status": "VERDICT", "run_id": str(RUN), "reason": None, "verdict": _verdict(approval)}
    assert debate_line(card, out, run_status) == f"Debate for {INSTRUMENT}: verdict BUY 5% (conviction 0.62), {tail}."


def test_a_run_sent_under_a_mandate_writes_no_approval_wait(client, debate_worker):
    c, app = client
    card, notes, _ = debate_worker
    _open(c, app, _DebateConn(_run("COMPLETED"), DECISION, None), str(uuid.uuid4()))
    assert card["status"] == "VERDICT"
    assert [n["content"] for n in notes] == [f"Debate for {INSTRUMENT}: verdict BUY 5% (conviction 0.62), "
                                             "no approval needed."]


async def test_the_outcome_reads_a_run_and_its_decision_from_the_database(pool):
    """The real SQL of the read: the run by its message id (bare or with the stream), and its latest decision."""
    run_id = uuid.uuid4()
    await pool.execute(
        """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at, completed_at,
                                    stream_message_id, decision_trace_id)
           VALUES ($1, $2, 'COMPLETED', 'brain_on_demand', now(), now(), $3, $4)""",
        run_id, INSTRUMENT, STREAM_REF, uuid.uuid4())
    await pool.execute(
        """INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_level, conviction_score,
                                            allocation_pct, reasoning, decision_price)
           VALUES ($1, $2, 'BUY', 'HIGH', $3, $4, 'why', $5)""",
        run_id, INSTRUMENT, Decimal("0.62"), Decimal("5.00"), Decimal("65000"))
    async with pool.acquire() as conn:
        out, run_status = await debate_read(conn, REF)  # the tool's bare id finds the run recorded as "<stream>/<id>"
    assert run_status == "COMPLETED"
    assert out == {"status": "VERDICT", "run_id": str(run_id), "reason": None, "verdict": _verdict(None)}


# --- The worker and the API's client endpoint: the status set the sync may send, and no wider ---------------------

@pytest.mark.parametrize("status, expect_status", [("VERDICT", "QUEUED"), ("NO_TRADE", "QUEUED"),
                                                   ("GATE_REFUSED", "QUEUED"), ("FAILED", "QUEUED")])
def test_the_worker_accepts_a_debate_card_transition_from_the_sync_path(monkeypatch, status, expect_status):
    worker_app = worker_server.app
    calls: list[tuple] = []

    class Brain:
        async def note(self, operator_id, session_id, content, widget_id=None, widget_patch=None, expect_status=None):
            calls.append((widget_id, widget_patch, expect_status))
            return True

    sid = str(uuid.uuid4())
    path = f"/api/v1/brain/sessions/{sid}/notes"
    body = json.dumps({"operator_id": "op-1", "content": "Debate line.", "widget_id": "deb-1", "status": status,
                       "expect_status": expect_status}).encode()
    with TestClient(worker_app) as client:
        monkeypatch.setattr(worker_app.state, "brain", Brain(), raising=False)
        res = client.post(path, content=body, headers=_generate_hmac_headers("POST", path, body))
    assert res.status_code == 200
    assert calls == [("deb-1", {"status": status}, expect_status)]


@pytest.mark.parametrize("status, expect_status", [("COMPLETED", "QUEUED"),  # not a card state the sync sends
                                                   ("RUNNING", "QUEUED"),  # the sync only patches final states
                                                   ("VERDICT", "COMPLETED"),  # not an expectation the sync sends
                                                   ("VERDICT", "RUNNING")])  # no card waits in RUNNING either
def test_the_worker_still_refuses_a_status_or_expectation_outside_the_debate_set(monkeypatch, status, expect_status):
    worker_app = worker_server.app

    class Brain:
        async def note(self, *args, **kwargs):  # never reached: the request is refused first
            raise AssertionError("a refused note reached the brain")

    sid = str(uuid.uuid4())
    path = f"/api/v1/brain/sessions/{sid}/notes"
    body = json.dumps({"operator_id": "op-1", "content": "x", "widget_id": "deb-1", "status": status,
                       "expect_status": expect_status}).encode()
    with TestClient(worker_app) as client:
        monkeypatch.setattr(worker_app.state, "brain", Brain(), raising=False)
        res = client.post(path, content=body, headers=_generate_hmac_headers("POST", path, body))
    assert res.status_code == 422


def test_the_client_notes_endpoint_still_refuses_every_debate_status(client, debate_worker):
    c, app = client
    card, notes, _ = debate_worker
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    for status in ("VERDICT", "NO_TRADE", "GATE_REFUSED", "FAILED", "RUNNING"):
        res = c.post(f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/notes",
                     json={"widget_id": "deb-1", "status": status}, headers={"X-CSRF-Token": CSRF})
        assert res.status_code == 422, status
    assert notes == [] and card["status"] == "QUEUED"
