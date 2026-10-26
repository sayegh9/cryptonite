"""A5.5 autonomy ladder (§4.5): every transition of the state machine, the gate evaluator, system
proposals confirmed with TOTP, automatic demotion on a breach, and the mandate editor's TOTP rules."""

import asyncio
import datetime
import json
import math
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import asyncpg
import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_execution.autonomy import (
    AutonomyState,
    AutonomyTrigger,
    GateMetrics,
    TransitionRefused,
    evaluate_gate,
    period_returns,
    sortino,
    transition,
)
from cryptonite_execution.autonomy_metrics import gate_metrics, propose_if_ready
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_orchestrator.server import SimpleQuoteCache, command_poller
from cryptonite_schemas.mandate import AutonomyLevel, MandateStatus
from hypothesis import given
from hypothesis import strategies as st
from migrated_db import migrated_database
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_fresh_dispatcher

D = Decimal
L1, L2, L3 = AutonomyLevel.L1, AutonomyLevel.L2, AutonomyLevel.L3
ACTIVE, HALTED, PAUSED = MandateStatus.ACTIVE, MandateStatus.HALTED, MandateStatus.PAUSED

L2_READY = GateMetrics(days_at_level=31, closed_trades=30, max_drawdown_pct=D("8"), breaches=0, sortino=1.4, benchmark_sortino=0.9)
L3_READY = GateMetrics(days_at_level=61, calibration_error_pp=D("6"), veto_rate_pct=D("4"), breaches=0)


def s(level, status=ACTIVE, perps=False):
    return AutonomyState(level, status, perps)


# ------------------------------------------------------------------------- promotion (TOTP, gate)


def test_l1_to_l2_needs_the_operator_totp_and_a_passing_gate():
    t = transition(s(L1), AutonomyTrigger.PROMOTION_CONFIRMED, totp_ok=True, gate=evaluate_gate(L1, L2_READY))
    assert (t.after.level, t.kind) == (L2, "PROMOTED")


def test_l2_to_l3():
    t = transition(s(L2), AutonomyTrigger.PROMOTION_CONFIRMED, totp_ok=True, gate=evaluate_gate(L2, L3_READY))
    assert (t.after.level, t.kind) == (L3, "PROMOTED")


@pytest.mark.parametrize("state,kw,code", [
    (s(L1), {"totp_ok": False, "gate": evaluate_gate(L1, L2_READY)}, "TOTP_REQUIRED"),
    # ADR 0011 D3: the gate is advisory, so a failing or missing gate refuses nothing (see test_autonomy_advisory.py)
    (s(L3), {"totp_ok": True, "gate": evaluate_gate(L3, L3_READY)}, "ALREADY_AT_LEVEL"),  # was ALREADY_AT_TOP
    (s(L1, HALTED), {"totp_ok": True, "gate": evaluate_gate(L1, L2_READY)}, "MANDATE_NOT_ACTIVE"),
])
def test_promotion_is_refused(state, kw, code):
    with pytest.raises(TransitionRefused) as e:
        transition(state, AutonomyTrigger.PROMOTION_CONFIRMED, **kw)
    assert e.value.code == code


# --------------------------------------------------------------------------------- demotion


@pytest.mark.parametrize("trigger", [AutonomyTrigger.BREACH, AutonomyTrigger.HALF_DRAWDOWN])
@pytest.mark.parametrize("before,after,kind", [(L3, L2, "DEMOTED"), (L2, L1, "DEMOTED"), (L1, L1, "NOOP")])
def test_breach_or_half_drawdown_drops_one_level(trigger, before, after, kind):
    # ADR 0011 D3: no automatic level change; the caller records and alerts. (was: one level down)
    t = transition(s(before, perps=True), trigger)
    assert (t.after.level, t.kind, t.after.status) == (before, "NOOP", ACTIVE)
    assert t.after.allow_perps is True


def test_promotion_off_l1_withdraws_the_perps_opt_in():
    # ADR 0011 D4: perps at any mode (was: leaving L1 withdrew the opt-in)
    t = transition(s(L1, perps=True), AutonomyTrigger.PROMOTION_CONFIRMED, totp_ok=True, gate=evaluate_gate(L1, L2_READY))
    assert (t.after.level, t.after.allow_perps, t.kind) == (L2, True, "PROMOTED")


