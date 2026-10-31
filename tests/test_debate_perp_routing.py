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
    from cryptonite_execution import profile_lifecycle
    from cryptonite_schemas.profile import base_asset
    await harness.pool.execute("DELETE FROM ai_profile_coin_locks")
    await harness.pool.execute("INSERT INTO system_settings(key,value) VALUES('autotrade.enabled','1') "
                               "ON CONFLICT(key) DO UPDATE SET value='1'")
    profile_lifecycle.reset_switch_cache()
    original_mandate = harness.mandate

    async def owned_mandate(*args, **kw):
        m = await original_mandate(*args, **kw)
        for inst in m.instruments:
            await harness.pool.execute("INSERT INTO ai_profile_coin_locks(base_asset,mandate_id) VALUES($1,$2) "
                                       "ON CONFLICT DO NOTHING", base_asset(inst), m.id)
        return m

    harness.mandate = owned_mandate
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
               allow_perps, max_leverage, direction)
           VALUES ($1, 10000, ARRAY[$2], 25, $3, 'ACTIVE', true, $4, 'BOTH') RETURNING id""",
        name or f"Brain {PERP} {uuid.uuid4().hex[:4]}", PERP, level, max_leverage)
    async with h.pool.acquire() as conn:
        m = await load_mandate(conn, mid)
        await ensure_mandate_instance(conn, m, "PAPER-ACCOUNT-001")
    await h.pool.execute("INSERT INTO ai_profile_coin_locks(base_asset,mandate_id) VALUES('BTC',$1) ON CONFLICT DO NOTHING", mid)
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

async def test_a_spot_candidate_routes_to_its_perp_coin_owner(h, routing):
    m = await perp_mandate(h)
    async with h.pool.acquire() as conn:
        assert (await routing.resolve_profile(conn, SPOT)).id == m.id
    status, mandate, run_id = await route(h, routing)
    assert (status, mandate.id) == ("PENDING_APPROVAL", m.id)
    assert (await approval_of(h, run_id))["payload"]["instrument_id"] == PERP


async def test_a_spot_coin_owner_is_preferred_over_an_unlocked_perp_profile(h, routing):
    spot = await h.mandate(level=1, instruments=(SPOT,))
    await perp_mandate(h)
    async with h.pool.acquire() as conn:
        assert (await routing.resolve_profile(conn, SPOT)).id == spot.id


async def test_two_perp_profiles_use_the_exclusive_coin_owner(h, routing):
    owner = await perp_mandate(h)
    await perp_mandate(h)
    async with h.pool.acquire() as conn:
        assert (await routing.resolve_profile(conn, SPOT)).id == owner.id


async def test_an_explicit_profile_wins_resolution(h, routing):
    spot = await h.mandate(level=1, instruments=(SPOT,))
    perp = await perp_mandate(h)
    async with h.pool.acquire() as conn:
        assert (await routing.resolve_profile(conn, SPOT, str(perp.id))).id == perp.id
        assert (await routing.resolve_profile(conn, SPOT, str(spot.id))).id == spot.id
        other = await h.mandate(level=1, instruments=("ETHUSDT.BINANCE",))
        assert (await routing.resolve_profile(conn, SPOT, str(other.id))).id == other.id


async def test_a_paused_perp_profile_refuses_new_entries(h, routing):
    m = await perp_mandate(h)
    await h.pool.execute("UPDATE ai_mandates SET status = 'PAUSED' WHERE id = $1", m.id)
    status, mandate, _ = await route(h, routing)
    assert (status, mandate.id) == ("NO_TRADE", m.id)


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
    assert p["instrument_id"] == PERP and "stop_price" not in p
    command_type = await h.pool.fetchval("SELECT command_type FROM ai_approvals WHERE source_ref = $1", str(run_id))
    if a["action_class"] == "REDUCE_RISK":
        assert command_type == "CLOSE_POSITION" and "leverage" not in p  # capped to the live position at execution
        assert p["reduce_only"] is True and p["quantity"] == "0.01"
        assert a["timeout_policy"] == "PROCEED"  # L1 reduction: a short veto window that proceeds
        assert "allocation_pct" not in p
    else:
        assert a["timeout_policy"] == "DENY"  # an L1 entry: no answer means no
        assert command_type == "SUBMIT_ORDER" and p["leverage"] == p["perp_plan"]["leverage"]  # the approved ceiling
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
    assert any("NO_STRUCTURE" in r.message for r in caplog.records)


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

async def run_debate_command(payload: dict, m, requester: str, *, command_type: str = "SUBMIT_ORDER",
                             mark: str = "60000", held: str | None = None,
                             owner: str | None = None):
    """The orchestrator's command poller on one approved SUBMIT_ORDER carrying ``payload`` (a fake connection, the real
    gate and dispatcher, mark and quote at 60,000), as test_leverage_orchestrator_sizing runs a Brain card's."""
    import asyncio
    from unittest.mock import MagicMock

    from cryptonite_orchestrator.server import SimpleQuoteCache, command_poller
    from test_ai_mandates import FakeConn, mandate_row
    from test_orchestrator_perp_wiring import make_engine
    from test_paper_risk_guardrails import make_perp_dispatcher

    dispatcher = make_perp_dispatcher(mark=mark)
    if held is not None:  # the mandate's live perp position (signed quantity) when the command runs
        dispatcher.set_perp_position(PERP, D(held), D("60000"), leverage=3, margin_mode="ISOLATED",
                                     instance_id=owner or m.instance_id)
    engine = make_engine(dispatcher)
    engine.bridge = MagicMock()
    quotes = SimpleQuoteCache()
    quotes.set_quote(PERP, float(mark), float(mark) + 10.0)
    cid = uuid.uuid4()
    row = {"command_id": cid, "command_type": command_type, "target_instance_id": m.instance_id,
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
    assert "quantity" not in payload and payload["leverage"] == payload["perp_plan"]["leverage"]  # sized at execution
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


# --- review fixes -----------------------------------------------------------------------------------------------------
# Item 1: the routing preference (ADR 0010 P-10). Item 2: a perp reduction is a CLOSE_POSITION the orchestrator caps.
# Item 3: a routed run never turns into an unapproved order. Item 4: the approved leverage is a ceiling. Item 5: an
# entry another owner blocks is refused before the operator spends a TOTP.

async def test_retired_routing_setting_cannot_override_coin_ownership(h, routing):
    spot = await h.mandate(level=1, instruments=(SPOT,))
    await perp_mandate(h)
    await h.pool.execute("INSERT INTO system_settings(key,value) VALUES('debate.perp_routing','1') "
                         "ON CONFLICT(key) DO UPDATE SET value='1'")
    async with h.pool.acquire() as conn:
        assert (await routing.resolve_profile(conn, SPOT)).id == spot.id


# --- item 2: the reduction is a CLOSE_POSITION the orchestrator caps ------------------------------------------------------

async def routed_reduction(h, routing, held_side: str = "LONG", rec: str = "Sell"):
    m = await perp_mandate(h)
    await hold_perp(h, m, held_side, "0.01")
    _, _, run_id = await route(h, routing, rec)
    return m, (await approval_of(h, run_id))["payload"], run_id


async def test_a_reduction_is_a_close_position_with_the_veto_semantics_unchanged(h, routing):
    m, payload, run_id = await routed_reduction(h, routing)
    row = await h.pool.fetchrow("SELECT command_type, action_class, timeout_policy, expires_at, created_at "
                                "FROM ai_approvals WHERE source_ref = $1", str(run_id))
    assert (row["command_type"], row["action_class"], row["timeout_policy"]) == ("CLOSE_POSITION", "REDUCE_RISK", "PROCEED")
    assert (row["expires_at"] - row["created_at"]).total_seconds() == pytest.approx(300, abs=5)  # the 5-minute veto window
    assert payload["instrument_id"] == PERP and payload["quantity"] == "0.01" and payload["reduce_only"] is True
    async with h.client() as c:  # no TOTP for a reduction, as before
        approval_id = await h.pool.fetchval("SELECT id FROM ai_approvals WHERE source_ref = $1", str(run_id))
        res = await c.post(f"/api/v1/ai/approvals/{approval_id}/decision", headers=h.headers, json={"decision": "APPROVE"})
    assert res.status_code == 200, res.text
    cmd = await h.pool.fetchrow("SELECT command_type, payload FROM execution_commands WHERE command_id = $1",
                                uuid.UUID(res.json()["command_id"]))
    assert cmd["command_type"] == "CLOSE_POSITION"


async def test_a_position_that_shrank_during_the_veto_window_is_still_reduced_capped(h, routing):
    m, payload, _ = await routed_reduction(h, routing)  # routed against 0.01 held
    conn, engine = await run_debate_command(payload, m, "approval:web:operator:u1", command_type="CLOSE_POSITION",
                                            held="0.004")  # a TP1 half close left 0.004
    assert conn.outcome() == ("COMPLETED", None)
    assert PERP not in engine.dispatcher.perp_positions  # closed, capped at the live 0.004: no oversize, no flip
    evt = engine.bridge.enqueue_event.call_args.args[0]
    assert (evt["side"], evt["quantity"]) == ("SELL", "0.004")


async def test_a_short_that_shrank_is_covered_capped_and_never_flipped(h, routing):
    m, payload, _ = await routed_reduction(h, routing, "SHORT", "Buy")
    conn, engine = await run_debate_command(payload, m, "ai:debate_worker", command_type="CLOSE_POSITION", held="-0.003")
    assert conn.outcome() == ("COMPLETED", None)
    assert engine.dispatcher.get_perp_position_qty(PERP) == 0  # a reduce-only BUY of 0.003, not 0.01
    assert engine.bridge.enqueue_event.call_args.args[0]["side"] == "BUY"


async def test_a_position_closed_meanwhile_is_a_no_op_not_a_refusal(h, routing):
    m, payload, _ = await routed_reduction(h, routing)
    conn, engine = await run_debate_command(payload, m, "approval:web:operator:u1", command_type="CLOSE_POSITION")
    assert conn.outcome() == ("COMPLETED", None)  # not REDUCE_ONLY_OVERSIZE
    assert engine.dispatcher.perp_positions == {} and engine.dispatcher.open_orders == {}
    engine.bridge.enqueue_event.assert_not_called()  # nothing was filled: no new position, no alert


async def test_a_reduction_never_closes_the_other_side_opened_during_the_veto_window(h, routing, caplog):
    m, payload, _ = await routed_reduction(h, routing)  # reduces a LONG
    assert payload["position_side"] == "LONG"
    conn, engine = await run_debate_command(payload, m, "approval:web:operator:u1", command_type="CLOSE_POSITION",
                                            held="0.01")
    assert conn.outcome() == ("COMPLETED", None) and engine.dispatcher.get_perp_position_qty(PERP) == 0  # still long
    with caplog.at_level("WARNING"):  # the mandate flipped to a short meanwhile
        conn, engine = await run_debate_command(payload, m, "approval:web:operator:u1", command_type="CLOSE_POSITION",
                                                held="-0.01")
    assert conn.outcome() == ("COMPLETED", None)
    assert engine.dispatcher.get_perp_position_qty(PERP) == D("-0.01")  # untouched: no close, no flip
    engine.bridge.enqueue_event.assert_not_called()
    assert "skipped: reduces a LONG" in caplog.text
    # a command without position_side (exit monitor, kill switch, manual) keeps closing whatever is held
    legacy = {k: v for k, v in payload.items() if k != "position_side"}
    conn, engine = await run_debate_command(legacy, m, "approval:web:operator:u1", command_type="CLOSE_POSITION",
                                            held="-0.01")
    assert engine.dispatcher.get_perp_position_qty(PERP) == 0


async def test_a_short_reduction_is_held_to_the_short_side_the_same_way(h, routing):
    m, payload, _ = await routed_reduction(h, routing, "SHORT", "Buy")
    assert payload["position_side"] == "SHORT"
    conn, engine = await run_debate_command(payload, m, "ai:debate_worker", command_type="CLOSE_POSITION", held="0.01")
    assert conn.outcome() == ("COMPLETED", None) and engine.dispatcher.get_perp_position_qty(PERP) == D("0.01")


async def test_the_close_never_touches_a_perp_another_owner_holds(h, routing):
    m, payload, _ = await routed_reduction(h, routing)
    other = await perp_mandate(h)
    conn, engine = await run_debate_command(payload, m, "ai:debate_worker", command_type="CLOSE_POSITION", held="0.01",
                                            owner=other.instance_id)
    assert conn.outcome() == ("REJECTED", "CLOSE_NOT_OWNED")
    assert engine.dispatcher.get_perp_position_qty(PERP) == D("0.01")


# --- item 3: a routed-twice run is never "no approval needed" ------------------------------------------------------------

async def test_a_run_routed_twice_returns_the_existing_status_and_queues_nothing(h, routing, caplog):
    await h.mandate(level=1, instruments=(SPOT,))
    run_id = uuid.uuid4()

    async def once():
        async with h.pool.acquire() as conn:
            return await routing.route_entry(conn, run_id, SPOT, "Buy", 0.8, 2.5,
                                             reference_price=60000.0, stop_loss=57000.0)

    first = await once()
    assert first[0] == "PENDING_APPROVAL"
    with caplog.at_level("WARNING"):
        second = await once()
    assert second[0] == "PENDING_APPROVAL" and second[1].id == first[1].id
    assert "already routed" in caplog.text
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0  # the order waits for the approval
    approval_id = await h.pool.fetchval("SELECT id FROM ai_approvals WHERE source_ref = $1", str(run_id))
    async with h.client() as c:
        await c.post(f"/api/v1/ai/approvals/{approval_id}/decision", headers=h.headers, json={"decision": "VETO"})
    assert (await once())[0] == "NO_TRADE"  # a vetoed run stays vetoed
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_l3_still_queues_an_entry_without_an_approval(h, routing):
    m = await h.mandate(level=3, instruments=(SPOT,))
    run_id = uuid.uuid4()
    async with h.pool.acquire() as conn:
        status, mandate = await routing.route_entry(conn, run_id, SPOT, "Buy", 0.8, 2.5,
                                                    reference_price=60000.0, stop_loss=57000.0)
    assert (status, mandate.id) == ("COMPLETED", m.id)
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands WHERE idempotency_key = $1",
                                 f"debate:{run_id}") == 1


