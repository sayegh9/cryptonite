"""A5.7: the global DEBATE_EXECUTION_MODE is gone; a debate verdict is routed by the mandate's level.

No mandate -> the operator decides; L1 -> approval (timeout = no); L2 -> veto window (proceeds only in
active hours); L3 -> queued; risk reducing is immediate at L2/L3 and a proceeding veto window at L1.
"""

import datetime
import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_execution.approvals import sweep_expired
from migrated_db import migrated_database

WORKER = Path(__file__).resolve().parents[1] / "services" / "debate_worker"
sys.path.insert(0, str(WORKER))
BTC = "BTCUSDT.BINANCE"


@pytest.fixture(scope="module")
def routing():
    spec = importlib.util.spec_from_file_location("debate_mandate_routing", WORKER / "mandate_routing.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_global_execution_mode_is_gone():
    main = (WORKER / "main.py").read_text()
    for gone in ("DEBATE_EXECUTION_MODE", "resolve_execution_mode", "system_settings", "'autonomous'", "MODIFY_ORDER"):
        assert gone not in main, gone
    assert "route_entry(" in main and "route_exit(" in main


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_routing") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_positions CASCADE")
    try:
        yield harness
    finally:
        await close_harness(harness)


async def run_row(h, run_id):
    await h.pool.execute("INSERT INTO debate_runs (id, instrument_id, status, trigger_source) VALUES ($1, $2, 'RUNNING', 'test')",
                         run_id, BTC)


async def test_no_mandate_leaves_it_to_the_operator(h, routing):
    async with h.pool.acquire() as conn:
        status, m = await routing.route_entry(conn, uuid.uuid4(), BTC, "Overweight", 0.7, 6.0)
    assert (status, m) == ("PENDING_APPROVAL", None)
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_l1_opens_an_approval_that_times_out_as_no(h, routing):
    m = await h.mandate(level=1)
    run_id = uuid.uuid4()
    async with h.pool.acquire() as conn:
        status, got = await routing.route_entry(conn, run_id, BTC, "Overweight", 0.7, 40.0)
    assert (status, got.id) == ("PENDING_APPROVAL", m.id)
    a = await h.pool.fetchrow("SELECT * FROM ai_approvals WHERE source = 'debate' AND source_ref = $1", str(run_id))
    payload = json.loads(a["payload"])
    assert (a["action_class"], a["timeout_policy"], a["command_type"]) == ("ADD_RISK", "DENY", "SUBMIT_ORDER")
    assert payload["allocation_pct"] == 25.0  # capped at the mandate's own position limit, never a breach
    assert await h.pool.fetchval("SELECT tier FROM notifications_outbox WHERE approval_id = $1", a["id"]) == "ACTION"


async def test_l2_veto_window_depends_on_active_hours(h, routing):
    noon = datetime.datetime(2026, 9, 30, 12, tzinfo=datetime.UTC)
    await h.mandate(level=2, active_hours="08:00-22:00")
    run_in, run_out = uuid.uuid4(), uuid.uuid4()
    async with h.pool.acquire() as conn:
        await routing.route_entry(conn, run_in, BTC, "Buy", 0.7, 5.0, now=noon)
        await routing.route_entry(conn, run_out, BTC, "Buy", 0.7, 5.0, now=noon.replace(hour=23))
    policies = {r["source_ref"]: r["timeout_policy"] for r in await h.pool.fetch("SELECT source_ref, timeout_policy FROM ai_approvals")}
    assert (policies[str(run_in)], policies[str(run_out)]) == ("PROCEED", "DENY")


async def test_l3_queues_the_order_inside_the_mandate(h, routing):
    m = await h.mandate(level=3)
    run_id = uuid.uuid4()
    async with h.pool.acquire() as conn:
        status, _ = await routing.route_entry(conn, run_id, BTC, "Buy", 0.8, 5.0)
    assert status == "COMPLETED"
    cmd = await h.pool.fetchrow("SELECT * FROM execution_commands WHERE idempotency_key = $1", f"debate:{run_id}")
    assert (cmd["command_type"], cmd["target_instance_id"], cmd["requester_id"], cmd["mandate_id"]) == (
        "SUBMIT_ORDER", m.instance_id, "ai:debate_worker", m.id)
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0


