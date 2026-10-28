"""A5.2 replay: a mandate's 20%-class hard stop triggers, closes its positions through the normal command
path and latches; half the stop demotes once per episode; the account-wide backstop latches everything.

Runs against a throwaway database at Alembic head with the real orchestrator command poller.
"""

import asyncio
import datetime
import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_execution.mandates import MandateExposure, MandateOrder, check_mandate
from cryptonite_orchestrator.mandate_gate import ensure_mandate_instance
from cryptonite_orchestrator.mandate_monitor import evaluate_drawdown, mandate_monitor_once
from cryptonite_orchestrator.server import SimpleQuoteCache, command_poller
from cryptonite_schemas.mandate import AutonomyLevel, MandateReject, MandateStatus
from migrated_db import migrated_database, seed_paper_account
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_fresh_dispatcher

D = Decimal
SPOT = "BTCUSDT.BINANCE"
ACCOUNT = "PAPER-ACCOUNT-001"


def test_evaluate_drawdown_is_pure_and_tracks_the_high_water_mark():
    v = evaluate_drawdown(D("100"), None, D("20"))
    assert (v["hwm"], v["drawdown_pct"], v["hard_stop"]) == (D("100"), D("0"), False)
    v = evaluate_drawdown(D("90"), D("100"), D("20"))
    assert (v["drawdown_pct"], v["half_stop"], v["hard_stop"]) == (D("10"), True, False)
    v = evaluate_drawdown(D("80"), D("100"), D("20"))
    assert v["hard_stop"] is True
    assert evaluate_drawdown(D("120"), D("100"), D("20"))["hwm"] == D("120")


@pytest.fixture
def db_url():
    with migrated_database("cryptonite_hardstop") as url:
        yield url


