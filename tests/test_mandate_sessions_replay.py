"""ADR 0010 replay: a mandate's daily session on a throwaway database at Alembic head, with the real mandate monitor,
the real command poller and the real mandate gate.

- start -> profit target (Info alert, ADD_RISK approvals cancelled, the instance in mandate:session:target_met until the
  next 00:00 UTC) -> profit lock (flatten, Critical alert);
- start -> daily loss (flatten, Critical alert) -> an approved BUY refused -> a SELL passes -> the close runs -> the
  next UTC day is ACTIVE again from a new start equity;
- repeated ticks queue no second close and raise no second alert; a latch never changes the status or the level and
  records no breach; a budget edit during the day is not P&L.
"""

import datetime
import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import asyncpg
import pytest
from cryptonite_execution.mandate_store import load_mandate, session_verdict_for
from cryptonite_orchestrator.mandate_gate import ensure_mandate_instance
from cryptonite_orchestrator.mandate_monitor import (
    SESSION_TARGET_MET_KEY,
    mandate_monitor_once,
    session_pnl,
)
from cryptonite_schemas.mandate import AutonomyLevel, MandateStatus
from cryptonite_schemas.session import SessionVerdict, session_start, utc_session_date
from migrated_db import migrated_database, seed_paper_account
from test_mandate_hard_stop import drain, engine_holding, hold, queue, quotes_at

D = Decimal
ACCOUNT = "PAPER-ACCOUNT-001"
# The gate reads today's session with the real clock, so the replay runs on today's UTC date
DAY_START = session_start(utc_session_date(datetime.datetime.now(datetime.UTC)))


def test_session_pnl_ignores_a_budget_edit():
    # equity 9,995 at the start on a 10,000 budget; the budget halves, equity drops 5,000 with it: no P&L
    assert session_pnl(D("4995"), D("5000"), D("9995"), D("10000")) == D("0")
    assert session_pnl(D("10195"), D("10000"), D("9995"), D("10000")) == D("200")


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_sessions") as url:
        yield url


@pytest.fixture
async def db(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE execution_commands, ai_approvals, notifications_outbox, paper_positions, "
                           "paper_trades, paper_orders, ai_mandate_equity, ai_autonomy_events, ai_mandates CASCADE")
        await seed_paper_account(conn)
    try:
        yield pool
    finally:
        await pool.close()


async def make_mandate(pool, **limits):
    cols = ", ".join(limits)
    params = ", ".join(f"${i + 2}" for i in range(len(limits)))
    async with pool.acquire() as conn:
        mid = await conn.fetchval(
            f"""INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, max_drawdown_pct,
                    autonomy_level{', ' + cols if cols else ''})
                VALUES ($1, 10000, ARRAY['BTCUSDT.BINANCE'], 25, 10, 1{', ' + params if params else ''}) RETURNING id""",
            f"s-{uuid.uuid4().hex[:6]}", *(D(v) for v in limits.values()))
        m = await load_mandate(conn, mid)
        assert await ensure_mandate_instance(conn, m, ACCOUNT)
    return m


async def pending_add_risk(pool, m) -> uuid.UUID:
    return await pool.fetchval(
        """INSERT INTO ai_approvals (mandate_id, action_class, command_type, payload, summary, source, source_ref,
               level_at_creation, timeout_policy, expires_at)
           VALUES ($1, 'ADD_RISK', 'SUBMIT_ORDER', '{}'::jsonb, 'buy', 'debate', $2, 1, 'DENY', now() + interval '15 minutes')
           RETURNING id""", m.id, str(uuid.uuid4()))


def alerts(redis, kind: str) -> list[dict]:
    return [p for p in (json.loads(c.args[1]) for c in redis.publish.call_args_list) if p.get("kind") == kind]


async def session_row(pool, m, day=None):
    return await pool.fetchrow("SELECT * FROM ai_mandate_daily_sessions WHERE mandate_id = $1 AND session_date = $2",
                               m.id, day or DAY_START.date())


async def closes(pool, m) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM execution_commands WHERE command_type = 'CLOSE_POSITION' AND mandate_id = $1", m.id)


