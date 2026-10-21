"""Auto-trade Phase B (ADR 0010 amendment): debates propose perp trades, still at L1 with every entry approved with
TOTP. B0: a scanner debate carries its candidate's stop and target into ``route_entry``; B1: a spot verdict no spot
mandate takes goes to the single perps mandate allowing the perpetual; B2: the perp branch (BUY/SELL x flat/long/
short, the structural stop, the setup grade, the card dry run's sizing); B4: the approval's TOTP and the Inbox.

Runs on a throwaway migrated database and the Redis at REDIS_URL, as test_mandate_entry_filters does. No model, no
Binance: the perp mark is a Redis key and the leverage brackets are the BTC reference table.
"""

import importlib.util
import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from ai_api_harness import close_harness, make_harness
from migrated_db import migrated_database

WORKER = Path(__file__).resolve().parents[1] / "services" / "debate_worker"
sys.path.insert(0, str(WORKER))
SPOT = "BTCUSDT.BINANCE"
PERP = "BTCUSDT-PERP.BINANCE"
MARK_KEY = "perp:mark:BTCUSDT"
D = Decimal
BULL = {"trend_4h": "BULL", "trend_1h": "BULL", "trend_15m": "BULL", "adx": 30.0, "plus_di": 30.0, "minus_di": 10.0,
        "atr_pct": 1.0, "atr_pct_q90": 2.0}
BEAR = {**BULL, "trend_4h": "BEAR", "trend_1h": "BEAR", "trend_15m": "BEAR", "plus_di": 10.0, "minus_di": 30.0}
# A long candidate around the 60,000 decision price: stop 57,000 (5%), TP1 66,000 (R:R 2)
LONG_LEVELS = {"stop_loss": 57000.0, "take_profit": 66000.0}
SHORT_LEVELS = {"stop_loss": 63000.0, "take_profit": 54000.0}


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, WORKER / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def routing():
    return _load("debate_mandate_routing_perp", "mandate_routing.py")


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_debate_perp") as url:
        yield url


@pytest.fixture(autouse=True)
def fresh_routing_cache(routing):
    routing.reset_routing_cache()
    yield
    routing.reset_routing_cache()


@pytest.fixture
async def h(db_url, monkeypatch):
    monkeypatch.setenv("AI_ALLOW_DEV_TOTP", "false")
    monkeypatch.setenv("ALLOW_DEV_TOTP", "false")
    harness = await make_harness(db_url)
    await harness.pool.execute("DELETE FROM system_settings WHERE key = 'debate.perp_routing'")
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_orders, "
                               "paper_positions, ai_mandate_daily_sessions, debate_runs CASCADE")
    # $10,000 of equity: the dispatcher's 1% minimum order is $100, so the small test entries are placeable
    await harness.pool.execute("UPDATE paper_accounts SET total_equity = 10000, cash_balance = 10000 "
                               "WHERE account_id = 'PAPER-ACCOUNT-001'")
    await harness.redis.set(MARK_KEY, json.dumps({"type": "perp.mark", "mark_price": "60000", "funding_rate": "0.0001",
                                                  "ts_event_ns": 1}), ex=300)
    try:
        yield harness
    finally:
        await harness.redis.delete(MARK_KEY)
        await close_harness(harness)


async def perp_mandate(h, *, level: int = 1, max_leverage: int = 3, name: str | None = None):
    from cryptonite_execution.mandate_store import load_mandate
    from cryptonite_orchestrator.mandate_gate import ensure_mandate_instance

    mid = await h.pool.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, autonomy_level, status,
               allow_perps, max_leverage)
           VALUES ($1, 10000, ARRAY[$2], 25, $3, 'ACTIVE', true, $4) RETURNING id""",
        name or f"Brain {PERP} {uuid.uuid4().hex[:4]}", PERP, level, max_leverage)
    async with h.pool.acquire() as conn:
        m = await load_mandate(conn, mid)
        await ensure_mandate_instance(conn, m, "PAPER-ACCOUNT-001")
    return m


async def hold_perp(h, m, side: str, qty: str = "0.01") -> None:
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               quantity, current_price, leverage, margin_mode, status)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, $4, 60000, $5::numeric, 60000, 3, 'ISOLATED', 'OPEN')""",
        uuid.uuid4().hex, m.instance_id, PERP, side, qty)


