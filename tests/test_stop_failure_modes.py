"""The three stop failures of the 2026-10 leverage replay (docs/research/leverage-replay-2026-10.md), replayed
through the exit monitor and the real orchestrator command poller on a throwaway database at Alembic head.

1. BTC 2026-10-08: the AI hard stop only alerted for 99 minutes because a manual position shared the instrument.
2. WLD 2026-10-17: two manual SELLs sold the mandate's coins, so its structural stop sold 0.0000017 and the rest ran
   to the hard stop the next day.
3. WLD 2026-10-17: a trailing-stop close waited 5 h 10 min behind failing RESUME commands at the queue head.
"""

import datetime
import json
import uuid
from decimal import Decimal

import asyncpg
import pytest
from cryptonite_execution.risk import OrderIntent
from cryptonite_scanner_service import exit_monitor as em
from migrated_db import migrated_database, seed_paper_account
from test_exit_monitor import Redis, book
from test_mandate_hard_stop import (
    ACCOUNT,
    SPOT,
    drain,
    engine_holding,
    hold,
    make_mandate,
    queue,
    quotes_at,
)

D = Decimal


@pytest.fixture
def db_url():
    with migrated_database("cryptonite_stopfail") as url:
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


async def manual_order(pool, side: str, qty: str) -> uuid.UUID:
    """A MANUAL_ORDER as POST /paper/orders queues it (the operator's trade ticket)."""
    cid = uuid.uuid4()
    await pool.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, requester_id,
               payload, status, environment, account_id)
           VALUES ($1, $2, 'MANUAL_ORDER', 'MANUAL_TRADE', 'web-ui', $3::jsonb, 'PENDING', 'PAPER', $4)""",
        cid, f"MANUAL_ORDER:{cid}",
        json.dumps({"instrument_id": SPOT, "side": side, "order_type": "MARKET", "quantity": qty,
                    "client_order_id": f"MANUAL-{cid.hex[:8].upper()}", "account_id": ACCOUNT}),
        ACCOUNT)
    return cid


def working_limit_buy(engine, instance_id: str) -> str:
    cid = f"W-{uuid.uuid4().hex[:8]}"
    ok, reason = engine.dispatcher.dispatch(OrderIntent(
        client_order_id=cid, instance_id=instance_id, instrument_id=SPOT, side="BUY", order_type="LIMIT",
        quantity=D("0.01"), price=D("49500")), lambda o: None, nautilus_order=None)[:2]
    assert ok, reason
    return cid


# --- 1. A stop on a shared instrument closes the owner's own quantity --------------------------------------------------

async def test_a_mandate_stop_on_a_shared_instrument_closes_only_the_mandates_coins(db):
    m = await make_mandate(db, level=3)
    await hold(db, m.instance_id, "0.1", "50000")
    await hold(db, "MANUAL_TRADE", "0.5", "50000")
    engine = engine_holding("0.6")
    manual_working = working_limit_buy(engine, "MANUAL_TRADE")
    mandate_working = working_limit_buy(engine, m.instance_id)

    redis = Redis()
    await em.monitor_once(db, redis, book(47000.0))  # -6%: both positions are past the 5% hard stop
    close = await db.fetchrow("SELECT command_id, target_instance_id, payload FROM execution_commands")
    assert close["target_instance_id"] == m.instance_id
    assert json.loads(close["payload"]) == {"instrument_id": SPOT, "reason": "HARD_STOP", "quantity": "0.1"}
    assert ("ALERT", "MANUAL") in {(p["action"], "MANUAL" if not p["ai_owned"] else "AI") for p in redis.published}

    assert (await drain(db, engine, quotes_at(47000), [close["command_id"]]))[close["command_id"]] == ("COMPLETED", None)
    assert engine.dispatcher.get_position_qty(SPOT) == D("0.5")  # the manual holding is untouched
    fill = engine.bridge.enqueue_event.call_args.args[0]
    assert (fill["instance_id"], fill["side"], D(fill["quantity"])) == (m.instance_id, "SELL", D("0.1"))
    # only the mandate's working order goes with its close
    assert manual_working in engine.dispatcher.open_orders and mandate_working not in engine.dispatcher.open_orders
    assert await db.fetchval("SELECT count(*) FROM notifications_outbox WHERE kind = 'CLOSE_SHORT'") == 0


# --- 2. A manual SELL cannot sell a mandate's coins; a short close is never silent -------------------------------------

async def test_manual_sells_cannot_sell_the_mandates_coins_so_its_stop_closes_all_of_them(db):
    m = await make_mandate(db, level=3)
    await hold(db, m.instance_id, "0.2", "50000")
    await hold(db, "MANUAL_TRADE", "0.05", "50000")
    engine = engine_holding("0.25")

    # The 2026-10-17 18:09 replay: two manual SELLs of half the mandate's holding each
    first, second = await manual_order(db, "SELL", "0.1"), await manual_order(db, "SELL", "0.1")
    out = await drain(db, engine, quotes_at(50000), [first, second])
    for cid in (first, second):
        status, error = out[cid]
        assert status == "REJECTED" and error.startswith("SELL_EXCEEDS_OWN_HOLDING: MANUAL_TRADE can sell 0.05 ")
    assert engine.dispatcher.get_position_qty(SPOT) == D("0.25")

    # the manual trader's own coins still sell
    own = await manual_order(db, "SELL", "0.05")
    assert (await drain(db, engine, quotes_at(50000), [own]))[own] == ("COMPLETED", None)
    assert engine.dispatcher.get_position_qty(SPOT) == D("0.2")

    # the structural stop then sells all of the mandate's 0.2, not dust
    stop = await queue(db, m, {"_type": "CLOSE_POSITION", "instrument_id": SPOT, "reason": "STRUCTURAL_STOP"})
    assert (await drain(db, engine, quotes_at(48000), [stop]))[stop] == ("COMPLETED", None)
    fill = engine.bridge.enqueue_event.call_args.args[0]
    assert (fill["instance_id"], D(fill["quantity"])) == (m.instance_id, D("0.2"))
    assert engine.dispatcher.get_position_qty(SPOT) == D("0")


async def test_a_close_that_sells_less_than_the_mandate_owns_raises_a_critical_alert(db):
    m = await make_mandate(db, level=3)
    await hold(db, m.instance_id, "0.2", "50000")
    engine = engine_holding("0.0000017")  # what the account held after the 2026-10-17 manual sells

    stop = await queue(db, m, {"_type": "CLOSE_POSITION", "instrument_id": SPOT, "reason": "STRUCTURAL_STOP"})
    assert (await drain(db, engine, quotes_at(48000), [stop]))[stop] == ("COMPLETED", None)
    row = await db.fetchrow("SELECT tier, title, payload FROM notifications_outbox WHERE kind = 'CLOSE_SHORT'")
    assert row["tier"] == "CRITICAL" and "sold 0.0000017 of its 0.2" in row["title"]
    assert json.loads(row["payload"])["command_id"] == str(stop)

    # nothing left in the account at all: still an alert, not a silent success
    engine.dispatcher.positions[SPOT] = D("0")
    again = await queue(db, m, {"_type": "CLOSE_POSITION", "instrument_id": SPOT, "reason": "HARD_STOP"})
    await drain(db, engine, quotes_at(47000), [again])
    assert await db.fetchval("SELECT count(*) FROM notifications_outbox WHERE kind = 'CLOSE_SHORT'") == 2


# --- 3. A stop is claimed ahead of older commands ----------------------------------------------------------------------

async def test_a_stop_is_claimed_ahead_of_older_queued_commands(db):
    m = await make_mandate(db, level=3)
    await hold(db, m.instance_id, "0.1", "50000")
    engine = engine_holding("0.1")
    older = []
    for _ in range(2):  # queued hours before the stop, like the 2026-10-16 21:59 RESUMEs
        cid = await manual_order(db, "BUY", "0.001")
        await db.execute("UPDATE execution_commands SET created_at = now() - interval '9 hours' WHERE command_id = $1", cid)
        older.append(cid)
    stop = await queue(db, m, {"_type": "CLOSE_POSITION", "instrument_id": SPOT, "reason": "TRAILING_STOP"})

    await drain(db, engine, quotes_at(51000), [stop, *older])
    order = [r["command_id"] for r in await db.fetch(
        "SELECT command_id FROM execution_commands WHERE command_id = ANY($1::uuid[]) ORDER BY processed_at", [stop, *older])]
    assert order[0] == stop
    processed = await db.fetchval("SELECT processed_at FROM execution_commands WHERE command_id = $1", stop)
    created = await db.fetchval("SELECT created_at FROM execution_commands WHERE command_id = $1", stop)
    assert processed - created < datetime.timedelta(seconds=5)