# --- item 4: the approved leverage is a ceiling ------------------------------------------------------------------------

async def test_executed_leverage_is_at_most_the_approved_one_when_the_price_moves_toward_the_stop(h, routing):
    m = await perp_mandate(h, max_leverage=10)
    _, _, run_id = await route(h, routing, allocation=8.0)  # a large margin: the risk at the stop binds the dry run
    payload = (await approval_of(h, run_id))["payload"]
    approved = payload["leverage"]
    assert approved == payload["perp_plan"]["leverage"] and payload["perp_plan"]["binding_ceiling"] == "risk at stop"
    # at 58,500 the stop (57,000) is closer: the risk ceiling the orchestrator computes now is higher
    uncapped, engine_u = await run_debate_command({k: v for k, v in payload.items() if k != "leverage"}, m,
                                                  "approval:web:operator:u1", mark="58500")
    assert uncapped.outcome() == ("COMPLETED", None) and engine_u.dispatcher.perp_positions[PERP].leverage > approved
    capped, engine_c = await run_debate_command(payload, m, "approval:web:operator:u1", mark="58500")
    assert capped.outcome() == ("COMPLETED", None)
    assert engine_c.dispatcher.perp_positions[PERP].leverage == approved  # a ceiling, clamped not refused
    # the other way (price away from the stop): the ceilings now allow less and the plan clamps, never refuses
    away, engine_a = await run_debate_command(payload, m, "approval:web:operator:u1", mark="63000")
    assert away.outcome() == ("COMPLETED", None) and engine_a.dispatcher.perp_positions[PERP].leverage <= approved