async def route(h, routing, recommendation: str = "Buy", *, conviction: float = 0.8, allocation: float = 2.5,
                levels: dict | None = None, context: dict | None = None, **kw):
    run_id = uuid.uuid4()
    async with h.pool.acquire() as conn:
        status, mandate = await routing.route_entry(
            conn, run_id, SPOT, recommendation, conviction, allocation, reference_price=60000.0, redis=h.redis,
            grade_context=BULL if context is None else context, **(LONG_LEVELS if levels is None else levels), **kw)
    return status, mandate, run_id


async def approval_of(h, run_id) -> dict | None:
    row = await h.pool.fetchrow("SELECT action_class, payload, summary, timeout_policy FROM ai_approvals "
                                "WHERE source = 'debate' AND source_ref = $1", str(run_id))
    if row is None:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"]) if isinstance(d["payload"], str) else d["payload"]
    return d


# --- B1: spot -> perp mapping ---------------------------------------------------------------------------------------

async def test_a_spot_candidate_no_spot_mandate_takes_goes_to_the_perps_mandate(h, routing):
    m = await perp_mandate(h)
    async with h.pool.acquire() as conn:
        mandate, instrument = await routing.resolve_route(conn, SPOT)
    assert (mandate.id, instrument) == (m.id, PERP)
    status, mandate, run_id = await route(h, routing)
    assert (status, mandate.id) == ("PENDING_APPROVAL", m.id)
    assert (await approval_of(h, run_id))["payload"]["instrument_id"] == PERP


async def test_a_spot_mandate_is_preferred_over_a_perps_mandate(h, routing, caplog):
    spot = await h.mandate(level=1, instruments=(SPOT,))
    await perp_mandate(h)
    with caplog.at_level("INFO"):
        async with h.pool.acquire() as conn:
            mandate, instrument = await routing.resolve_route(conn, SPOT)
    assert (mandate.id, instrument) == (spot.id, SPOT)
    assert any("preferred over perps mandate" in r.message for r in caplog.records)


async def test_two_perps_mandates_are_ambiguous(h, routing):
    await perp_mandate(h)
    await perp_mandate(h)
    async with h.pool.acquire() as conn:
        assert await routing.resolve_route(conn, SPOT) == (None, SPOT)


async def test_an_explicit_mandate_wins(h, routing):
    spot = await h.mandate(level=1, instruments=(SPOT,))
    perp = await perp_mandate(h)
    async with h.pool.acquire() as conn:
        # named perps mandate: its perpetual, although a spot mandate allows the spot instrument
        assert (await routing.resolve_route(conn, SPOT, str(perp.id)))[0].id == perp.id
        assert (await routing.resolve_route(conn, SPOT, str(perp.id)))[1] == PERP
        assert (await routing.resolve_route(conn, SPOT, str(spot.id))) == (
            await routing.resolve_mandate(conn, SPOT, str(spot.id)), SPOT)
        other = await h.mandate(level=1, instruments=("ETHUSDT.BINANCE",))
        assert await routing.resolve_route(conn, SPOT, str(other.id)) == (None, SPOT)


async def test_a_paused_perps_mandate_takes_nothing(h, routing):
    m = await perp_mandate(h)
    await h.pool.execute("UPDATE ai_mandates SET status = 'PAUSED' WHERE id = $1", m.id)
    status, mandate, _ = await route(h, routing)
    assert (status, mandate) == ("PENDING_APPROVAL", None)  # outside any mandate: the operator's call, as before


