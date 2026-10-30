"""A5.3 approvals with expiry and the D8 timeout policy, end to end on a migrated database.

- ADD_RISK timeout = no; REDUCE_RISK timeout = proceed (its command runs); the L2 veto window
  proceeds only inside the operator's active hours;
- decisions are server-side, claimed once and idempotent; a late decision gets the timeout;
- an approved order passes the orchestrator's mandate check only with the command it is bound to;
- the Brain order card and the web decision endpoint go through the same approvals.
"""

import asyncio
import datetime
import json
import time
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import asyncpg
import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_execution.approvals import (
    ApprovalConflict,
    create_approval,
    decide_approval,
    record_operator_approval,
    sweep_expired,
)
from cryptonite_orchestrator.server import SimpleQuoteCache, command_poller
from cryptonite_schemas.mandate import (
    ActionClass,
    AutonomyLevel,
    TimeoutPolicy,
    in_active_hours,
    timeout_policy_for,
)
from cryptonite_schemas.marks import quote_tick_key
from migrated_db import migrated_database
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_fresh_dispatcher

D = Decimal
SPOT = "BTCUSDT.BINANCE"
UTC = datetime.UTC
NOON = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------------------------ pure policy


@pytest.mark.parametrize("action,level,active,expected", [
    (ActionClass.ADD_RISK, AutonomyLevel.L1, True, TimeoutPolicy.DENY),
    (ActionClass.ADD_RISK, AutonomyLevel.L1, False, TimeoutPolicy.DENY),
    (ActionClass.ADD_RISK, AutonomyLevel.L2, True, TimeoutPolicy.PROCEED),  # veto window
    (ActionClass.ADD_RISK, AutonomyLevel.L2, False, TimeoutPolicy.DENY),  # outside active hours
    (ActionClass.ADD_RISK, AutonomyLevel.L3, False, None),  # no approval at L3
    (ActionClass.REDUCE_RISK, AutonomyLevel.L1, False, TimeoutPolicy.PROCEED),
    (ActionClass.REDUCE_RISK, AutonomyLevel.L3, True, TimeoutPolicy.PROCEED),
])
def test_d8_timeout_policy(action, level, active, expected):
    assert timeout_policy_for(action, level, active) is expected


def test_active_hours_are_utc_and_may_wrap_midnight():
    assert in_active_hours("08:00-22:00", NOON)
    assert not in_active_hours("08:00-22:00", NOON.replace(hour=23))
    assert in_active_hours("22:00-06:00", NOON.replace(hour=23)) and in_active_hours("22:00-06:00", NOON.replace(hour=5))
    assert not in_active_hours("22:00-06:00", NOON)
    assert not in_active_hours(None, NOON)  # no active hours: never, so an L2 timeout is no


# ------------------------------------------------------------------------------------ database


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_approvals") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.redis.set(quote_tick_key(SPOT), json.dumps({
        "instrument_id": SPOT, "bid_price": "59999", "ask_price": "60001", "arrival_ns": time.time_ns(),
    }), ex=60)
    try:
        yield harness
    finally:
        await close_harness(harness)


def buy_payload(qty="0.01"):
    return {"instrument_id": SPOT, "side": "BUY", "order_type": "MARKET", "quantity": qty,
            "proposed_stop_loss": "49000"}


async def commands_for(pool, approval_id):
    return await pool.fetch("SELECT * FROM execution_commands WHERE payload->>'approval_id' = $1", str(approval_id))


async def test_add_risk_timeout_means_no(h):
    m = await h.mandate(level=1)
    async with h.pool.acquire() as conn:
        a = await create_approval(conn, m, ActionClass.ADD_RISK, "SUBMIT_ORDER", buy_payload(), summary="BUY BTC",
                                  source="test", source_ref=uuid.uuid4().hex)
        assert (a["status"], a["timeout_policy"]) == ("PENDING", "DENY")
        assert await conn.fetchval("SELECT tier FROM notifications_outbox WHERE approval_id = $1", a["id"]) == "ACTION"
        done = await sweep_expired(conn, now=a["expires_at"] + datetime.timedelta(seconds=1))
    assert (a["id"], "EXPIRED") in done
    assert await h.pool.fetchval("SELECT status FROM ai_approvals WHERE id = $1", a["id"]) == "EXPIRED"
    assert await commands_for(h.pool, a["id"]) == []