async def test_replay_target_then_profit_lock_flattens_once(db):
    pool = db
    m = await make_mandate(pool, daily_loss_limit_pct="3", daily_profit_target_pct="2", profit_lock_pct="50")
    await hold(pool, m.instance_id, "0.1", "50000")  # equity = 10,000 - 5,005 + 0.1 * bid
    engine = engine_holding("0.1")
    redis = AsyncMock()
    approval = await pending_add_risk(pool, m)

    async def tick(bid: float, i: int) -> None:
        await mandate_monitor_once(engine, pool, redis, quotes_at(bid),
                                   now=DAY_START + datetime.timedelta(minutes=5, seconds=61 * i))

    await tick(50000, 0)  # no snapshot before 00:00: the day starts from the equity now, 9,995
    row = await session_row(pool, m)
    assert (row["start_equity"], row["start_budget"], row["last_pnl"]) == (D("9995.00"), D("10000.00"), D("0.00"))
    async with pool.acquire() as conn:
        assert await session_verdict_for(conn, m, datetime.datetime.now(datetime.UTC)) is SessionVerdict.ACTIVE

    await tick(52000, 1)  # equity 10,195: P&L +200 = 2% of the budget -> target met
    row = await session_row(pool, m)
    assert row["target_hit_at"] is not None and row["last_pnl"] == D("200.00")
    assert len(alerts(redis, "MANDATE_DAILY_TARGET")) == 1
    assert alerts(redis, "MANDATE_DAILY_TARGET")[0]["severity"] == "INFO"
    assert await pool.fetchval("SELECT status FROM ai_approvals WHERE id = $1", approval) == "CANCELLED"
    redis.sadd.assert_awaited_with(SESSION_TARGET_MET_KEY, m.instance_id)
    assert redis.expireat.await_args.args == (SESSION_TARGET_MET_KEY,
                                              int((DAY_START + datetime.timedelta(days=1)).timestamp()))
    assert await closes(pool, m) == 0  # positions kept
    async with pool.acquire() as conn:
        assert await session_verdict_for(conn, m, datetime.datetime.now(datetime.UTC)) is SessionVerdict.TARGET_MET

    await tick(52100, 2)  # still above: no second alert
    assert len(alerts(redis, "MANDATE_DAILY_TARGET")) == 1
    assert (await session_row(pool, m))["peak_pnl"] == D("210.00")

    await tick(50900, 3)  # P&L +90 <= 50% of the $200 target -> the lock flattens
    row = await session_row(pool, m)
    assert row["lock_fired_at"] is not None
    assert await closes(pool, m) == 1
    cmd = await pool.fetchrow("SELECT payload, requester_id FROM execution_commands WHERE mandate_id = $1", m.id)
    payload = json.loads(cmd["payload"])
    assert (payload["reason"], payload["reduce_only"], D(payload["quantity"])) == ("PROFIT_LOCK", True, D("0.1"))
    assert [a["severity"] for a in alerts(redis, "MANDATE_PROFIT_LOCK")] == ["CRITICAL"]

    # Repeated ticks, even back above the target: no second close, no second alert, still locked
    await tick(52500, 4)
    await tick(50000, 5)
    assert await closes(pool, m) == 1
    assert len(alerts(redis, "MANDATE_PROFIT_LOCK")) == 1
    assert await pool.fetchval("SELECT count(*) FROM notifications_outbox WHERE kind = 'MANDATE_PROFIT_LOCK'") == 1
    async with pool.acquire() as conn:
        assert await session_verdict_for(conn, m, datetime.datetime.now(datetime.UTC)) is SessionVerdict.LOCK_FIRED
        after = await load_mandate(conn, m.id)
    # a session latch is not a halt, a demotion or a breach
    assert (after.status, after.autonomy_level) == (MandateStatus.ACTIVE, AutonomyLevel.L1)
    assert await pool.fetchval("SELECT count(*) FROM ai_autonomy_events WHERE mandate_id = $1", m.id) == 0