# --- B2: the routing matrix -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("recommendation,held,expected", [
    ("Buy", None, ("ADD_RISK", "BUY", "LONG")),
    ("Buy", "LONG", ("ADD_RISK", "BUY", "LONG")),
    ("Buy", "SHORT", ("REDUCE_RISK", "BUY", None)),
    ("Sell", None, ("ADD_RISK", "SELL", "SHORT")),
    ("Sell", "SHORT", ("ADD_RISK", "SELL", "SHORT")),
    ("Sell", "LONG", ("REDUCE_RISK", "SELL", None)),
])
async def test_the_routing_matrix(h, routing, recommendation, held, expected):
    m = await perp_mandate(h)
    if held:
        await hold_perp(h, m, held)
    entry_short = expected[0] == "ADD_RISK" and expected[1] == "SELL"
    status, mandate, run_id = await route(h, routing, recommendation, levels=SHORT_LEVELS if entry_short else None,
                                          context=BEAR if entry_short else BULL)
    assert (status, mandate.id) == ("PENDING_APPROVAL", m.id)
    a = await approval_of(h, run_id)
    p = a["payload"]
    assert (a["action_class"], p["side"], p.get("perp_side")) == expected
    assert p["instrument_id"] == PERP and "leverage" not in p and "stop_price" not in p
    if a["action_class"] == "REDUCE_RISK":
        assert p["reduce_only"] is True and p["quantity"] == "0.01"
        assert a["timeout_policy"] == "PROCEED"  # L1 reduction: a short veto window that proceeds
        assert "allocation_pct" not in p
    else:
        assert a["timeout_policy"] == "DENY"  # an L1 entry: no answer means no
        assert p["margin_mode"] == "ISOLATED" and p["stop_loss"] == (63000.0 if entry_short else 57000.0)
        assert p["setup_grade"] in ("A", "B", "C") and p["grade"]["grade"] == p["setup_grade"]
        assert p["perp_plan"]["ok"] is True and p["perp_plan"]["side"] == expected[1]


async def test_the_entry_payload_and_summary_carry_the_dry_run(h, routing):
    m = await perp_mandate(h)
    status, _, run_id = await route(h, routing)
    p = (await approval_of(h, run_id))["payload"]
    plan = p["perp_plan"]
    # $250 margin; the mandate's 3x binds (grade B allows 5x: liquidity is unknown here, a weak input)
    assert (plan["leverage"], plan["binding_ceiling"], p["setup_grade"]) == (3, "mandate maximum", "B")
    assert "liquidity unknown" in p["grade_reasons"]
    assert D(plan["liquidation_price"]) < D("57000") and D(plan["loss_at_stop_usd"]) > 0
    assert p["allocation_pct"] == 2.5 and p["mandate_id"] == str(m.id)
    summary = (await approval_of(h, run_id))["summary"]
    assert summary.startswith(f"LONG {PERP} MARKET ISOLATED, dry run 3x (mandate maximum)")
    assert "grade B" in summary and "loss at stop $" in summary and "% of budget" in summary
    # the Action notification the notifier sends to Telegram
    assert await h.pool.fetchval("SELECT count(*) FROM notifications_outbox WHERE kind = 'APPROVAL'") == 1


async def test_a_margin_too_large_for_1x_is_cut_like_the_orchestrator_sizes_it(h, routing):
    await perp_mandate(h)
    status, _, run_id = await route(h, routing, allocation=25.0)
    p = (await approval_of(h, run_id))["payload"]
    assert status == "PENDING_APPROVAL" and p["perp_plan"]["margin_cut"] is True and p["allocation_pct"] < 25


# --- B2: the structural stop, the short structure and the grade -----------------------------------------------------

async def test_a_perp_entry_without_a_stop_and_target_is_refused(h, routing, caplog):
    await perp_mandate(h)
    with caplog.at_level("INFO"):
        status, mandate, run_id = await route(h, routing, levels={})
    assert status == "NO_TRADE" and mandate is not None
    assert await approval_of(h, run_id) is None
    assert any("STOP_REQUIRED" in r.message for r in caplog.records)