@pytest.mark.parametrize("trigger,kind", [(AutonomyTrigger.HARD_STOP, "HALTED"), (AutonomyTrigger.BACKSTOP, "BACKSTOP")])
@pytest.mark.parametrize("before", [L1, L2, L3])
def test_hard_stop_and_backstop_halt_at_l1(trigger, kind, before):
    t = transition(s(before, perps=before == L1), trigger)
    # ADR 0011: level and markets stay (was: reset to L1 without perps, which 050's CHECK would refuse)
    assert (t.after.level, t.after.status, t.after.allow_perps, t.kind) == (before, HALTED, before == L1, kind)


def test_operator_demotion_needs_no_totp_but_not_below_l1():
    assert transition(s(L2), AutonomyTrigger.OPERATOR_DEMOTE).after.level == L1
    with pytest.raises(TransitionRefused):
        transition(s(L1), AutonomyTrigger.OPERATOR_DEMOTE)


def test_pause_and_resume():
    assert transition(s(L2), AutonomyTrigger.PAUSE).after.status is PAUSED
    with pytest.raises(TransitionRefused):
        transition(s(L1, HALTED), AutonomyTrigger.PAUSE)
    with pytest.raises(TransitionRefused) as e:
        transition(s(L1, HALTED), AutonomyTrigger.RESUME)
    assert e.value.code == "TOTP_REQUIRED"
    t = transition(s(L1, HALTED), AutonomyTrigger.RESUME, totp_ok=True)
    assert (t.after.level, t.after.status, t.kind) == (L1, ACTIVE, "RESUMED")
    with pytest.raises(TransitionRefused):
        transition(s(L2), AutonomyTrigger.RESUME, totp_ok=True)


@given(level=st.sampled_from(list(AutonomyLevel)), status=st.sampled_from([ACTIVE, HALTED, PAUSED]),
       trigger=st.sampled_from([t for t in AutonomyTrigger if t is not AutonomyTrigger.PROMOTION_CONFIRMED]),
       totp=st.booleans())
def test_no_trigger_but_a_confirmed_promotion_ever_raises_the_level(level, status, trigger, totp):
    try:
        t = transition(s(level, status, perps=level == L1), trigger, totp_ok=totp, gate=evaluate_gate(level, L2_READY))
    except TransitionRefused:
        return
    assert t.after.level <= t.before.level
    assert t.after.allow_perps == t.before.allow_perps  # ADR 0011 D4: no trigger touches the markets


# ------------------------------------------------------------------------------ gate evaluator


def test_l2_gate_passes_only_when_every_check_passes():
    assert evaluate_gate(L1, L2_READY).passed
    for change in ({"days_at_level": 29}, {"closed_trades": 29}, {"max_drawdown_pct": D("10.5")}, {"max_drawdown_pct": None},
                   {"breaches": 1}, {"sortino": 0.5}, {"sortino": None}, {"benchmark_sortino": None}, {"honest_paper": False}):
        r = evaluate_gate(L1, GateMetrics(**{**L2_READY.__dict__, **change}))
        assert not r.passed, change
        assert [c.name for c in r.checks if not c.passed]


def test_l3_gate_fails_closed_until_calibration_is_measured():
    assert evaluate_gate(L2, L3_READY).passed
    r = evaluate_gate(L2, GateMetrics(**{**L3_READY.__dict__, "calibration_error_pp": None}))
    assert not r.passed and next(c for c in r.checks if c.name == "calibration_error_pp").actual == "not measured"
    assert not evaluate_gate(L2, GateMetrics(**{**L3_READY.__dict__, "veto_rate_pct": D("10")})).passed
    assert evaluate_gate(L3, L3_READY).target_level is None


def test_sortino():
    assert sortino([0.01]) is None
    assert sortino([0.01, 0.02]) == math.inf
    assert sortino([0.02, -0.01, 0.01]) > sortino([0.01, -0.02, 0.0])
    assert period_returns([100, 110, 99]) == [0.1, -0.1]


# ------------------------------------------------------------------------------ database / API


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_autonomy") as url:
        yield url