@pytest.fixture
async def db(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await seed_paper_account(conn)
    try:
        yield pool
    finally:
        await pool.close()


def quotes_at(bid: float) -> SimpleQuoteCache:
    q = SimpleQuoteCache()
    q.set_quote(SPOT, bid, bid + 10)
    return q


async def make_mandate(pool, level: int, max_dd: str = "10", name: str | None = None, daily: str | None = "same"):
    """``daily``: the daily loss limit. Default: the hard stop itself, so the replay tests the drawdown stop (ADR 0011 D9
    gives every running profile a daily limit of 3 x risk, which would fire first); None: the D9 default."""
    async with pool.acquire() as conn:
        mid = await conn.fetchval(
            """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, max_drawdown_pct, autonomy_level,
                                        daily_loss_limit_pct)
               VALUES ($1, 10000, ARRAY['BTCUSDT.BINANCE'], 25, $2, $3, $4) RETURNING id""",
            name or f"m-{uuid.uuid4().hex[:6]}", D(max_dd), level, D(max_dd) if daily == "same" else (D(daily) if daily else None))
        m = await load_mandate(conn, mid)
        assert await ensure_mandate_instance(conn, m, ACCOUNT)
    return m


async def hold(pool, instance_id: str, qty: str, px: str, fee: str = "5"):
    """Book a filled BUY for the instance, as the bridge would."""
    cid = f"T-{uuid.uuid4().hex[:10]}"
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type, quantity,
                   filled_qty, avg_fill_price, status) VALUES ($1, $2, $3, $4, 'BUY', 'MARKET', $5, $5, $6, 'FILLED')""",
            cid, instance_id, ACCOUNT, SPOT, D(qty), D(px))
        await conn.execute(
            """INSERT INTO paper_trades (trade_id, account_id, client_order_id, instance_id, instrument_id, side, price, quantity, commission)
               VALUES ($1, $2, $3, $4, $5, 'BUY', $6, $7, $8)""",
            uuid.uuid4().hex, ACCOUNT, cid, instance_id, SPOT, D(px), D(qty), D(fee))
        await conn.execute(
            """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price, quantity, current_price, status)
               VALUES ($1, $2, $3, $4, 'LONG', $5, $6, $5, 'OPEN')""",
            uuid.uuid4().hex, ACCOUNT, instance_id, SPOT, D(px), D(qty))


def engine_holding(qty: str):
    d, _ = make_fresh_dispatcher(starting_cash=D("100000"))
    d.positions[SPOT] = D(qty)
    engine = make_engine(d)
    engine.bridge = MagicMock()
    return engine


async def run_poller_until_done(pool, engine, quotes, key_prefix: str):
    task = asyncio.create_task(command_poller(pool, engine, quotes))
    try:
        for _ in range(100):
            status = await pool.fetchval(
                "SELECT status FROM execution_commands WHERE idempotency_key LIKE $1 || '%' ORDER BY created_at DESC LIMIT 1", key_prefix)
            if status in ("COMPLETED", "REJECTED"):
                return status
            await asyncio.sleep(0.05)
        raise AssertionError("close command was not processed")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_replay_hard_stop_triggers_flattens_and_latches(db):
    pool = db
    m = await make_mandate(pool, level=3)
    await hold(pool, m.instance_id, "0.1", "50000")  # equity = 10,000 - 5,005 + 0.1 * bid
    engine = engine_holding("0.6")  # 0.1 is the mandate's; 0.5 belongs to a manual trade
    redis = AsyncMock()
    t0 = datetime.datetime.now(datetime.UTC)

    async def step(bid: float, i: int):
        await mandate_monitor_once(engine, pool, redis, quotes_at(bid), now=t0 + datetime.timedelta(seconds=61 * i))
        async with pool.acquire() as conn:
            return await load_mandate(conn, m.id)

    assert (await step(50000, 1)).autonomy_level == AutonomyLevel.L3
    assert (await step(52000, 2)).autonomy_level == AutonomyLevel.L3  # new HWM 10,195
    # 46,500: equity 9,645, drawdown 5.39% >= half of 10% -> an alert once per episode, the mode stays (ADR 0011 D3)
    assert (await step(46500, 3)).autonomy_level == AutonomyLevel.L3
    assert (await step(46400, 4)).autonomy_level == AutonomyLevel.L3
    half_alerts = [json.loads(c.args[1]) for c in redis.publish.call_args_list if json.loads(c.args[1]).get("kind") == "MANDATE_HALF_DRAWDOWN"]
    assert len(half_alerts) == 1
    # 30,000: equity 7,995, drawdown 21.6% >= 10% -> HALTED (mode kept), close queued
    halted = await step(30000, 5)
    assert (halted.status, halted.autonomy_level) == (MandateStatus.HALTED, AutonomyLevel.L3)

    cmd = await pool.fetchrow("SELECT * FROM execution_commands WHERE command_type = 'CLOSE_POSITION' AND mandate_id = $1", m.id)
    payload = json.loads(cmd["payload"])
    assert (cmd["target_instance_id"], cmd["requester_id"], cmd["status"]) == (m.instance_id, "risk:mandate_monitor", "PENDING")
    assert D(payload["quantity"]) == D("0.1") and payload["reason"] == "HARD_STOP"
    kinds = [r["kind"] for r in await pool.fetch("SELECT kind FROM ai_autonomy_events WHERE mandate_id = $1 ORDER BY id", m.id)]
    assert kinds == ["HALTED"]
    published = [json.loads(c.args[1]) for c in redis.publish.call_args_list]
    assert any(p["kind"] == "MANDATE_HARD_STOP" and p["severity"] == "CRITICAL" for p in published)
    assert await pool.fetchval("SELECT count(*) FROM ai_mandate_equity WHERE mandate_id = $1", m.id) >= 4

    # The real command poller closes only the mandate's 0.1, under the mandate's instance
    status = await run_poller_until_done(pool, engine, quotes_at(30000), f"mandate_stop:{m.instance_id}")
    assert status == "COMPLETED"
    assert engine.dispatcher.get_position_qty(SPOT) == D("0.5")
    fill = engine.bridge.enqueue_event.call_args.args[0]
    assert (fill["instance_id"], fill["side"], D(fill["quantity"])) == (m.instance_id, "SELL", D("0.1"))

    # Latched: a recovery does not un-halt it, and new risk is refused
    again = await step(60000, 6)
    assert again.status is MandateStatus.HALTED
    buy = MandateOrder(instrument_id=SPOT, side="BUY", quantity=D("0.001"), ref_price=D("60000"))
    assert check_mandate(again, buy, MandateExposure(), approval_ok=True) is MandateReject.HALTED


async def test_the_default_daily_limit_fires_before_the_hard_stop_when_it_is_not_raised(db):
    """ADR 0011 D9: a running profile always has a daily loss limit (3 x risk by default), so a 5% fall flattens the
    day long before the 10% hard stop; the profile stays ACTIVE and its mode is untouched."""
    pool = db
    m = await make_mandate(pool, level=3, daily=None)
    assert m.daily_loss_limit_pct == D("3")
    await hold(pool, m.instance_id, "0.1", "50000")
    engine = engine_holding("0.1")
    redis = AsyncMock()
    t0 = datetime.datetime.now(datetime.UTC)
    for i, bid in enumerate((50000, 46500), start=1):
        await mandate_monitor_once(engine, pool, redis, quotes_at(bid), now=t0 + datetime.timedelta(seconds=61 * i))
    cur = await load_mandate(pool, m.id)
    assert (cur.status, cur.autonomy_level) == (MandateStatus.ACTIVE, AutonomyLevel.L3)
    cmd = await pool.fetchrow("SELECT payload FROM execution_commands WHERE command_type = 'CLOSE_POSITION' AND mandate_id = $1", m.id)
    assert json.loads(cmd["payload"])["reason"] == "DAILY_LOSS_LIMIT"
    assert await pool.fetchval("SELECT count(*) FROM ai_mandate_daily_sessions WHERE mandate_id = $1 AND loss_latched_at IS NOT NULL", m.id) == 1


async def test_account_backstop_latches_everything(db):
    pool = db
    a = await make_mandate(pool, level=2, max_dd="20")
    b = await make_mandate(pool, level=1, max_dd="20")
    await hold(pool, a.instance_id, "0.1", "50000")
    engine = engine_holding("0.1")
    redis = AsyncMock()
    now = datetime.datetime.now(datetime.UTC)
    await mandate_monitor_once(engine, pool, redis, quotes_at(50000), now=now)
    assert await pool.fetchval("SELECT count(*) FROM ai_mandate_equity WHERE mandate_id IS NULL") == 1

    engine.dispatcher.cash_balance = D("75000")  # account equity ~80,000 vs HWM ~105,000: > 20% down
    await mandate_monitor_once(engine, pool, redis, quotes_at(50000), now=now + datetime.timedelta(seconds=5))

    assert engine.kill_switch.is_latched
    ks = await pool.fetchrow("SELECT is_latched, latched_by FROM execution_kill_switch WHERE environment = 'PAPER' AND account_id = $1", ACCOUNT)
    assert (ks["is_latched"], ks["latched_by"]) == (True, "risk:account_backstop")
    async with pool.acquire() as conn:
        for m in (a, b):
            cur = await load_mandate(conn, m.id)
            assert (cur.status, cur.autonomy_level) == (MandateStatus.HALTED, m.autonomy_level)  # the mode is kept
    assert await pool.fetchval("SELECT count(*) FROM ai_autonomy_events WHERE kind = 'BACKSTOP'") == 2
    assert await pool.fetchval(
        "SELECT count(*) FROM execution_commands WHERE command_type = 'CLOSE_POSITION' AND target_instance_id = $1", a.instance_id) == 1
    assert any(json.loads(c.args[1])["kind"] == "ACCOUNT_BACKSTOP_DRAWDOWN" for c in redis.publish.call_args_list)

    # A second pass does not queue a second close or re-alert
    redis.publish.reset_mock()
    await mandate_monitor_once(engine, pool, redis, quotes_at(50000), now=now + datetime.timedelta(seconds=10))
    assert await pool.fetchval(
        "SELECT count(*) FROM execution_commands WHERE command_type = 'CLOSE_POSITION' AND target_instance_id = $1", a.instance_id) == 1
    assert redis.publish.call_count == 0


async def queue(pool, m, payload, key=None):
    cid = uuid.uuid4()
    await pool.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, requester_id, payload,
               status, environment, account_id, mandate_id) VALUES ($1, $2, $3, $4, 'ai:debate_worker', $5::jsonb, 'PENDING', 'PAPER', $6, $7)""",
        cid, key or f"t:{cid}", payload.pop("_type", "SUBMIT_ORDER"), m.instance_id, json.dumps({**payload, "mandate_id": str(m.id)}),
        ACCOUNT, m.id)
    return cid


