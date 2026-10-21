"""Ratchet of AI perps' resting stops (auto-trade plan A4).

The pure rule (cryptonite_execution.stop_ratchet), then the orchestrator's ratchet_resting_stops against the paper
dispatcher, an in-memory paper_orders table (so the #110 restart restore reads back what the ratchet wrote) and a real
Redis under a private key prefix (best mark, the mandate:session:target_met set)."""

import asyncio
import itertools
import json
import time
import uuid
from decimal import Decimal as D
from unittest.mock import MagicMock

import pytest
import redis.asyncio as aioredis
from connections import REDIS_URL
from cryptonite_execution.stop_ratchet import favourable_move_pct, ratcheted_stop, to_tick
from cryptonite_orchestrator import server
from cryptonite_orchestrator.server import (
    attach_resting_stop,
    consume_perp_marks,
    fire_resting_stops,
    load_stop_restores,
    ratchet_resting_stops,
    reconcile_working_orders,
    restore_pending_stops,
)
from test_orchestrator_perp_wiring import (
    make_engine,
    mark_tick,
    publish_until_seen,
    redis_bus,  # noqa: F401 - fixture
)
from test_paper_risk_guardrails import PERP

OWNER = "AI_MANDATE_efa3a2e12ee54c32b4776aae022860f5"
MANUAL = "INST-001"
SPOT = "BTCUSDT.BINANCE"


# --- The pure rule -----------------------------------------------------------------------------------------------

def rule(**kw):
    args = dict(long=True, entry=D("50000"), best=D("51500"), mark=D("51500"), current_stop=D("48000"),
                trail_activate_pct=D("3"), trail_pct=D("2"), tick=D("0.01"), target_met=False)
    args.update(kw)
    return ratcheted_stop(**args)


def test_long_arms_at_the_activation_move_and_trails_the_peak():
    assert rule(best=D("51499.99"), mark=D("51499.99")) is None  # 2.99998 %: not armed
    assert rule() == D("50470.00")  # 51,500 x 0.98
    assert rule(best=D("52000"), mark=D("51900"), current_stop=D("50470")) == D("50960.00")


def test_short_mirror_trails_the_trough():
    short = dict(long=False, current_stop=D("52000"))
    assert rule(best=D("48500.01"), mark=D("48500.01"), **short) is None
    assert rule(best=D("48500"), mark=D("48500"), **short) == D("49470.00")  # 48,500 x 1.02
    assert rule(best=D("48000"), mark=D("48100"), long=False, current_stop=D("49470")) == D("48960.00")


def test_target_met_arms_at_once():
    assert rule(best=D("50200"), mark=D("50200")) is None  # +0.4 %: not armed on its own
    assert rule(best=D("50200"), mark=D("50200"), target_met=True) == D("49196.00")
    assert rule(long=False, best=D("49800"), mark=D("49800"), current_stop=D("52000"), target_met=True) == D("50796.00")


def test_never_loosens():
    assert rule(current_stop=D("50600")) is None  # 50,470 would loosen a 50,600 stop
    assert rule(long=False, best=D("48500"), mark=D("48500"), current_stop=D("49000")) is None


def test_tick_rounding_away_from_the_mark_and_one_tick_minimum():
    # 52,234.56 x 0.98 = 51,189.8688 -> 51,189.8 (down, for a long); 48,265.43 x 1.02 = 49,230.7386 -> 49,230.8 (up)
    assert rule(best=D("52234.56"), mark=D("52234.56"), tick=D("0.1")) == D("51189.8")
    assert rule(long=False, best=D("48265.43"), mark=D("48265.43"), current_stop=D("52000"),
                tick=D("0.1")) == D("49230.8")
    assert rule(current_stop=D("50469.995")) is None  # less than one tick tighter
    assert rule(current_stop=D("50469.99")) == D("50470.00")  # exactly one tick
    assert to_tick(D("100.07"), D("0.05"), long=True) == D("100.05")
    assert to_tick(D("100.07"), D("0.05"), long=False) == D("100.10")


def test_never_at_or_through_the_mark():
    # the mark gave back more than the trail since the peak: the new stop would trigger at once
    assert rule(best=D("51500"), mark=D("50400")) is None
    assert rule(best=D("51500"), mark=D("50470")) is None  # at the mark
    assert rule(best=D("51500"), mark=D("50470.01")) == D("50470.00")
    assert rule(long=False, best=D("48500"), mark=D("49600"), current_stop=D("52000")) is None
    assert rule(long=False, best=D("48500"), mark=D("49470"), current_stop=D("52000")) is None


def test_degenerate_inputs_do_not_move():
    assert rule(tick=D("0")) is None and rule(trail_pct=D("0")) is None and rule(entry=D("0")) is None
    assert favourable_move_pct(True, D("100"), D("103")) == D("3")
    assert favourable_move_pct(False, D("100"), D("103")) == D("-3")