@pytest.fixture
async def h(db_url, monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_TOTP", "true")  # 000000 in DEV; the real-TOTP path is in test_notifier.py
    monkeypatch.setenv("AI_ALLOW_DEV_TOTP", "true")  # /api/v1/ai refuses 000000 unless explicitly allowed
    harness = await make_harness(db_url)
    # ADR 0011 D11: creating a mandate starts it under the capital cap; these tests create several 10k budgets
    await harness.pool.execute("UPDATE paper_accounts SET total_equity = 1000000")
    try:
        yield harness
    finally:
        await close_harness(harness)


async def earn_l2_record(h, m, *, days=31, trades=30):
    """31 days at L1 with 30 closed trades, a max drawdown of ~5% and returns beating a falling BTC."""
    now = datetime.datetime.now(datetime.UTC)
    await h.pool.execute("UPDATE ai_mandates SET level_since = $2 WHERE id = $1", m.id, now - datetime.timedelta(days=days))
    equity, btc, hwm = 10000.0, 60000.0, 10000.0
    for i in range(days):
        equity *= 1.004 if i % 5 else 0.99
        btc *= 0.995 if i % 3 else 1.002
        hwm = max(hwm, equity)
        await h.pool.execute(
            "INSERT INTO ai_mandate_equity (mandate_id, ts, equity, hwm, drawdown_pct, benchmark_price) VALUES ($1, $2, $3, $4, $5, $6)",
            m.id, now - datetime.timedelta(days=days - i - 0.5), equity, hwm, (hwm - equity) / hwm * 100, btc)
    for _ in range(trades):
        cid = f"T-{uuid.uuid4().hex[:10]}"
        await h.pool.execute(
            """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type, quantity, filled_qty, status)
               VALUES ($1, $2, 'PAPER-ACCOUNT-001', 'BTCUSDT.BINANCE', 'SELL', 'MARKET', 0.001, 0.001, 'FILLED')""", cid, m.instance_id)
        await h.pool.execute(
            """INSERT INTO paper_trades (trade_id, account_id, client_order_id, instance_id, instrument_id, side, price, quantity, commission)
               VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'BTCUSDT.BINANCE', 'SELL', 60000, 0.001, 0.06)""", uuid.uuid4().hex, cid, m.instance_id)


async def test_gate_metrics_are_measured_from_the_database(h):
    m = await h.mandate(level=1)
    async with h.pool.acquire() as conn:
        empty = await gate_metrics(conn, m)
    assert (empty.closed_trades, empty.max_drawdown_pct, empty.sortino) == (0, None, None)
    await earn_l2_record(h, m)
    async with h.pool.acquire() as conn:
        mx = await gate_metrics(conn, m)
    assert mx.days_at_level >= 31 and mx.closed_trades == 30 and mx.max_drawdown_pct < 10
    assert mx.sortino > mx.benchmark_sortino and evaluate_gate(L1, mx).passed


async def test_the_system_proposes_and_only_the_operator_promotes_with_totp(h):
    m = await h.mandate(level=1)
    await h.pool.execute("INSERT INTO system_settings (key, value) VALUES ('autotrade.enabled', '1'::jsonb) "
                         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")  # ADR 0011 D10: raising the mode needs the switch
    await earn_l2_record(h, m)
    async with h.client() as c:
        view = (await c.post(f"/api/v1/ai/mandates/{m.id}/autonomy/evaluate", headers=h.headers)).json()
        assert view["level"] == 1 and view["requested_level"] == 2 and view["gate"]["passed"]
        pid = view["proposal"]["id"]
        assert (await c.post(f"/api/v1/ai/mandates/{m.id}/autonomy/evaluate", headers=h.headers)).json()["proposal"]["id"] == pid
        assert await h.pool.fetchval("SELECT count(*) FROM notifications_outbox WHERE kind = 'PROMOTION_PROPOSED'") >= 1

        no_totp = await c.post(f"/api/v1/ai/mandates/{m.id}/autonomy/confirm", json={"proposal_id": pid}, headers=h.headers)
        assert no_totp.status_code == 401
        assert (await load_mandate(h.pool, m.id)).autonomy_level == L1  # never silently

        ok = await c.post(f"/api/v1/ai/mandates/{m.id}/autonomy/confirm", json={"proposal_id": pid, "totp_code": "000000"},
                          headers=h.headers)
        assert ok.status_code == 200, ok.text
        assert ok.json()["level"] == 2 and ok.json()["proposal"] is None
    events = [(r["kind"], r["proposal_status"]) for r in
              await h.pool.fetch("SELECT kind, proposal_status FROM ai_autonomy_events WHERE mandate_id = $1 ORDER BY id", m.id)]
    assert ("PROPOSED", "CONFIRMED") in events and ("MODE_SET", None) in events  # was PROMOTED: an operator mode change is MODE_SET (050)


async def test_a_failing_gate_no_longer_blocks_the_confirmation(h):
    m = await h.mandate(level=1)
    await h.pool.execute("INSERT INTO system_settings (key, value) VALUES ('autotrade.enabled', '1'::jsonb) "
                         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")  # ADR 0011 D10: raising the mode needs the switch
    await earn_l2_record(h, m)
    async with h.pool.acquire() as conn:
        pid = await propose_if_ready(conn, m)
    await h.pool.execute("INSERT INTO ai_autonomy_events (mandate_id, kind, reason) VALUES ($1, 'BREACH', 'MANDATE_BUDGET_EXCEEDED')", m.id)
    async with h.client() as c:
        res = await c.post(f"/api/v1/ai/mandates/{m.id}/autonomy/confirm", json={"proposal_id": pid, "totp_code": "000000"},
                           headers=h.headers)
    assert res.status_code == 200 and res.json()["level"] == 2  # ADR 0011 D3: the gate is advisory, never a condition


async def test_a_voided_breach_stays_in_the_log_but_no_longer_counts(h):
    """Migration 045: a breach the system caused is set aside by a BREACH_VOIDED event; the BREACH row is kept."""
    from cryptonite_execution.mandate_store import BreachVoidError, void_breach

    m = await h.mandate(level=1)
    other = await h.mandate(level=1)
    bid = await h.pool.fetchval(
        """INSERT INTO ai_autonomy_events (mandate_id, kind, reason, detail)
           VALUES ($1, 'BREACH', 'MANDATE_POSITION_PCT_EXCEEDED', '{"command_id": "c-1"}') RETURNING id""", m.id)
    async with h.pool.acquire() as conn:
        assert (await gate_metrics(conn, m)).breaches == 1
        with pytest.raises(BreachVoidError):
            await void_breach(conn, m.id, bid, reason="  ", actor="operator:t")  # a reason is required
        with pytest.raises(BreachVoidError):
            await void_breach(conn, other.id, bid, reason="wrong mandate", actor="operator:t")
        vid = await void_breach(conn, m.id, bid, reason="orchestrator sizing conflict", actor="operator:t")
        assert (await gate_metrics(conn, m)).breaches == 0
        with pytest.raises(BreachVoidError):
            await void_breach(conn, m.id, bid, reason="again", actor="operator:t")
        with pytest.raises(BreachVoidError):  # only a BREACH can be voided
            await void_breach(conn, m.id, vid, reason="not a breach", actor="operator:t")
    kinds = [r["kind"] for r in await h.pool.fetch(
        "SELECT kind FROM ai_autonomy_events WHERE mandate_id = $1 ORDER BY id", m.id)]
    assert "BREACH" in kinds and kinds[-1] == "BREACH_VOIDED"
    with pytest.raises(asyncpg.UniqueViolationError):  # the database refuses a second voiding too
        await h.pool.execute(
            """INSERT INTO ai_autonomy_events (mandate_id, kind, reason, detail)
               VALUES ($1, 'BREACH_VOIDED', 'x', jsonb_build_object('voids', $2::text))""", m.id, str(bid))


async def test_a_breach_in_the_order_path_records_but_keeps_the_mode(h):
    m = await h.mandate(level=2, instruments=("ETHUSDT.BINANCE",))
    cid = uuid.uuid4()
    await h.pool.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, requester_id, payload, status,
               environment, account_id, mandate_id) VALUES ($1, $2, 'SUBMIT_ORDER', $3, 'ai:debate_worker', $4::jsonb, 'PENDING', 'PAPER',
               'PAPER-ACCOUNT-001', $5)""",
        cid, f"t:{cid}", m.instance_id,
        json.dumps({"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.001", "proposed_stop_loss": "49000"}),
        m.id)
    d, _ = make_fresh_dispatcher(starting_cash=D("100000"))
    engine = make_engine(d)
    engine.bridge = MagicMock()
    quotes = SimpleQuoteCache()
    quotes.set_quote("BTCUSDT.BINANCE", 50000.0, 50010.0)
    task = asyncio.create_task(command_poller(h.pool, engine, quotes))
    try:
        for _ in range(100):
            row = await h.pool.fetchrow("SELECT status, error_message FROM execution_commands WHERE command_id = $1", cid)
            if row["status"] == "REJECTED":
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert row["error_message"] == "MANDATE_INSTRUMENT_NOT_ALLOWED"
    assert (await load_mandate(h.pool, m.id)).autonomy_level == L2  # ADR 0011 D3: was demoted to L1
    kinds = [r["kind"] for r in await h.pool.fetch("SELECT kind FROM ai_autonomy_events WHERE mandate_id = $1 ORDER BY id", m.id)]
    assert kinds == ["BREACH"]


async def test_mandate_editor_totp_rules(h):
    body = {"name": f"alt-{uuid.uuid4().hex[:5]}", "capital_budget_usd": "5000", "instruments": ["ETHUSDT"], "max_position_pct": "10"}
    async with h.client() as c:
        assert (await c.post("/api/v1/ai/mandates", json=body, headers=h.headers)).status_code == 401  # creating adds room
        perps = await c.post("/api/v1/ai/mandates", json={**body, "name": body["name"] + "-p", "allow_perps": True,
                                                         "totp_code": "000000"}, headers=h.headers)
        assert perps.status_code == 201, perps.text  # ADR 0011 D4: perps at any mode
        assert (perps.json()["allow_perps"], perps.json()["max_leverage"], perps.json()["max_risk_per_trade_pct"]) == \
            (True, 3, "1")  # D2 default 3x, D6 default 1%
        # ADR 0011 D6: a coin has one owner; stop the perps profile so ETH is free for the next one
        assert (await c.post(f"/api/v1/ai/mandates/{perps.json()['id']}/stop", json={}, headers=h.headers)).json()["status"] == "STOPPED"
        assert (await c.post("/api/v1/ai/mandates", json={**body, "name": body["name"] + "-x", "max_leverage": 11,
                                                          "totp_code": "000000"}, headers=h.headers)).status_code == 422
        created = await c.post("/api/v1/ai/mandates", json={**body, "totp_code": "000000"}, headers=h.headers)
        assert created.status_code == 201, created.text
        mid = created.json()["id"]
        assert created.json()["autonomy_level"] == 1 and created.json()["instruments"] == ["ETHUSDT.BINANCE"]

        tighter = await c.patch(f"/api/v1/ai/mandates/{mid}", json={"capital_budget_usd": "4000", "max_position_pct": "5"}, headers=h.headers)
        assert tighter.status_code == 200 and tighter.json()["capital_budget_usd"] == "4000"
        looser = await c.patch(f"/api/v1/ai/mandates/{mid}", json={"capital_budget_usd": "9000"}, headers=h.headers)
        assert looser.status_code == 401
        more = await c.patch(f"/api/v1/ai/mandates/{mid}", json={"instruments": ["ETHUSDT", "BTCUSDT"], "totp_code": "000000"},
                             headers=h.headers)
        assert more.status_code == 200
        assert (await c.patch(f"/api/v1/ai/mandates/{mid}", json={"max_position_pct": "30"}, headers=h.headers)).status_code == 422

        paused = await c.post(f"/api/v1/ai/mandates/{mid}/pause", json={}, headers=h.headers)
        assert paused.json()["status"] == "PAUSED"
        assert (await c.post(f"/api/v1/ai/mandates/{mid}/resume", json={}, headers=h.headers)).status_code == 401
        resumed = await c.post(f"/api/v1/ai/mandates/{mid}/resume", json={"totp_code": "000000"}, headers=h.headers)
        assert resumed.json()["status"] == "ACTIVE"
        listed = (await c.get("/api/v1/ai/mandates")).json()["mandates"]
        assert any(x["id"] == mid for x in listed)
        status = (await c.get("/api/v1/ai/status")).json()
        assert status["exit_monitor_up"] is True and "Notifier down" in " ".join(status["degraded"])


async def test_changing_the_budget_rebases_the_high_water_mark(h):
    m = await h.mandate(level=1)
    before = await h.pool.fetchval("SELECT hwm_rebased_at FROM ai_mandates WHERE id = $1", m.id)
    async with h.client() as c:
        res = await c.patch(f"/api/v1/ai/mandates/{m.id}", json={"capital_budget_usd": "7000"}, headers=h.headers)
    assert res.status_code == 200
    assert await h.pool.fetchval("SELECT hwm_rebased_at FROM ai_mandates WHERE id = $1", m.id) > before
