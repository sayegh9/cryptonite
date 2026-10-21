"""Resting stops survive an orchestrator restart (2026-10-21: a deploy restarted the orchestrator and the BTC perp's
STOP-70CBB940B141 was marked CANCELED; resting stops live in memory only).

load_stop_restores finds each open perp's stop (its ACCEPTED STOP_MARKET row, else the entry's stop_loss);
reconcile_working_orders keeps those rows ACCEPTED; restore_pending_stops places them once the mark is known, and
closes the position if the mark already went through the stop. Stale ingress right after the restart defers the
stop (never closes); any other refusal drops it without a close."""

import asyncio
import json
import logging
import time
from decimal import Decimal as D
from unittest.mock import MagicMock

from cryptonite_orchestrator.server import (
    load_stop_restores,
    reconcile_working_orders,
    restore_pending_stops,
)
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import PERP

OWNER = "AI_MANDATE_efa3a2e12ee54c32b4776aae022860f5"


class FakeConn:
    """Answers the restore queries; records every execute."""

    def __init__(self, stop_row=None, entry_stop=None):
        self.stop_row, self.entry_stop = stop_row, entry_stop
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        return self.stop_row if "STOP_MARKET" in sql else None

    async def fetchval(self, sql, *args):
        return self.entry_stop if "stop_loss" in sql else None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    def transaction(self):
        class T:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *_):
                return False
        return T()


