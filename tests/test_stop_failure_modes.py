"""The three stop failures of the 2026-10 leverage replay (docs/research/leverage-replay-2026-10.md), replayed
through the exit monitor and the real orchestrator command poller on a throwaway database at Alembic head.

1. BTC 2026-10-08: the AI hard stop only alerted for 99 minutes because a manual position shared the instrument.
2. WLD 2026-10-17: two manual SELLs sold the mandate's coins, so its structural stop sold 0.0000017 and the rest ran
   to the hard stop the next day.
3. WLD 2026-10-17: a trailing-stop close waited 5 h 10 min behind failing RESUME commands at the queue head.
"""

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
    SPOT,
    drain,
    engine_holding,
    hold,
    make_mandate,
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