async def test_a_stop_on_the_wrong_side_of_the_entry_is_refused(h, routing, caplog):
    await perp_mandate(h)
    with caplog.at_level("INFO"):
        # the price fell through the candidate's stop since the bar closed: 61,000 / 66,000 around a 60,000 entry
        status, _, _ = await route(h, routing, levels={"stop_loss": 61000.0, "take_profit": 66000.0})
    assert status == "NO_TRADE"
    assert any("wrong side of the entry" in r.message for r in caplog.records)


async def test_a_short_from_long_levels_is_refused_no_short_structure(h, routing, caplog):
    await perp_mandate(h)
    with caplog.at_level("INFO"):
        status, _, run_id = await route(h, routing, "Sell", context=BEAR)  # the candidate's levels are a long's
    assert status == "NO_TRADE" and await approval_of(h, run_id) is None
    assert any("NO_SHORT_STRUCTURE" in r.message for r in caplog.records)


async def test_a_buy_with_short_levels_is_refused(h, routing):
    await perp_mandate(h)
    status, _, _ = await route(h, routing, "Buy", levels=SHORT_LEVELS)
    assert status == "NO_TRADE"


async def test_a_no_trade_grade_is_no_trade(h, routing, caplog):
    await perp_mandate(h)
    with caplog.at_level("INFO"):
        status, _, run_id = await route(h, routing, "Buy", context=BEAR)  # the 4h and the 1h trend against a long
    assert status == "NO_TRADE" and await approval_of(h, run_id) is None
    assert any("setup grade NO_TRADE" in r.message for r in caplog.records)


async def test_a_short_entry_is_graded_in_its_own_direction(h, routing):
    await perp_mandate(h)
    status, _, run_id = await route(h, routing, "Sell", levels=SHORT_LEVELS, context=BEAR)
    p = (await approval_of(h, run_id))["payload"]
    assert status == "PENDING_APPROVAL" and p["grade"]["inputs"]["side"] == "SHORT" and p["setup_grade"] == "B"
    assert D(p["perp_plan"]["liquidation_price"]) > D("63000")


async def test_no_mark_means_no_perp_entry(h, routing, caplog):
    await perp_mandate(h)
    await h.redis.delete(MARK_KEY)
    with caplog.at_level("INFO"):
        status, _, _ = await route(h, routing)
    assert status == "NO_TRADE" and any("NO_MARK_PRICE" in r.message for r in caplog.records)


async def test_a_mandate_maximum_above_the_room_still_fits_the_plan(h, routing):
    """The dry run's ceilings refuse what they cannot fit (here the room, after the mandate already holds its 25%)."""
    m = await perp_mandate(h)
    await hold_perp(h, m, "LONG", qty="0.05")  # $3,000 at 60,000: over the 25% position limit of $2,500
    status, _, _ = await route(h, routing)
    assert status == "NO_TRADE"


# --- Phase A still applies -----------------------------------------------------------------------------------------

async def test_the_session_latch_refuses_perp_entries_from_debates_but_reductions_pass(h, routing):
    m = await perp_mandate(h)
    await h.pool.execute(
        """INSERT INTO ai_mandate_daily_sessions (mandate_id, session_date, start_equity, start_budget, last_pnl,
               loss_latched_at)
           VALUES ($1, (now() AT TIME ZONE 'UTC')::date, 10000, 10000, 0, now())""", m.id)
    entry, _, entry_run = await route(h, routing)
    assert entry == "NO_TRADE" and await approval_of(h, entry_run) is None
    await hold_perp(h, m, "LONG")
    reduce, _, reduce_run = await route(h, routing, "Sell")
    assert reduce == "PENDING_APPROVAL" and (await approval_of(h, reduce_run))["action_class"] == "REDUCE_RISK"