async def hold(h, m, qty="0.1"):
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price, quantity, current_price, status)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'LONG', 50000, $4, 50000, 'OPEN')""", uuid.uuid4().hex, m.instance_id, BTC, float(qty))


async def test_an_entry_only_asks_for_the_room_left_under_the_position_limit(h, routing):
    """The mandate's limit is 25% of $10,000. Holding $2,300 leaves 2%: the operator is asked for 2%,
    not the debate's 6%. Holding $2,475 leaves 0.25%: nothing to ask, no trade, no approval."""
    m = await h.mandate(level=1)
    await hold(h, m, "0.046")  # $2,300 at 50,000
    run_id = uuid.uuid4()
    async with h.pool.acquire() as conn:
        assert (await routing.route_entry(conn, run_id, BTC, "Overweight", 0.7, 6.0))[0] == "PENDING_APPROVAL"
    a = await h.pool.fetchrow("SELECT payload, summary FROM ai_approvals WHERE source_ref = $1", str(run_id))
    assert json.loads(a["payload"])["allocation_pct"] == 2.0 and "2% of mandate ($200)" in a["summary"]

    await hold(h, m, "0.0035")  # +$175: $2,475, 0.25% left
    run_id = uuid.uuid4()
    async with h.pool.acquire() as conn:
        assert (await routing.route_entry(conn, run_id, BTC, "Overweight", 0.7, 6.0))[0] == "NO_TRADE"
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals WHERE source_ref = $1", str(run_id)) == 0


async def test_selling_the_mandates_position_is_risk_reducing(h, routing):
    m = await h.mandate(level=2)
    async with h.pool.acquire() as conn:
        assert (await routing.route_entry(conn, uuid.uuid4(), BTC, "Underweight", 0.6, 5.0))[0] == "NO_TRADE"  # holds nothing
    await hold(h, m)
    run_id = uuid.uuid4()
    async with h.pool.acquire() as conn:
        assert (await routing.route_entry(conn, run_id, BTC, "Sell", 0.6, 5.0))[0] == "COMPLETED"  # immediate at L2
    payload = json.loads(await h.pool.fetchval("SELECT payload FROM execution_commands WHERE idempotency_key = $1", f"debate:{run_id}"))
    assert (payload["side"], float(payload["quantity"])) == ("SELL", 0.1)


async def test_exit_debate_reduce_proceeds_at_l1_and_is_immediate_at_l3(h, routing):
    m1 = await h.mandate(level=1)
    await hold(h, m1)
    run1 = uuid.uuid4()
    await run_row(h, run1)
    async with h.pool.acquire() as conn:
        status, _ = await routing.route_exit(conn, run1, BTC, "REDUCE")
    assert status == "PENDING_APPROVAL"
    a = await h.pool.fetchrow("SELECT * FROM ai_approvals WHERE source = 'exit_debate' AND source_ref = $1", str(run1))
    assert (a["action_class"], a["timeout_policy"], a["command_type"]) == ("REDUCE_RISK", "PROCEED", "CLOSE_POSITION")
    assert float(json.loads(a["payload"])["quantity"]) == pytest.approx(0.05)
    await h.pool.execute("UPDATE debate_runs SET status = 'PENDING_APPROVAL' WHERE id = $1", run1)
    async with h.pool.acquire() as conn:
        await sweep_expired(conn, now=a["expires_at"] + datetime.timedelta(seconds=1))
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands WHERE command_type = 'CLOSE_POSITION'") == 1
    assert await h.pool.fetchval("SELECT status FROM debate_runs WHERE id = $1", run1) == "COMPLETED"

    await h.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'; UPDATE paper_positions SET status = 'CLOSED'")
    m3 = await h.mandate(level=3)
    await hold(h, m3)
    run3 = uuid.uuid4()
    async with h.pool.acquire() as conn:
        assert (await routing.route_exit(conn, run3, BTC, "CLOSE"))[0] == "COMPLETED"
    cmd = await h.pool.fetchrow("SELECT * FROM execution_commands WHERE idempotency_key = $1", f"exit_debate:{run3}")
    assert (cmd["command_type"], cmd["target_instance_id"], float(json.loads(cmd["payload"])["quantity"])) == (
        "CLOSE_POSITION", m3.instance_id, 0.1)