def pool_for(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    return pool


def engine_with_long(mark: str | None):
    engine = make_engine()
    d = engine.dispatcher
    d.set_perp_position(PERP, D("0.04"), D("50000"), leverage=2, margin_mode="ISOLATED", instance_id=OWNER)
    if mark is None:
        d.mark_prices.pop(PERP, None)  # the test dispatcher starts with one
    else:
        d.update_mark_price(PERP, D(mark), time.time_ns())
    return engine


def closes(conn):
    return [json.loads(a[3]) for sql, a in conn.executed if "CLOSE_POSITION" in sql]


def test_an_accepted_stop_row_is_kept_and_placed_again_with_its_id():
    engine = engine_with_long(mark="50500")
    conn = FakeConn(stop_row={"client_order_id": "STOP-OLD", "price": D("48000")})
    pool = pool_for(conn)
    assert asyncio.run(load_stop_restores(pool, engine)) == 1
    # the reconcile at startup keeps the row ACCEPTED while it waits for a mark
    asyncio.run(reconcile_working_orders(conn, engine))
    kept = conn.executed[-1][1][1]
    assert "STOP-OLD" in kept
    assert asyncio.run(restore_pending_stops(pool, engine)) == 1
    stops = engine.dispatcher.resting_stops(PERP)
    assert list(stops) == ["STOP-OLD"] and stops["STOP-OLD"]["intent"].stop_price == D("48000")
    assert engine.pending_stop_restores == {} and closes(conn) == []


def test_without_a_row_the_entry_stop_loss_is_used():
    engine = engine_with_long(mark="50500")
    conn = FakeConn(stop_row=None, entry_stop="48000")
    pool = pool_for(conn)
    asyncio.run(load_stop_restores(pool, engine))
    assert asyncio.run(restore_pending_stops(pool, engine)) == 1
    (stop,) = engine.dispatcher.resting_stops(PERP).values()
    assert stop["intent"].stop_price == D("48000") and stop["side"] == "SELL"
    assert any("STOP_MARKET" in sql for sql, _ in conn.executed)  # a new paper_orders row


def test_no_mark_yet_waits_for_the_next_pass():
    engine = engine_with_long(mark=None)
    conn = FakeConn(entry_stop="48000")
    pool = pool_for(conn)
    asyncio.run(load_stop_restores(pool, engine))
    assert asyncio.run(restore_pending_stops(pool, engine)) == 0
    assert len(engine.pending_stop_restores) == 1 and engine.dispatcher.resting_stops(PERP) == {}
    engine.dispatcher.update_mark_price(PERP, D("50500"), time.time_ns())
    assert asyncio.run(restore_pending_stops(pool, engine)) == 1


def test_a_stop_the_mark_went_through_closes_the_position():
    engine = engine_with_long(mark="47500")  # below the 48,000 stop: it cannot rest any more
    conn = FakeConn(stop_row={"client_order_id": "STOP-OLD", "price": D("48000")})
    pool = pool_for(conn)
    asyncio.run(load_stop_restores(pool, engine))
    assert asyncio.run(restore_pending_stops(pool, engine)) == 0
    (close,) = closes(conn)
    assert close["instrument_id"] == PERP and close["reason"] == "STOP_NOT_RESTORED"
    owner = [a[2] for sql, a in conn.executed if "CLOSE_POSITION" in sql][0]
    assert owner == OWNER


def test_no_stop_anywhere_restores_nothing():
    engine = engine_with_long(mark="50500")
    pool = pool_for(FakeConn())
    assert asyncio.run(load_stop_restores(pool, engine)) == 0
    assert engine.pending_stop_restores == {}


def stale(engine, fresh=False):
    engine.dispatcher.ingress_health.is_fresh = MagicMock(return_value=fresh)


def test_stale_ingress_defers_the_stop_and_the_next_fresh_pass_places_it():
    engine = engine_with_long(mark="50500")
    conn = FakeConn(stop_row={"client_order_id": "STOP-OLD", "price": D("48000")})
    pool = pool_for(conn)
    asyncio.run(load_stop_restores(pool, engine))
    stale(engine)
    assert asyncio.run(restore_pending_stops(pool, engine)) == 0
    assert list(engine.pending_stop_restores) == ["STOP-OLD"] and closes(conn) == []
    assert engine.dispatcher.resting_stops(PERP) == {}
    stale(engine, fresh=True)
    assert asyncio.run(restore_pending_stops(pool, engine)) == 1
    assert list(engine.dispatcher.resting_stops(PERP)) == ["STOP-OLD"]
    assert engine.pending_stop_restores == {} and closes(conn) == []


def test_stale_ingress_defers_a_new_row_stop_too():
    engine = engine_with_long(mark="50500")
    conn = FakeConn(entry_stop="48000")
    pool = pool_for(conn)
    asyncio.run(load_stop_restores(pool, engine))
    stale(engine)
    assert asyncio.run(restore_pending_stops(pool, engine)) == 0
    assert len(engine.pending_stop_restores) == 1 and closes(conn) == []
    stale(engine, fresh=True)
    assert asyncio.run(restore_pending_stops(pool, engine)) == 1
    (stop,) = engine.dispatcher.resting_stops(PERP).values()
    assert stop["intent"].stop_price == D("48000") and closes(conn) == []


def test_stale_longer_than_the_threshold_alerts_once_and_never_closes(caplog):
    engine = engine_with_long(mark="50500")
    conn = FakeConn(stop_row={"client_order_id": "STOP-OLD", "price": D("48000")})
    pool = pool_for(conn)
    asyncio.run(load_stop_restores(pool, engine))
    stale(engine)
    engine.stop_restore_deferred = {"STOP-OLD": (time.monotonic() - 500, False)}
    with caplog.at_level(logging.CRITICAL):
        for _ in range(3):
            asyncio.run(restore_pending_stops(pool, engine))
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1 and "NOT closed" in critical[0].getMessage()
    assert sum("notifications_outbox" in sql for sql, _ in conn.executed) == 1
    assert list(engine.pending_stop_restores) == ["STOP-OLD"] and closes(conn) == []


def test_a_new_row_stop_the_mark_went_through_closes_the_position():
    engine = engine_with_long(mark="47500")
    conn = FakeConn(entry_stop="48000")
    pool = pool_for(conn)
    asyncio.run(load_stop_restores(pool, engine))
    assert asyncio.run(restore_pending_stops(pool, engine)) == 0
    (close,) = closes(conn)
    assert close["reason"] == "STOP_NOT_RESTORED" and engine.pending_stop_restores == {}


def test_another_refusal_is_dropped_without_a_close():
    engine = engine_with_long(mark="50500")
    conn = FakeConn()
    pool = pool_for(conn)
    engine.pending_stop_restores = {"STOP-X": (PERP, D("48000"), "AI_MANDATE_other", False)}
    assert asyncio.run(restore_pending_stops(pool, engine)) == 0
    assert engine.pending_stop_restores == {} and closes(conn) == []
    assert engine.dispatcher.resting_stops(PERP) == {}