async def test_min_conviction_and_min_rr_apply_to_perp_entries(h, routing):
    m = await perp_mandate(h)
    await h.pool.execute("UPDATE ai_mandates SET min_conviction = 0.7, min_rr = 2.5 WHERE id = $1", m.id)
    assert (await route(h, routing, conviction=0.6))[0] == "NO_TRADE"
    assert (await route(h, routing))[0] == "NO_TRADE"  # R:R 2.0 below 2.5
    await h.pool.execute("UPDATE ai_mandates SET min_rr = 2.0 WHERE id = $1", m.id)
    assert (await route(h, routing))[0] == "PENDING_APPROVAL"


# --- B0: the candidate's levels on spot ------------------------------------------------------------------------------

async def test_a_spot_entry_carries_the_candidate_levels_and_min_rr_measures_them(h, routing):
    m = await h.mandate(level=1, instruments=(SPOT,))
    await h.pool.execute("UPDATE ai_mandates SET min_rr = 2.0 WHERE id = $1", m.id)
    status, _, run_id = await route(h, routing)
    p = (await approval_of(h, run_id))["payload"]
    assert status == "PENDING_APPROVAL" and "filters_skipped" not in p
    assert (p["proposed_stop_loss"], p["proposed_take_profit"]) == (57000.0, 66000.0)
    # never a resting-stop field on spot: the dispatcher refuses it (SPOT_DERIVATIVE_FLAG_NOT_ALLOWED)
    assert "stop_price" not in p and "stop_loss" not in p
    assert (await route(h, routing, levels={"stop_loss": 57000.0, "take_profit": 63000.0}))[0] == "NO_TRADE"  # R:R 1


def test_candidate_levels_come_only_from_a_scanner_candidate():
    main = _load_main()
    candidate = {"candidate_id": str(uuid.uuid4()), "suggested_entry": "60000", "suggested_stop": "57000",
                 "suggested_target": "66000"}
    assert main.candidate_levels({"trigger_source": "scanner", "candidate_payload": json.dumps(candidate)}) == (
        57000.0, 66000.0)
    assert main.candidate_levels({"trigger_source": "brain_on_demand", "candidate_payload": json.dumps(candidate)}) == (
        None, None)
    assert main.candidate_levels({"trigger_source": "scanner"}) == (None, None)
    bad = {**candidate, "suggested_stop": "NaN"}
    assert main.candidate_levels({"trigger_source": "scanner", "candidate_payload": json.dumps(bad)}) == (None, None)


def _load_main():
    return _load("debate_worker_main_perp", "main.py")


# --- B4: the approval asks for TOTP in the web; the Inbox points to the queue -------------------------------------

async def test_approving_a_perp_entry_in_the_web_needs_totp(h, routing):
    await perp_mandate(h)
    _, _, run_id = await route(h, routing)
    approval_id = await h.pool.fetchval("SELECT id FROM ai_approvals WHERE source_ref = $1", str(run_id))
    async with h.client() as c:
        listed = (await c.get("/api/v1/ai/approvals")).json()["approvals"]
        assert [a["needs_totp"] for a in listed] == [True]
        url = f"/api/v1/ai/approvals/{approval_id}/decision"
        refused = await c.post(url, headers=h.headers, json={"decision": "APPROVE"})
        assert refused.status_code == 401 and refused.json()["detail"].startswith("TOTP_REQUIRED")
        ok = await c.post(url, headers=h.headers, json={"decision": "APPROVE", "totp_code": h.totp.now()})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "APPROVED" and ok.json()["command_id"]


async def test_a_veto_and_a_perp_reduction_need_no_totp(h, routing):
    m = await perp_mandate(h)
    _, _, entry_run = await route(h, routing)
    await hold_perp(h, m, "SHORT")
    _, _, reduce_run = await route(h, routing)  # a BUY against the short: a reduction
    ids = {r: await h.pool.fetchval("SELECT id FROM ai_approvals WHERE source_ref = $1", str(r))
           for r in (entry_run, reduce_run)}
    async with h.client() as c:
        veto = await c.post(f"/api/v1/ai/approvals/{ids[entry_run]}/decision", headers=h.headers,
                            json={"decision": "VETO"})
        reduce = await c.post(f"/api/v1/ai/approvals/{ids[reduce_run]}/decision", headers=h.headers,
                              json={"decision": "APPROVE"})
    assert veto.status_code == 200 and reduce.status_code == 200, (veto.text, reduce.text)