async def test_exit_debate_on_a_shared_or_legacy_position_waits_for_the_operator(h, routing):
    m = await h.mandate(level=3)
    await hold(h, m)
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price, quantity, current_price, status)
           VALUES ($1, 'PAPER-ACCOUNT-001', 'MANUAL_TRADE', $2, 'LONG', 50000, 1, 50000, 'OPEN')""", uuid.uuid4().hex, BTC)
    async with h.pool.acquire() as conn:
        assert (await routing.route_exit(conn, uuid.uuid4(), BTC, "CLOSE")) == ("PENDING_APPROVAL", None)
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_web_approval_of_a_debate_decides_its_open_approval_once(h, routing):
    await h.mandate(level=1)
    run_id = uuid.uuid4()
    await run_row(h, run_id)
    await h.pool.execute("UPDATE debate_runs SET status = 'PENDING_APPROVAL' WHERE id = $1", run_id)
    await h.pool.execute(
        # the debate worker's write shape since B2 / migration 037: direction + the CRO's wording
        """INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_level, conviction_score,
                                            allocation_pct, reasoning)
           VALUES ($1, $2, 'BUY', 'Overweight', 0.7, 5, 'r')""", run_id, BTC)
    await h.pool.execute("UPDATE paper_accounts SET total_equity = 100000")
    await h.redis.set("market:24h:BTCUSDT", json.dumps({"last_price": 50000}), ex=60)
    async with h.pool.acquire() as conn:
        await routing.route_entry(conn, run_id, BTC, "Overweight", 0.7, 5.0)
    async with h.client() as c:
        res = await c.post(f"/api/v1/agents/debates/{run_id}/approve", headers=h.headers)
        again = await c.post(f"/api/v1/agents/debates/{run_id}/approve", headers=h.headers)
    assert res.status_code == 200, res.text
    assert again.status_code == 409  # the run is no longer pending
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands WHERE payload->>'run_id' = $1", str(run_id)) == 1
    assert await h.pool.fetchval("SELECT status FROM debate_runs WHERE id = $1", run_id) == "COMPLETED"


# --- R7.4: decision_trace_id set at insert on every command a debate leads to ----------------------

TRACE = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"


async def test_l3_command_carries_the_decision_trace_id(h, routing):
    await h.mandate(level=3)
    run_id = uuid.uuid4()
    async with h.pool.acquire() as conn:
        await routing.route_entry(conn, run_id, BTC, "Buy", 0.8, 5.0, decision_trace_id=TRACE)
    cmd = await h.pool.fetchrow("SELECT decision_trace_id, payload FROM execution_commands WHERE idempotency_key = $1",
                                f"debate:{run_id}")
    assert str(cmd["decision_trace_id"]) == TRACE
    assert json.loads(cmd["payload"])["decision_trace_id"] == TRACE


async def test_approved_command_carries_the_decision_trace_id(h, routing):
    from cryptonite_execution.approvals import decide_approval
    await h.mandate(level=1)
    run_id = uuid.uuid4()
    async with h.pool.acquire() as conn:
        await routing.route_entry(conn, run_id, BTC, "Buy", 0.8, 5.0, decision_trace_id=TRACE)
        approval_id = await conn.fetchval("SELECT id FROM ai_approvals WHERE source_ref = $1", str(run_id))
        decision = await decide_approval(conn, approval_id, True, via="web", decided_by="operator:test")
    assert str(await h.pool.fetchval("SELECT decision_trace_id FROM execution_commands WHERE command_id = $1",
                                     decision.command_id)) == TRACE


async def test_commands_without_a_trace_keep_it_null(h, routing):
    await h.mandate(level=3)
    run_id = uuid.uuid4()
    async with h.pool.acquire() as conn:
        await routing.route_entry(conn, run_id, BTC, "Buy", 0.8, 5.0)
    assert await h.pool.fetchval("SELECT decision_trace_id FROM execution_commands WHERE idempotency_key = $1",
                                 f"debate:{run_id}") is None


@pytest.fixture(autouse=True)
def _autotrade_switch_on(monkeypatch):
    """ADR 0011 D10 applies at runtime: with the master switch off a Veto window or Fully automatic mandate behaves as
    Ask me. These tests exercise the switch-on behaviour of those levels."""
    from cryptonite_execution import profile_lifecycle

    async def on(conn):
        return True

    monkeypatch.setattr(profile_lifecycle, "autotrade_switch_on", on)