async def test_reduce_risk_timeout_means_proceed(h):
    m = await h.mandate(level=1)
    payload = {"instrument_id": SPOT, "quantity": "0.01", "reason": "EXIT_DEBATE"}
    async with h.pool.acquire() as conn:
        a = await create_approval(conn, m, ActionClass.REDUCE_RISK, "CLOSE_POSITION", payload, summary="close BTC",
                                  source="test", source_ref=uuid.uuid4().hex)
        assert a["timeout_policy"] == "PROCEED"
        assert (a["expires_at"] - a["created_at"]).total_seconds() == pytest.approx(300, abs=5)
        await sweep_expired(conn, now=a["expires_at"] + datetime.timedelta(seconds=1))
    row = await h.pool.fetchrow("SELECT status, decided_via, command_id FROM ai_approvals WHERE id = $1", a["id"])
    assert (row["status"], row["decided_via"]) == ("EXPIRED_PROCEEDED", "timeout")
    (cmd,) = await commands_for(h.pool, a["id"])
    assert (cmd["command_type"], cmd["target_instance_id"], cmd["command_id"]) == ("CLOSE_POSITION", m.instance_id, row["command_id"])
    assert cmd["mandate_id"] == m.id


async def test_l2_veto_window_proceeds_inside_active_hours_only(h):
    inside = await h.mandate(level=2, active_hours="00:00-23:59")
    outside = await h.mandate(level=2, active_hours=None)
    async with h.pool.acquire() as conn:
        a = await create_approval(conn, inside, ActionClass.ADD_RISK, "SUBMIT_ORDER", buy_payload(), summary="s",
                                  source="test", source_ref=uuid.uuid4().hex, now=NOON)
        b = await create_approval(conn, outside, ActionClass.ADD_RISK, "SUBMIT_ORDER", buy_payload(), summary="s",
                                  source="test", source_ref=uuid.uuid4().hex, now=NOON)
        assert (a["timeout_policy"], b["timeout_policy"]) == ("PROCEED", "DENY")
        assert (a["expires_at"] - NOON).total_seconds() == 600  # the mandate's veto window
        await sweep_expired(conn, now=NOON + datetime.timedelta(hours=1))
    assert len(await commands_for(h.pool, a["id"])) == 1
    assert await commands_for(h.pool, b["id"]) == []


async def test_l3_add_risk_needs_no_approval(h):
    m = await h.mandate(level=3)
    async with h.pool.acquire() as conn:
        assert await create_approval(conn, m, ActionClass.ADD_RISK, "SUBMIT_ORDER", buy_payload(), summary="s",
                                     source="test", source_ref=uuid.uuid4().hex) is None


async def test_db_refuses_a_silent_add_risk_at_l1(h):
    m = await h.mandate(level=1)
    with pytest.raises(asyncpg.CheckViolationError):
        await h.pool.execute(
            """INSERT INTO ai_approvals (mandate_id, action_class, command_type, payload, source, source_ref, level_at_creation,
                   timeout_policy, expires_at) VALUES ($1, 'ADD_RISK', 'SUBMIT_ORDER', '{}', 't', $2, 1, 'PROCEED', now())""",
            m.id, uuid.uuid4().hex)


async def test_decisions_are_claimed_once_and_idempotent(h):
    m = await h.mandate(level=1)
    async with h.pool.acquire() as conn:
        a = await create_approval(conn, m, ActionClass.ADD_RISK, "SUBMIT_ORDER", buy_payload(), summary="s",
                                  source="test", source_ref=uuid.uuid4().hex)
        first = await decide_approval(conn, a["id"], True, via="web", decided_by="op")
        again = await decide_approval(conn, a["id"], True, via="telegram", decided_by="tg")
        assert (first.status, again.replayed, again.command_id) == ("APPROVED", True, first.command_id)
        with pytest.raises(ApprovalConflict):
            await decide_approval(conn, a["id"], False, via="telegram", decided_by="tg")  # a late veto loses
    assert len(await commands_for(h.pool, a["id"])) == 1


async def test_racing_approve_and_veto_have_one_winner(h):
    m = await h.mandate(level=1)
    async with h.pool.acquire() as conn:
        a = await create_approval(conn, m, ActionClass.ADD_RISK, "SUBMIT_ORDER", buy_payload(), summary="s",
                                  source="test", source_ref=uuid.uuid4().hex)

    async def decide(approve):
        async with h.pool.acquire() as conn:
            try:
                return (await decide_approval(conn, a["id"], approve, via="web", decided_by="x")).status
            except ApprovalConflict as e:
                return f"conflict:{e.status}"

    results = await asyncio.gather(decide(True), decide(False))
    assert sorted(r.startswith("conflict") for r in results) == [False, True]
    final = await h.pool.fetchval("SELECT status FROM ai_approvals WHERE id = $1", a["id"])
    assert len(await commands_for(h.pool, a["id"])) == (1 if final == "APPROVED" else 0)