async def test_replay_daily_loss_flattens_refuses_entries_and_clears_next_day(db):
    pool = db
    m = await make_mandate(pool, daily_loss_limit_pct="3")
    await hold(pool, m.instance_id, "0.1", "50000")
    engine = engine_holding("0.1")
    redis = AsyncMock()
    # yesterday's last snapshot: the day starts from 9,995 even though the first tick is later
    await pool.execute("""INSERT INTO ai_mandate_equity (mandate_id, ts, equity, hwm, drawdown_pct)
                          VALUES ($1, $2, 9995, 9995, 0)""", m.id, DAY_START - datetime.timedelta(minutes=1))

    async def tick(bid: float, i: int, day: int = 0) -> None:
        await mandate_monitor_once(engine, pool, redis, quotes_at(bid),
                                   now=DAY_START + datetime.timedelta(days=day, minutes=5, seconds=61 * i))

    await tick(49000, 0)  # P&L -100
    assert (await session_row(pool, m))["start_equity"] == D("9995.00")
    approval = await pending_add_risk(pool, m)
    await tick(47000, 1)  # equity 9,695: P&L -300 = -3% of the budget -> latched
    row = await session_row(pool, m)
    assert row["loss_latched_at"] is not None and row["last_pnl"] == D("-300.00")
    assert await pool.fetchval("SELECT status FROM ai_approvals WHERE id = $1", approval) == "CANCELLED"
    assert [a["severity"] for a in alerts(redis, "MANDATE_DAILY_LOSS")] == ["CRITICAL"]
    assert await closes(pool, m) == 1
    close_key = await pool.fetchval("SELECT idempotency_key FROM execution_commands WHERE mandate_id = $1", m.id)

    # Repeated ticks (even after a recovery): no second close while the first is pending, no second alert
    await tick(48000, 2)
    await tick(51000, 3)
    assert await closes(pool, m) == 1
    assert len(alerts(redis, "MANDATE_DAILY_LOSS")) == 1

    # An approved BUY executed now is refused
    buy = await queue(pool, m, {"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.001"})
    await pool.execute(
        """INSERT INTO ai_approvals (mandate_id, action_class, command_type, payload, summary, source, source_ref,
               level_at_creation, timeout_policy, expires_at, status, command_id)
           VALUES ($1, 'ADD_RISK', 'SUBMIT_ORDER', '{}'::jsonb, 'buy', 'debate', $2, 1, 'DENY',
                   now() + interval '15 minutes', 'APPROVED', $3)""", m.id, str(uuid.uuid4()), buy)
    approval_id = await pool.fetchval("SELECT id FROM ai_approvals WHERE command_id = $1", buy)
    await pool.execute("UPDATE execution_commands SET payload = payload || jsonb_build_object('approval_id', $2::text) "
                       "WHERE command_id = $1", buy, str(approval_id))
    # The poller runs the latch's own close (created first) and refuses the approved BUY
    close_id = await pool.fetchval("SELECT command_id FROM execution_commands WHERE idempotency_key = $1", close_key)
    out = await drain(pool, engine, quotes_at(51000), [close_id, buy])
    assert out[close_id] == ("COMPLETED", None)
    assert out[buy] == ("REJECTED", "MANDATE_DAILY_LOSS_LATCHED")
    assert engine.dispatcher.get_position_qty("BTCUSDT.BINANCE") == D("0")
    # A holding still left while latched (a fill that landed late) can be sold: REDUCE_RISK is never refused
    await hold(pool, m.instance_id, "0.05", "51000")
    engine.dispatcher.positions["BTCUSDT.BINANCE"] = D("0.05")
    sell = await queue(pool, m, {"instrument_id": "BTCUSDT", "side": "SELL", "quantity": "0.05"})
    assert (await drain(pool, engine, quotes_at(51000), [sell]))[sell][0] == "COMPLETED"
    assert engine.dispatcher.get_position_qty("BTCUSDT.BINANCE") == D("0")
    async with pool.acquire() as conn:
        after = await load_mandate(conn, m.id)
    assert (after.status, after.autonomy_level) == (MandateStatus.ACTIVE, AutonomyLevel.L1)
    assert await pool.fetchval("SELECT count(*) FROM ai_autonomy_events WHERE mandate_id = $1 AND kind = 'BREACH'",
                               m.id) == 0

    # The next UTC day: a new row from the last snapshot before 00:00, ACTIVE again
    await tick(51000, 0, day=1)
    next_day = (DAY_START + datetime.timedelta(days=1)).date()
    row = await session_row(pool, m, next_day)
    last_snapshot = await pool.fetchval(
        "SELECT equity FROM ai_mandate_equity WHERE mandate_id = $1 AND ts < $2 ORDER BY ts DESC, id DESC LIMIT 1",
        m.id, DAY_START + datetime.timedelta(days=1))
    assert row["start_equity"] == D(str(last_snapshot)).quantize(D("0.01"))
    assert row["loss_latched_at"] is None
    async with pool.acquire() as conn:
        assert await session_verdict_for(conn, m, DAY_START + datetime.timedelta(days=1, minutes=6)) is \
            SessionVerdict.ACTIVE
    assert len(alerts(redis, "MANDATE_DAILY_LOSS")) == 1