async def test_a_spot_approval_still_needs_no_totp(h, routing):
    await h.mandate(level=1, instruments=(SPOT,))
    _, _, run_id = await route(h, routing)
    approval_id = await h.pool.fetchval("SELECT id FROM ai_approvals WHERE source_ref = $1", str(run_id))
    async with h.client() as c:
        res = await c.post(f"/api/v1/ai/approvals/{approval_id}/decision", headers=h.headers,
                           json={"decision": "APPROVE"})
    assert res.status_code == 200, res.text


async def test_the_inbox_sends_a_perp_approval_to_the_queue(h, routing):
    await perp_mandate(h)
    run_id = uuid.uuid4()
    await h.pool.execute(
        """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at)
           VALUES ($1, $2, 'RUNNING', 'scanner', now())""", run_id, SPOT)
    await h.pool.execute(
        """INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_level, conviction_score,
               allocation_pct, reasoning, decision_price)
           VALUES ($1, $2, 'BUY', 'Buy', 0.8, 2.5, 'test', 60000)""", run_id, SPOT)
    async with h.pool.acquire() as conn:
        status, mandate = await routing.route_entry(conn, run_id, SPOT, "Buy", 0.8, 2.5, reference_price=60000.0,
                                                    redis=h.redis, grade_context=BULL, **LONG_LEVELS)
    await h.pool.execute("UPDATE debate_runs SET status = $2, mandate_id = $3 WHERE id = $1", run_id, status, mandate.id)
    async with h.client() as c:
        sizing = (await c.get(f"/api/v1/agents/debates/{run_id}/sizing")).json()
        approve = await c.post(f"/api/v1/agents/debates/{run_id}/approve", headers=h.headers, json={})
    assert sizing["code"] == "PERP_APPROVAL_IN_QUEUE", sizing
    assert approve.status_code == 409 and approve.json()["detail"].startswith("PERP_APPROVAL_IN_QUEUE")
    assert await h.pool.fetchval("SELECT status FROM ai_approvals WHERE source_ref = $1", str(run_id)) == "PENDING"


# --- B2: the orchestrator sizes a debate's perp command with plan_leveraged_order ---------------------------------

async def run_debate_command(payload: dict, m, requester: str):
    """The orchestrator's command poller on one approved SUBMIT_ORDER carrying ``payload`` (a fake connection, the real
    gate and dispatcher, mark and quote at 60,000), as test_leverage_orchestrator_sizing runs a Brain card's."""
    import asyncio
    from unittest.mock import MagicMock

    from cryptonite_orchestrator.server import SimpleQuoteCache, command_poller
    from test_ai_mandates import FakeConn, mandate_row
    from test_orchestrator_perp_wiring import make_engine
    from test_paper_risk_guardrails import make_perp_dispatcher

    engine = make_engine(make_perp_dispatcher(mark="60000"))
    engine.bridge = MagicMock()
    quotes = SimpleQuoteCache()
    quotes.set_quote(PERP, 60000.0, 60010.0)
    cid = uuid.uuid4()
    row = {"command_id": cid, "command_type": "SUBMIT_ORDER", "target_instance_id": m.instance_id,
           "requester_id": requester, "mandate_id": str(m.id),
           "payload": json.dumps({**payload, "approval_id": str(uuid.uuid4())})}
    approval = {"mandate_id": m.id, "action_class": "ADD_RISK", "status": "APPROVED", "command_id": cid}
    conn = FakeConn(row, mandate_row(m), (), approval)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    with pytest.raises(asyncio.CancelledError):
        await command_poller(pool, engine, quotes)
    return conn, engine