# --- The orchestrator against the paper dispatcher -------------------------------------------------------------------

class PaperOrdersDB:
    """The statements the stop paths issue: paper_orders STOP_MARKET rows (insert, reconcile, newest ACCEPTED for the
    restore), system_settings and instruments reads."""

    def __init__(self, settings=None, ticks=None):
        self.rows: dict[str, dict] = {}
        self.settings = settings or {}
        self.ticks = {SPOT: D("0.01")} if ticks is None else ticks
        self.closes: list[dict] = []
        self._seq = itertools.count()

    async def execute(self, sql, *args):
        if "INSERT INTO paper_orders" in sql and "STOP_MARKET" in sql:
            cid, instance, _, inst, side, qty, price = args
            self.rows.setdefault(cid, {"instance_id": instance, "instrument_id": inst, "side": side, "quantity": qty,
                                       "price": price, "status": "ACCEPTED", "seq": next(self._seq)})
        elif "UPDATE paper_orders SET status = 'CANCELED'" in sql:
            keep = set(args[1])
            for cid, row in self.rows.items():
                if row["status"] == "ACCEPTED" and cid not in keep:
                    row["status"] = "CANCELED"
        elif "CLOSE_POSITION" in sql:
            self.closes.append(json.loads(args[3]))

    async def fetch(self, sql, *args):
        if "system_settings" in sql:
            return [{"key": k, "value": v} for k, v in self.settings.items() if k in args[0]]
        if "instruments" in sql:
            return [{"instrument_id": i, "price_increment": self.ticks[i]} for i in args[0] if i in self.ticks]
        return []

    async def fetchrow(self, sql, *args):
        if "STOP_MARKET" not in sql:
            return None
        _, inst, instance = args
        live = [(r["seq"], cid, r) for cid, r in self.rows.items()
                if r["status"] == "ACCEPTED" and r["instrument_id"] == inst and r["instance_id"] == instance]
        if not live:
            return None
        _, cid, r = max(live)
        return {"client_order_id": cid, "price": r["price"]}

    async def fetchval(self, sql, *args):
        return None

    def accepted(self):
        return {cid: r["price"] for cid, r in self.rows.items() if r["status"] == "ACCEPTED"}


def pool_for(db):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = db
    return pool