async def test_a_budget_edit_during_the_day_is_not_a_loss(db):
    pool = db
    m = await make_mandate(pool, daily_loss_limit_pct="3")
    await hold(pool, m.instance_id, "0.1", "50000")
    engine = engine_holding("0.1")
    redis = AsyncMock()
    t = DAY_START + datetime.timedelta(hours=1)
    await mandate_monitor_once(engine, pool, redis, quotes_at(50000), now=t)
    await pool.execute("UPDATE ai_mandates SET capital_budget_usd = 6000, hwm_rebased_at = $2 WHERE id = $1",
                       m.id, t + datetime.timedelta(seconds=30))
    await mandate_monitor_once(engine, pool, redis, quotes_at(50000), now=t + datetime.timedelta(seconds=61))
    row = await session_row(pool, m)
    assert row["last_pnl"] == D("0.00") and row["loss_latched_at"] is None
    assert await closes(pool, m) == 0


async def test_a_mandate_without_limits_has_no_session_row(db):
    pool = db
    m = await make_mandate(pool)
    await mandate_monitor_once(engine_holding("0"), pool, AsyncMock(), quotes_at(50000),
                               now=DAY_START + datetime.timedelta(minutes=1))
    assert await session_row(pool, m) is None
    async with pool.acquire() as conn:
        assert await session_verdict_for(conn, m, datetime.datetime.now(datetime.UTC)) is SessionVerdict.ACTIVE


@pytest.mark.parametrize(("column", "value"), [
    ("min_conviction", "0.49"), ("min_conviction", "0.96"), ("min_rr", "0.99"),
    ("daily_loss_limit_pct", "0"), ("daily_loss_limit_pct", "10.01"),  # above the mandate's 10% hard stop
    ("daily_profit_target_pct", "0"), ("daily_profit_target_pct", "100.01"),
    ("profit_lock_pct", "-1"), ("profit_lock_pct", "100.01"),
])
async def test_the_database_checks_the_limits(db, column, value):
    with pytest.raises(asyncpg.CheckViolationError, match="chk_ai_mandates_"):
        await make_mandate(db, **{column: value})


async def test_deleting_a_mandate_drops_its_sessions(db):
    m = await make_mandate(db, daily_loss_limit_pct="3")
    await mandate_monitor_once(engine_holding("0"), db, AsyncMock(), quotes_at(50000),
                               now=DAY_START + datetime.timedelta(minutes=1))
    assert await session_row(db, m) is not None
    await db.execute("DELETE FROM paper_strategy_instances WHERE instance_id = $1", m.instance_id)
    await db.execute("DELETE FROM ai_mandate_equity WHERE mandate_id = $1", m.id)
    await db.execute("DELETE FROM ai_mandates WHERE id = $1", m.id)
    assert await db.fetchval("SELECT count(*) FROM ai_mandate_daily_sessions WHERE mandate_id = $1", m.id) == 0


def test_the_session_alerts_reach_telegram_once_through_the_outbox():
    from cryptonite_notifier.bot import OUTBOX_KINDS  # noqa: PLC0415

    # the monitor writes them to the outbox and the channel: the notifier must not relay the channel copy too
    assert {"MANDATE_DAILY_LOSS", "MANDATE_PROFIT_LOCK", "MANDATE_DAILY_TARGET"} <= OUTBOX_KINDS