async def test_late_approval_gets_the_timeout_instead(h):
    m = await h.mandate(level=1)
    async with h.pool.acquire() as conn:
        a = await create_approval(conn, m, ActionClass.ADD_RISK, "SUBMIT_ORDER", buy_payload(), summary="s",
                                  source="test", source_ref=uuid.uuid4().hex)
        with pytest.raises(ApprovalConflict) as e:
            await decide_approval(conn, a["id"], True, via="web", decided_by="op",
                                  now=a["expires_at"] + datetime.timedelta(seconds=1))
    assert e.value.status == "EXPIRED"
    assert await h.pool.fetchval("SELECT status FROM ai_approvals WHERE id = $1", a["id"]) == "EXPIRED"
    assert await commands_for(h.pool, a["id"]) == []


async def run_poller(pool, command_id):
    d, _ = make_fresh_dispatcher(starting_cash=D("100000"))
    engine = make_engine(d)
    engine.bridge = MagicMock()
    quotes = SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)
    task = asyncio.create_task(command_poller(pool, engine, quotes))
    try:
        for _ in range(100):
            row = await pool.fetchrow("SELECT status, error_message FROM execution_commands WHERE command_id = $1", command_id)
            if row["status"] in ("COMPLETED", "REJECTED"):
                return row, engine
            await asyncio.sleep(0.05)
        raise AssertionError("command not processed")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_orchestrator_accepts_an_approved_order_only_through_its_bound_command(h):
    m = await h.mandate(level=1)
    async with h.pool.acquire() as conn:
        a = await create_approval(conn, m, ActionClass.ADD_RISK, "SUBMIT_ORDER", buy_payload(), summary="s",
                                  source="test", source_ref=uuid.uuid4().hex)
        d = await decide_approval(conn, a["id"], True, via="web", decided_by="op")
        # a forged second command that reuses the approval id
        forged = uuid.uuid4()
        await conn.execute(
            """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, requester_id, payload,
                   status, environment, account_id, mandate_id) VALUES ($1, $2, 'SUBMIT_ORDER', $3, 'ai:x', $4::jsonb, 'PENDING', 'PAPER', $5, $6)""",
            forged, f"forged:{forged}", m.instance_id,
            json.dumps({**buy_payload(), "approval_id": str(a["id"]), "mandate_id": str(m.id)}), "PAPER-ACCOUNT-001", m.id)
    row, engine = await run_poller(h.pool, d.command_id)
    assert row["status"] == "COMPLETED", row["error_message"]
    assert engine.bridge.enqueue_event.call_args.args[0]["instance_id"] == m.instance_id
    row, _ = await run_poller(h.pool, forged)
    assert (row["status"], row["error_message"]) == ("REJECTED", "MANDATE_APPROVAL_REQUIRED")


async def test_a_proceeded_veto_window_counts_only_while_the_mandate_is_still_l2(h):
    m = await h.mandate(level=2, active_hours="00:00-23:59")
    async with h.pool.acquire() as conn:
        a = await create_approval(conn, m, ActionClass.ADD_RISK, "SUBMIT_ORDER", buy_payload(), summary="s",
                                  source="test", source_ref=uuid.uuid4().hex)
        await h.pool.execute("UPDATE ai_mandates SET autonomy_level = 1 WHERE id = $1", m.id)  # demoted meanwhile
        await sweep_expired(conn, now=a["expires_at"] + datetime.timedelta(seconds=1))
    (cmd,) = await commands_for(h.pool, a["id"])
    row, _ = await run_poller(h.pool, cmd["command_id"])
    assert (row["status"], row["error_message"]) == ("REJECTED", "MANDATE_APPROVAL_REQUIRED")


async def test_record_operator_approval_is_idempotent(h):
    m = await h.mandate(level=1)
    async with h.pool.acquire() as conn:
        ref = uuid.uuid4().hex
        one = await record_operator_approval(conn, m, "SUBMIT_ORDER", buy_payload(), summary="s", source="brain_card",
                                             source_ref=ref, decided_by="op", idempotency_key=f"k:{ref}")
        two = await record_operator_approval(conn, m, "SUBMIT_ORDER", buy_payload(), summary="s", source="brain_card",
                                             source_ref=ref, decided_by="op", idempotency_key=f"k:{ref}")
    assert (one.command_id, two.replayed) == (two.command_id, True)
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands WHERE idempotency_key = $1", f"k:{ref}") == 1