@pytest.fixture
async def rds(monkeypatch):
    """Real Redis; the ratchet's keys under a private prefix."""
    client = aioredis.from_url(REDIS_URL)
    prefix = f"test-ratchet-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(server, "TARGET_MET_KEY", f"{prefix}:mandate:session:target_met")
    monkeypatch.setattr(server, "STOP_RATCHET_STATE_KEY", prefix + ":perp:stop_ratchet:{account}:{instrument}")
    yield client
    keys = [k async for k in client.scan_iter(match=f"{prefix}:*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


def set_mark(engine, price):
    engine.dispatcher.update_mark_price(PERP, D(price), time.time_ns())


async def open_perp(db, qty="0.04", stop="48000", owner=OWNER, mark="50000"):
    """A 2x ISOLATED perp from 50,000 with its resting stop attached as an entry fill does."""
    engine = make_engine()
    engine.bridge = MagicMock()
    engine.dispatcher.set_perp_position(PERP, D(qty), D("50000"), leverage=2, margin_mode="ISOLATED",
                                        instance_id=owner)
    set_mark(engine, mark)
    assert await attach_resting_stop(db, engine, PERP, D(stop), owner)
    return engine


async def tick(engine, rds, db, price, now):
    set_mark(engine, price)
    return await ratchet_resting_stops(pool_for(db), engine, rds, {PERP: D(price)}, now=now)


def stop_of(engine):
    (cid, info), = engine.dispatcher.resting_stops(PERP).items()
    return cid, info["intent"].stop_price


async def test_long_stop_moves_up_as_the_mark_rises(rds):
    db = PaperOrdersDB()
    engine = await open_perp(db)
    first, _ = stop_of(engine)
    assert await tick(engine, rds, db, "51000", now=0) == 0  # +2 %: not armed
    assert stop_of(engine) == (first, D("48000"))
    assert await tick(engine, rds, db, "51500", now=1) == 1  # +3 %: 51,500 x 0.98
    moved, price = stop_of(engine)
    assert price == D("50470.00") and moved != first
    assert await tick(engine, rds, db, "52000", now=5) == 0  # within the minimum interval
    assert await tick(engine, rds, db, "51990", now=12) == 1  # peak 52,000 kept: 52,000 x 0.98
    cid, price = stop_of(engine)
    assert price == D("50960.00")
    # one ACCEPTED row, the newest one; the replaced ones are CANCELED
    assert db.accepted() == {cid: D("50960.00")}
    assert db.rows[first]["status"] == db.rows[moved]["status"] == "CANCELED"
    assert engine.dispatcher.open_orders[cid]["quantity"] == D("0.04")


async def test_ratcheted_stop_fires_on_the_retrace_at_its_price(rds):
    db = PaperOrdersDB()
    engine = await open_perp(db)
    await tick(engine, rds, db, "52000", now=0)
    cid, price = stop_of(engine)
    assert price == D("50960.00")
    set_mark(engine, "50960")
    assert await fire_resting_stops(pool_for(db), engine, {PERP: D("50960")}) == 1
    evt = engine.bridge.enqueue_event.call_args.args[0]
    assert (evt["client_order_id"], evt["side"], evt["price"], evt["limit_price"]) == (cid, "SELL", "50960.00",
                                                                                      "50960.00")
    assert PERP not in engine.dispatcher.perp_positions


async def test_ratcheted_stop_survives_a_restart(rds):
    db = PaperOrdersDB()
    engine = await open_perp(db)
    await tick(engine, rds, db, "52000", now=0)
    cid, _ = stop_of(engine)
    # restart: a new engine with the same position (rehydrated), the stop restored from paper_orders
    engine2 = make_engine()
    engine2.dispatcher.set_perp_position(PERP, D("0.04"), D("50000"), leverage=2, margin_mode="ISOLATED",
                                         instance_id=OWNER)
    set_mark(engine2, "51800")
    pool = pool_for(db)
    assert await load_stop_restores(pool, engine2) == 1
    await reconcile_working_orders(db, engine2)
    assert db.accepted() == {cid: D("50960.00")}  # kept while it waits for the mark
    assert await restore_pending_stops(pool, engine2) == 1
    assert stop_of(engine2) == (cid, D("50960.00"))
    # the best mark came back from Redis: a lower mark does not move the stop, a new peak does
    assert await tick(engine2, rds, db, "51800", now=0) == 0
    assert engine2.stop_ratchet[PERP].best == D("52000")
    assert await tick(engine2, rds, db, "53000", now=1) == 1
    assert stop_of(engine2)[1] == D("51940.00")


async def test_short_mirror_moves_down_fires_and_survives_a_restart(rds):
    db = PaperOrdersDB()
    engine = await open_perp(db, qty="-0.04", stop="52000")
    assert await tick(engine, rds, db, "49000", now=0) == 0  # +2 % for the short
    assert await tick(engine, rds, db, "48500", now=1) == 1
    cid, price = stop_of(engine)
    assert price == D("49470.00") and engine.dispatcher.open_orders[cid]["side"] == "BUY"
    assert db.accepted() == {cid: D("49470.00")}

    engine2 = make_engine()
    engine2.dispatcher.set_perp_position(PERP, D("-0.04"), D("50000"), leverage=2, margin_mode="ISOLATED",
                                         instance_id=OWNER)
    set_mark(engine2, "48700")
    pool = pool_for(db)
    await load_stop_restores(pool, engine2)
    assert await restore_pending_stops(pool, engine2) == 1
    assert stop_of(engine2) == (cid, D("49470.00"))

    engine2.bridge = MagicMock()
    set_mark(engine2, "49470")
    assert await fire_resting_stops(pool, engine2, {PERP: D("49470")}) == 1
    evt = engine2.bridge.enqueue_event.call_args.args[0]
    assert (evt["side"], evt["price"]) == ("BUY", "49470.00")


async def test_target_met_arms_the_ratchet_at_once(rds):
    db = PaperOrdersDB()
    engine = await open_perp(db)
    assert await tick(engine, rds, db, "50200", now=0) == 0
    await rds.sadd(server.TARGET_MET_KEY, OWNER)
    assert await tick(engine, rds, db, "50200", now=20) == 1
    assert stop_of(engine)[1] == D("49196.00")  # 50,200 x 0.98


async def test_another_mandate_meeting_its_target_does_not_arm_this_one(rds):
    db = PaperOrdersDB()
    engine = await open_perp(db)
    await rds.sadd(server.TARGET_MET_KEY, "AI_MANDATE_00000000000000000000000000000000")
    assert await tick(engine, rds, db, "50200", now=0) == 0
    assert stop_of(engine)[1] == D("48000")


async def test_not_moved_through_the_mark(rds):
    db = PaperOrdersDB()
    engine = await open_perp(db)
    assert await tick(engine, rds, db, "51500", now=0) == 1  # 50,470
    assert await tick(engine, rds, db, "52000", now=1) == 0  # peak recorded, within the interval
    # 52,000 x 0.98 = 50,960 is above this mark: no move; the 50,470 stop stays, the fire path decides
    assert await tick(engine, rds, db, "50900", now=20) == 0
    assert stop_of(engine)[1] == D("50470.00")


async def test_manual_perp_is_untouched(rds):
    db = PaperOrdersDB()
    engine = await open_perp(db, owner=MANUAL)
    first = stop_of(engine)
    assert await tick(engine, rds, db, "55000", now=0) == 0
    assert stop_of(engine) == first and len(db.rows) == 1
    assert [k async for k in rds.scan_iter(match=server.STOP_RATCHET_STATE_KEY.split("{")[0] + "*")] == []


async def test_spot_is_untouched(rds):
    engine = make_engine()
    d = engine.dispatcher
    d.open_orders["S-1"] = {"instrument_id": SPOT, "instance_id": OWNER, "side": "SELL", "quantity": D("0.01"),
                            "intent": MagicMock(order_type="LIMIT", stop_price=None)}
    before = dict(d.open_orders)
    assert await ratchet_resting_stops(pool_for(PaperOrdersDB()), engine, rds, {SPOT: D("60000")}, now=0) == 0
    assert d.open_orders == before


async def test_no_tick_known_leaves_the_stop(rds):
    db = PaperOrdersDB(ticks={})
    engine = await open_perp(db)
    assert await tick(engine, rds, db, "52000", now=0) == 0
    assert stop_of(engine)[1] == D("48000")


async def test_perp_tick_wins_over_the_spot_one(rds):
    db = PaperOrdersDB(ticks={SPOT: D("0.01"), PERP: D("0.1")})
    engine = await open_perp(db)
    assert await tick(engine, rds, db, "52234.56", now=0) == 1
    assert stop_of(engine)[1] == D("51189.8")


async def test_trail_settings_come_from_system_settings(rds):
    db = PaperOrdersDB(settings={"exit.trail_activate_pct": '"5.00"', "exit.trail_pct": '"1.00"'})
    engine = await open_perp(db)
    assert await tick(engine, rds, db, "52000", now=0) == 0  # +4 % < 5 %
    assert await tick(engine, rds, db, "52500", now=1) == 1
    assert stop_of(engine)[1] == D("51975.00")  # 52,500 x 0.99


async def test_a_refused_settings_set_keeps_the_defaults(rds):
    # trail above activation breaks a rule: the whole set is refused, the 3 % / 2 % defaults apply
    db = PaperOrdersDB(settings={"exit.trail_activate_pct": "1", "exit.trail_pct": "4"})
    engine = await open_perp(db)
    assert await tick(engine, rds, db, "51000", now=0) == 0
    assert await tick(engine, rds, db, "51500", now=1) == 1
    assert stop_of(engine)[1] == D("50470.00")


def test_defaults_are_the_exit_monitors():
    from cryptonite_scanner_service.exit_monitor import default_thresholds
    assert server.STOP_RATCHET_DEFAULTS == default_thresholds()


async def test_mark_loop_ratchets_then_fires_at_the_ratcheted_stop(redis_bus, monkeypatch):  # noqa: F811
    """End to end on the mark loop: the stop trails the rising mark, then the retrace fires it."""
    prefix = f"test-ratchet-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(server, "TARGET_MET_KEY", f"{prefix}:target_met")
    monkeypatch.setattr(server, "STOP_RATCHET_STATE_KEY", prefix + ":{account}:{instrument}")
    monkeypatch.setattr(server, "STOP_RATCHET_MIN_INTERVAL_S", 0.0)
    db = PaperOrdersDB()
    engine = await open_perp(db)
    d = engine.dispatcher
    task = asyncio.create_task(consume_perp_marks(
        redis_bus["redis"], engine, channel=redis_bus["channel"], key_pattern=redis_bus["key_pattern"],
        liquidation_interval_s=0.05, pool=pool_for(db),
    ))
    try:
        await publish_until_seen(redis_bus, mark_tick("51500"), engine)
        deadline = time.monotonic() + 5
        while stop_of(engine)[1] != D("50470.00"):
            assert time.monotonic() < deadline, "stop never ratcheted"
            await asyncio.sleep(0.02)
        await publish_until_seen(redis_bus, mark_tick("50400"), engine)  # gap through the 50,470 stop
        while PERP in d.perp_positions:
            assert time.monotonic() < deadline, "ratcheted stop never fired"
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        keys = [k async for k in redis_bus["redis"].scan_iter(match=f"{prefix}:*")]
        if keys:
            await redis_bus["redis"].delete(*keys)
    evt = engine.bridge.enqueue_event.call_args.args[0]
    assert (evt["side"], evt["price"], evt["limit_price"]) == ("SELL", "50400", "50470.00")