@pytest.mark.parametrize("requester", ["approval:web:operator:u1", "ai:debate_worker"])
async def test_the_orchestrator_sizes_a_debate_perp_entry_and_rests_its_stop(h, routing, requester):
    m = await perp_mandate(h)
    _, _, run_id = await route(h, routing)
    payload = (await approval_of(h, run_id))["payload"]
    assert "leverage" not in payload and "quantity" not in payload  # sized at execution, not by the debate
    conn, engine = await run_debate_command(payload, m, requester)
    assert conn.outcome() == ("COMPLETED", None)
    d = engine.dispatcher
    pos = d.perp_positions[PERP]
    plan = payload["perp_plan"]
    # the same plan as the dry run: 3x (the mandate's maximum), ISOLATED, in the mandate's own instance
    assert (pos.leverage, pos.margin_mode, pos.instance_id) == (plan["leverage"], "ISOLATED", m.instance_id)
    assert pos.signed_qty == D(plan["quantity"])
    (stop,) = d.resting_stops(PERP).values()
    assert (stop["side"], stop["intent"].stop_price, stop["quantity"]) == ("SELL", D("57000"), pos.signed_qty)


async def test_the_orchestrator_holds_a_debate_short_to_its_grade(h, routing):
    m = await perp_mandate(h, max_leverage=10)
    _, _, run_id = await route(h, routing, "Sell", levels=SHORT_LEVELS, context=BEAR)
    payload = (await approval_of(h, run_id))["payload"]
    assert payload["setup_grade"] == "B" and payload["perp_plan"]["leverage"] <= 5
    conn, engine = await run_debate_command(payload, m, "ai:debate_worker")
    assert conn.outcome() == ("COMPLETED", None)
    pos = engine.dispatcher.perp_positions[PERP]
    assert pos.signed_qty < 0 and pos.leverage <= 5  # grade B's ceiling binds below the mandate's 10x
    assert list(engine.dispatcher.resting_stops(PERP).values())[0]["side"] == "BUY"


async def test_the_orchestrator_refuses_a_debate_perp_entry_above_its_ceilings(h, routing):
    m = await perp_mandate(h)
    _, _, run_id = await route(h, routing)
    payload = (await approval_of(h, run_id))["payload"]
    # a grade ceiling of 0 (NO_TRADE), and a risk budget ($1 at stop) whose largest order is below the dispatcher's 1%
    # minimum: each refused by the plan itself, nothing placed
    tiny_risk = m.model_copy(update={"max_risk_per_trade_pct": D("0.01")})
    for changed, mandate in (({"setup_grade": "NO_TRADE"}, m), ({}, tiny_risk)):
        conn, engine = await run_debate_command({**payload, **changed}, mandate, "ai:debate_worker")
        status, reason = conn.outcome()
        assert status == "REJECTED" and "perp sizing" in reason, reason
        assert engine.dispatcher.open_orders == {} and engine.dispatcher.perp_positions == {}
    conn, engine = await run_debate_command({k: v for k, v in payload.items() if k != "stop_loss"}, m, "ai:debate_worker")
    assert conn.outcome()[0] == "REJECTED" and conn.outcome()[1].startswith("STOP_REQUIRED")


# --- review fix 1: the routing preference debate.perp_routing (ADR 0010 P-10) --------------------------------------------

SPOT_FIRST, PERP_FIRST = "SPOT_FIRST", "PERP_FIRST"