# -------------------------------------------------------------------------------------------- API


async def test_web_approve_and_veto(h):
    m = await h.mandate(level=1)
    async with h.pool.acquire() as conn:
        a = await create_approval(conn, m, ActionClass.ADD_RISK, "SUBMIT_ORDER", buy_payload(), summary="BUY BTC",
                                  source="test", source_ref=uuid.uuid4().hex)
        b = await create_approval(conn, m, ActionClass.ADD_RISK, "SUBMIT_ORDER", buy_payload(), summary="BUY BTC",
                                  source="test", source_ref=uuid.uuid4().hex)
    async with h.client() as c:
        listed = (await c.get("/api/v1/ai/approvals")).json()["approvals"]
        mine = {x["id"]: x for x in listed}
        assert mine[str(a["id"])]["seconds_left"] > 800 and mine[str(a["id"])]["mandate"] == m.name
        assert (await c.post(f"/api/v1/ai/approvals/{a['id']}/decision", json={"decision": "APPROVE"})).status_code == 403  # no CSRF
        ok = await c.post(f"/api/v1/ai/approvals/{a['id']}/decision", json={"decision": "APPROVE"}, headers=h.headers)
        assert ok.status_code == 200 and ok.json()["command_id"]
        veto = await c.post(f"/api/v1/ai/approvals/{b['id']}/decision", json={"decision": "VETO"}, headers=h.headers)
        assert veto.json()["status"] == "VETOED" and veto.json()["command_id"] is None
        late = await c.post(f"/api/v1/ai/approvals/{b['id']}/decision", json={"decision": "APPROVE"}, headers=h.headers)
        assert late.status_code == 409
    assert await commands_for(h.pool, b["id"]) == []


BRAIN_CARD = {"kind": "order_card", "id": "call-1", "instrument_id": SPOT, "side": "BUY", "quantity": 0.01,
              "order_type": "MARKET", "limit_price": None, "allocation_pct": 5.0, "stop_loss": 49000.0,
              "take_profit": None, "status": "PENDING_APPROVAL", "trace_id": str(uuid.uuid4()), "command_id": None}