async def drain(pool, engine, quotes, command_ids):
    task = asyncio.create_task(command_poller(pool, engine, quotes))
    try:
        for _ in range(200):
            rows = await pool.fetch("SELECT command_id, status, error_message FROM execution_commands WHERE command_id = ANY($1::uuid[])",
                                    command_ids)
            if all(r["status"] in ("COMPLETED", "REJECTED") for r in rows):
                return {r["command_id"]: (r["status"], r["error_message"]) for r in rows}
            await asyncio.sleep(0.05)
        raise AssertionError("commands not processed")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_back_to_back_orders_cannot_overrun_the_mandate_before_the_bridge_catches_up(db):
    """Review finding: exposure must include fills the event bridge has not written to paper_positions yet."""
    m = await make_mandate(db, level=3)
    await db.execute("UPDATE ai_mandates SET capital_budget_usd = 1000 WHERE id = $1", m.id)  # 25% cap = 250 USD
    engine = engine_holding("0")  # bridge mocked: paper_positions never updated
    first = await queue(db, m, {"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.004",
                                "proposed_stop_loss": "49000"})  # ~205 USD
    second = await queue(db, m, {"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.004",
                                 "proposed_stop_loss": "49000"})
    out = await drain(db, engine, quotes_at(50000), [first, second])
    assert out[first] == ("COMPLETED", None)
    assert out[second] == ("REJECTED", "MANDATE_POSITION_PCT_EXCEEDED")