async def set_routing(h, routing, mode: str) -> None:
    await h.pool.execute(
        """INSERT INTO system_settings (key, value, updated_at) VALUES ('debate.perp_routing', $1::jsonb, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        json.dumps({SPOT_FIRST: "0", PERP_FIRST: "1"}.get(mode, mode)))
    routing.reset_routing_cache()


@pytest.mark.parametrize("mode", [SPOT_FIRST, PERP_FIRST])
@pytest.mark.parametrize("spots", [0, 1, 2])
@pytest.mark.parametrize("perps", [0, 1, 2])
async def test_the_routing_matrix_of_the_preference(h, routing, mode, spots, perps):
    spot_mandates = [await h.mandate(level=1, instruments=(SPOT,), name=f"spot-{i}-{uuid.uuid4().hex[:4]}")
                     for i in range(spots)]
    perp_mandates = [await perp_mandate(h) for _ in range(perps)]
    await set_routing(h, routing, mode)
    async with h.pool.acquire() as conn:
        mandate, instrument = await routing.resolve_route(conn, SPOT)
    if mode == PERP_FIRST and perps == 1:
        expected = (perp_mandates[0].id, PERP)  # the single perps mandate, although spot mandates match
    elif spots == 1:
        expected = (spot_mandates[0].id, SPOT)
    elif spots == 0 and perps == 1:
        expected = (perp_mandates[0].id, PERP)
    else:
        expected = (None, SPOT)  # none, or several of a kind: the operator decides
    assert (mandate.id if mandate else None, instrument) == expected


async def test_perp_first_routes_a_btc_candidate_to_the_perps_mandate_and_logs_it(h, routing, caplog):
    await h.mandate(level=1, instruments=(SPOT,))
    await h.mandate(level=1, instruments=(SPOT,), name=f"second-{uuid.uuid4().hex[:4]}")  # two spot mandates: ambiguous
    m = await perp_mandate(h)
    status, mandate, _ = await route(h, routing)
    assert (status, mandate) == ("PENDING_APPROVAL", None)  # SPOT_FIRST: the operator decides
    await set_routing(h, routing, PERP_FIRST)
    with caplog.at_level("INFO"):
        status, mandate, run_id = await route(h, routing)
    assert (status, mandate.id) == ("PENDING_APPROVAL", m.id)
    assert (await approval_of(h, run_id))["payload"]["instrument_id"] == PERP
    assert any("PERP_FIRST routing; routed to perps mandate" in r.message for r in caplog.records)


async def test_an_explicit_mandate_wins_under_perp_first(h, routing):
    spot = await h.mandate(level=1, instruments=(SPOT,))
    perp = await perp_mandate(h)
    await set_routing(h, routing, PERP_FIRST)
    async with h.pool.acquire() as conn:
        assert (await routing.resolve_route(conn, SPOT, str(spot.id)))[1] == SPOT
        assert (await routing.resolve_route(conn, SPOT, str(spot.id)))[0].id == spot.id
        assert (await routing.resolve_route(conn, SPOT, str(perp.id)))[0].id == perp.id
        assert (await routing.resolve_route(conn, SPOT))[0].id == perp.id  # and without a name, the perps mandate


async def test_the_routing_setting_is_cached_and_an_unreadable_one_is_spot_first(h, routing, monkeypatch, caplog):
    await set_routing(h, routing, PERP_FIRST)
    async with h.pool.acquire() as conn:
        assert await routing.routing_mode(conn) == PERP_FIRST
        await h.pool.execute("UPDATE system_settings SET value = '\"0\"' WHERE key = 'debate.perp_routing'")
        assert await routing.routing_mode(conn) == PERP_FIRST  # cached
        clock = routing.time.monotonic() + routing.ROUTING_CACHE_S + 1
        monkeypatch.setattr(routing.time, "monotonic", lambda: clock)
        assert await routing.routing_mode(conn) == SPOT_FIRST  # read again after the cache window
    assert routing.ROUTING_CACHE_S <= 60

    class Broken:
        def transaction(self):
            raise RuntimeError("database unavailable")

    routing.reset_routing_cache()
    with caplog.at_level("WARNING"):
        assert await routing.routing_mode(Broken()) == SPOT_FIRST
    assert "Could not read debate.perp_routing" in caplog.text
    # a stored value that is not 0 or 1 is not trusted either
    await set_routing(h, routing, "7")
    async with h.pool.acquire() as conn:
        assert await routing.routing_mode(conn) == SPOT_FIRST
    assert routing.read_routing_mode(None) == (SPOT_FIRST, None)