# --- item 5: another owner holds the perpetual ------------------------------------------------------------------------

async def test_a_perp_entry_is_refused_when_another_owner_holds_the_perpetual(h, routing, caplog):
    await perp_mandate(h)
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               quantity, current_price, leverage, margin_mode, status)
           VALUES ($1, 'PAPER-ACCOUNT-001', 'MANUAL_TRADE', $2, 'LONG', 60000, 0.01, 60000, 3, 'ISOLATED', 'OPEN')""",
        uuid.uuid4().hex, PERP)
    with caplog.at_level("INFO"):
        status, mandate, run_id = await route(h, routing)
    assert status == "NO_TRADE" and mandate is not None
    assert "INSTRUMENT_CONFLICT: MANDATE_INSTRUMENT_CONFLICT: MANUAL_TRADE holds BTCUSDT-PERP.BINANCE" in caplog.text
    assert await approval_of(h, run_id) is None


@pytest.fixture(autouse=True)
def _autotrade_switch_on(monkeypatch):
    """ADR 0011 D10 applies at runtime: with the master switch off a Veto window or Fully automatic mandate behaves as
    Ask me. These tests exercise the switch-on behaviour of those levels."""
    from cryptonite_execution import profile_lifecycle

    async def on(conn):
        return True

    monkeypatch.setattr(profile_lifecycle, "autotrade_switch_on", on)