async def test_a_mandate_close_never_sells_more_than_the_mandate_owns(db):
    """Review finding: a stale CLOSE_POSITION quantity must not sell another owner's coins."""
    m = await make_mandate(db, level=1)
    await hold(db, m.instance_id, "0.1", "50000")
    engine = engine_holding("0.6")  # 0.5 is a manual holding
    stale = await queue(db, m, {"_type": "CLOSE_POSITION", "instrument_id": SPOT, "quantity": "1.0", "reason": "EXIT_DEBATE_CLOSE"})
    await drain(db, engine, quotes_at(50000), [stale])
    assert engine.dispatcher.get_position_qty(SPOT) == D("0.5")
    again = await queue(db, m, {"_type": "CLOSE_POSITION", "instrument_id": SPOT, "quantity": "0.1", "reason": "retry"})
    assert (await drain(db, engine, quotes_at(50000), [again]))[again] == ("COMPLETED", None)
    assert engine.dispatcher.get_position_qty(SPOT) == D("0.5")  # nothing of its own left: a no-op


@pytest.fixture(autouse=True)
def _autotrade_switch_on(monkeypatch):
    """ADR 0011 D10 applies at runtime: with the master switch off a Veto window or Fully automatic mandate behaves as
    Ask me. These tests exercise the switch-on behaviour of those levels."""
    from cryptonite_execution import profile_lifecycle
    from cryptonite_orchestrator import mandate_gate, server

    async def on(conn):
        return True

    monkeypatch.setattr(profile_lifecycle, "autotrade_switch_on", on)
    monkeypatch.setattr(mandate_gate, "fresh_autotrade_switch_on", on)
    monkeypatch.setattr(server, "fresh_autotrade_switch_on", on)