@pytest.fixture
def brain_worker(monkeypatch):
    import httpx
    from cryptonite_api.routes import brain as brain_routes

    card = dict(BRAIN_CARD, trace_id=str(uuid.uuid4()))  # one Brain turn per test (D4: one order per decision)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"session": {}, "messages": [{"role": "assistant", "widgets": [card]}]})
        body = json.loads(request.content)
        if body.get("expect_status") and card["status"] != body["expect_status"]:
            return httpx.Response(409)
        card.update({k: body[k] for k in ("status", "command_id") if body.get(k)})
        return httpx.Response(200, json={"status": "ok"})

    real = httpx.AsyncClient
    monkeypatch.setattr(brain_routes.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return card


async def test_brain_card_approval_records_an_approval_inside_the_mandate(h, brain_worker):
    await h.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")  # start with no mandate covering BTC
    sid = uuid.uuid4()
    path = f"/api/v1/agents/brain/sessions/{sid}/cards/call-1/approve"
    async with h.client() as c:
        none = await c.post(path, headers=h.headers)
        assert none.status_code == 409 and "NO_MANDATE" in none.json()["detail"]
        assert brain_worker["status"] == "PENDING_APPROVAL"  # not claimed when no mandate covers it

        m = await h.mandate(level=1, instruments=("BTCUSDT.BINANCE",))
        res = await c.post(path, headers=h.headers)
        assert res.status_code == 200, res.text
        body = res.json()
        again = await c.post(path, headers=h.headers)
        assert again.json()["command_id"] == body["command_id"]
    cmd = await h.pool.fetchrow("SELECT * FROM execution_commands WHERE command_id = $1", uuid.UUID(body["command_id"]))
    payload = json.loads(cmd["payload"])
    assert (cmd["idempotency_key"], cmd["target_instance_id"], cmd["mandate_id"]) == (f"brain-card:{sid}:call-1", m.instance_id, m.id)
    assert payload["approval_id"] == body["approval_id"] and payload["allocation_pct"] == 5.0
    appr = await h.pool.fetchrow("SELECT * FROM ai_approvals WHERE id = $1", uuid.UUID(body["approval_id"]))
    assert (appr["status"], appr["decided_via"], appr["source"], appr["command_id"]) == ("APPROVED", "web", "brain_card", cmd["command_id"])
    assert brain_worker["status"] == "APPROVED"
    row, _ = await run_poller(h.pool, cmd["command_id"])
    assert row["status"] == "COMPLETED", row["error_message"]


async def test_brain_card_gets_only_the_room_left_under_the_position_limit(h, brain_worker):
    """The card asks for 5%; the mandate (limit 25% of $10,000) already holds $2,300, so 2% is queued.
    At the limit the approval is refused with a clear reason and the card stays pending."""
    await h.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    m = await h.mandate(level=1, instruments=("BTCUSDT.BINANCE",), name=f"r-{uuid.uuid4().hex[:4]}")
    pos = """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price, quantity,
                 current_price, status) VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'LONG', 50000, $4, 50000, 'OPEN')"""
    path = f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/call-1/approve"
    try:
        await h.pool.execute(pos, uuid.uuid4().hex, m.instance_id, SPOT, 0.046)  # $2,300
        async with h.client() as c:
            res = await c.post(path, headers=h.headers)
            assert res.status_code == 200, res.text
            cmd = await h.pool.fetchrow("SELECT payload FROM execution_commands WHERE command_id = $1",
                                        uuid.UUID(res.json()["command_id"]))
            assert json.loads(cmd["payload"])["allocation_pct"] == 2.0

            brain_worker.update(status="PENDING_APPROVAL", command_id=None, trace_id=str(uuid.uuid4()))  # a new turn
            await h.pool.execute(pos, uuid.uuid4().hex, m.instance_id, SPOT, 0.004)  # $2,500: at the limit
            full = await c.post(f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/call-1/approve", headers=h.headers)
    finally:
        # The open BTC rows would make every later test's preflight see MANDATE_INSTRUMENT_CONFLICT (the module
        # shares one database), so this test removes its own rows and archives its own mandate.
        await h.pool.execute("DELETE FROM paper_positions WHERE instance_id = $1", m.instance_id)
        await h.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED' WHERE id = $1", m.id)
    assert full.status_code == 409 and "MANDATE_POSITION_LIMIT_REACHED" in full.json()["detail"]
    assert brain_worker["status"] == "PENDING_APPROVAL"


async def test_brain_card_needs_the_exit_monitor(h, brain_worker):
    await h.redis.delete("exit_monitor:heartbeat")
    await h.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await h.mandate(level=1, instruments=("BTCUSDT.BINANCE",), name=f"x-{uuid.uuid4().hex[:4]}")
    async with h.client() as c:
        res = await c.post(f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/call-1/approve", headers=h.headers)
    assert res.status_code == 409 and "EXIT_MONITOR_DOWN" in res.json()["detail"]


async def test_a_crash_between_claim_and_queue_is_retried_without_a_duplicate(h, brain_worker):
    await h.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await h.mandate(level=1, instruments=("BTCUSDT.BINANCE",))
    brain_worker["status"] = "APPROVED"  # claimed, then the API died before the command was queued
    sid = uuid.uuid4()
    async with h.client() as c:
        for _ in range(2):
            res = await c.post(f"/api/v1/agents/brain/sessions/{sid}/cards/call-1/approve", headers=h.headers)
            assert res.status_code == 200, res.text
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands WHERE idempotency_key = $1", f"brain-card:{sid}:call-1") == 1
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals WHERE source = 'brain_card' AND source_ref = $1", f"{sid}:call-1") == 1


async def test_a_veto_window_that_runs_past_the_active_hours_times_out_as_no(h):
    m = await h.mandate(level=2, active_hours="08:00-12:05")
    async with h.pool.acquire() as conn:
        a = await create_approval(conn, m, ActionClass.ADD_RISK, "SUBMIT_ORDER", buy_payload(), summary="s",
                                  source="test", source_ref=uuid.uuid4().hex, now=NOON)
        assert a["timeout_policy"] == "PROCEED"  # created inside the hours
        await sweep_expired(conn, now=NOON + datetime.timedelta(minutes=11))  # deadline 12:10 is outside
    assert await h.pool.fetchval("SELECT status FROM ai_approvals WHERE id = $1", a["id"]) == "EXPIRED"
    assert await commands_for(h.pool, a["id"]) == []
