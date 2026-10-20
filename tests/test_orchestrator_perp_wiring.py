"""Orchestrator perp wiring: Redis mark prices -> dispatcher, perp order attributes, liquidation loop.

Redis tests use a unique channel/key prefix per test so they never touch the real
channel:perp_mark_ticks / perp:mark:* used by a running feed or orchestrator.
"""

import asyncio
import json
import logging
import time
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.asyncio as aioredis
from connections import REDIS_URL
from cryptonite_execution.engine import PaperTradingEngine
from cryptonite_orchestrator.server import (
    SimpleQuoteCache,
    command_poller,
    consume_perp_marks,
    rehydrate_dispatcher_state,
)
from test_bridge_perpetuals_persistence import (  # noqa: F401 - ctx/db_url are fixtures
    ACCOUNT,
    INSTANCE,
    ctx,
    db_url,
    fill,
)
from test_paper_risk_guardrails import PERP, make_perp_dispatcher, perp_intent

D = Decimal
NEXT_FUNDING_NS = time.time_ns() + 3_600 * 10**9  # fixed within a funding period, as the real feed sends it


def mark_tick(price: str, age_s: float = 0.0, instrument_id: str = PERP) -> str:
    return json.dumps({
        "type": "perp.mark", "v": 1, "instrument_id": instrument_id, "symbol": "BTCUSDT",
        "ts_event_ns": time.time_ns() - int(age_s * 1e9), "mark_price": price,
        "index_price": price, "funding_rate": "0.0001", "next_funding_time_ns": NEXT_FUNDING_NS,
    })


def make_engine(dispatcher=None) -> PaperTradingEngine:
    engine = PaperTradingEngine(
        account_id="PAPER-ACCOUNT-001", trader_id="TRADER-WIRING", engine_run_id="RUN-WIRING",
        pg_pool=MagicMock(), redis_client=AsyncMock(),
    )
    engine.dispatcher = dispatcher or make_perp_dispatcher()
    return engine


@pytest.fixture
async def redis_bus():
    """Real Redis with a private channel and key prefix."""
    client = aioredis.from_url(REDIS_URL)
    prefix = f"test-{uuid.uuid4().hex[:8]}"
    bus = {"redis": client, "channel": f"{prefix}:perp_mark_ticks", "key_pattern": f"{prefix}:perp:mark:*",
           "key": f"{prefix}:perp:mark:BTCUSDT"}
    yield bus
    await client.delete(bus["key"])
    await client.aclose()


async def run_consumer(engine, bus, until, timeout=5.0):
    """Run consume_perp_marks until ``until()`` is true, then cancel it."""
    task = asyncio.create_task(consume_perp_marks(
        bus["redis"], engine, channel=bus["channel"], key_pattern=bus["key_pattern"], liquidation_interval_s=0.05,
    ))
    try:
        deadline = time.monotonic() + timeout
        while not until():
            assert time.monotonic() < deadline, "condition not reached"
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def publish_until_seen(bus, payload, engine):
    """Publish repeatedly until the consumer has subscribed and applied the tick."""
    expected = D(json.loads(payload)["mark_price"])
    for _ in range(100):
        await bus["redis"].publish(bus["channel"], payload)
        await asyncio.sleep(0.02)
        if engine.dispatcher.mark_prices.get(PERP) == expected:
            return
    raise AssertionError("mark tick never applied")


# --- Mark prices unblock perp orders ------------------------------------------------

async def test_redis_mark_tick_resolves_no_mark_price(redis_bus):
    engine = make_engine()
    d = engine.dispatcher
    d.mark_prices.clear()
    d.mark_ts_ns.clear()
    assert d.evaluate_intent(perp_intent("SELL", "0.01"))[1] == "NO_MARK_PRICE"

    task = asyncio.create_task(consume_perp_marks(
        redis_bus["redis"], engine, channel=redis_bus["channel"], key_pattern=redis_bus["key_pattern"],
    ))
    try:
        await publish_until_seen(redis_bus, mark_tick("50000"), engine)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert d.evaluate_intent(perp_intent("SELL", "0.01"))[:2] == (True, "APPROVED")


async def test_consumer_seeds_from_cached_mark_key(redis_bus):
    engine = make_engine()
    engine.dispatcher.mark_prices.clear()
    await redis_bus["redis"].set(redis_bus["key"], mark_tick("51000"))
    await run_consumer(engine, redis_bus, until=lambda: PERP in engine.dispatcher.mark_prices)
    assert engine.dispatcher.mark_prices[PERP] == D("51000")


def test_stale_mark_rejects_entries_but_allows_reduce_only():
    now = [1_000 * 10**9]
    d = make_perp_dispatcher()
    d._clock_ns = lambda: now[0]
    d.update_mark_price(PERP, D("50000"), ts_event_ns=now[0])
    d.set_perp_position(PERP, D("0.02"), D("50000"), leverage=10)
    assert d.evaluate_intent(perp_intent("BUY", "0.01"))[0] is True

    now[0] += 11 * 10**9  # mark is now 11s old
    approved, reason, _ = d.evaluate_intent(perp_intent("BUY", "0.01"))
    assert approved is False and reason.startswith("STALE_MARK_PRICE")
    assert d.evaluate_intent(perp_intent("SELL", "0.01", reduce_only=True))[0] is True
    assert d.fresh_mark_prices() == {}


# --- Order attribute passthrough ------------------------------------------------------

async def run_commands(engine, payloads, quote_cache):
    """Drive command_poller over MANUAL_ORDER payloads with a mocked connection."""
    rows = [
        {"command_id": uuid.uuid4(), "command_type": "MANUAL_ORDER", "target_instance_id": "INST-001",
         "payload": json.dumps(p)}
        for p in payloads
    ]
    conn = AsyncMock()
    remaining = list(rows)

    async def fake_fetchrow(query, *args):
        if remaining:
            return remaining.pop(0)
        raise asyncio.CancelledError()

    conn.fetchrow = fake_fetchrow
    conn.fetchval = AsyncMock(return_value=None)  # no paper_orders rows: nothing booked, no other owner holds
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    with pytest.raises(asyncio.CancelledError):
        await command_poller(pool, engine, quote_cache)
    return conn, rows


def command_outcomes(conn):
    """(status, error_message) per execution_commands update, in order."""
    out = []
    for call in conn.execute.call_args_list:
        sql = call.args[0]
        if "UPDATE execution_commands" in sql:
            status = "REJECTED" if "REJECTED" in sql else "COMPLETED"
            out.append((status, call.args[2] if status == "REJECTED" and len(call.args) > 2 else None))
    return out


async def test_order_attributes_reach_dispatcher_and_fill():
    engine = make_engine()
    engine.bridge = MagicMock()
    quotes = SimpleQuoteCache()
    quotes.set_quote(PERP, 50000.0, 50010.0)

    conn, _ = await run_commands(engine, [
        {"client_order_id": "P-OPEN", "instrument_id": "BTCUSDT-PERP", "side": "SELL", "quantity": "0.04",
         "leverage": 10, "margin_mode": "ISOLATED"},
    ], quotes)

    pos = engine.dispatcher.perp_positions[PERP]
    assert (pos.signed_qty, pos.leverage, pos.margin_mode, pos.instance_id) == (D("-0.04"), 10, "ISOLATED", "INST-001")
    evt = engine.bridge.enqueue_event.call_args.args[0]
    assert evt["position"]["margin_mode"] == "ISOLATED" and evt["position"]["leverage"] == 10
    assert command_outcomes(conn) == [("COMPLETED", None)]

    conn, _ = await run_commands(engine, [
        {"client_order_id": "P-CLOSE", "instrument_id": "BTCUSDT-PERP", "side": "BUY", "quantity": "0.04",
         "reduce_only": True},
    ], quotes)
    assert PERP not in engine.dispatcher.perp_positions
    assert engine.bridge.enqueue_event.call_args.args[0]["position"] == {"signed_qty": "0"}


async def test_invalid_or_out_of_range_attributes_are_rejected_not_retried():
    engine = make_engine()
    engine.bridge = MagicMock()
    quotes = SimpleQuoteCache()
    quotes.set_quote(PERP, 50000.0, 50010.0)

    conn, _ = await run_commands(engine, [
        {"client_order_id": "BAD-LEV", "instrument_id": "BTCUSDT-PERP", "side": "BUY", "quantity": "0.01", "leverage": "abc"},
        {"client_order_id": "HIGH-LEV", "instrument_id": "BTCUSDT-PERP", "side": "BUY", "quantity": "0.01", "leverage": 50},
        {"client_order_id": "RO-FLAT", "instrument_id": "BTCUSDT-PERP", "side": "SELL", "quantity": "0.01", "reduce_only": True},
        {"client_order_id": "SPOT-LEV", "instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.01", "leverage": 5},
    ], quotes)

    outcomes = command_outcomes(conn)
    assert [status for status, _ in outcomes] == ["REJECTED"] * 4
    reasons = [reason for _, reason in outcomes]
    assert reasons[0].startswith("INVALID_ORDER_PARAMETERS")
    assert reasons[1].startswith("LEVERAGE_OUT_OF_RANGE")
    assert reasons[2] == "REDUCE_ONLY_NO_POSITION"
    assert reasons[3].startswith("SPOT_LEVERAGE_NOT_ALLOWED")
    assert engine.dispatcher.perp_positions == {}


# --- Liquidation loop ------------------------------------------------------------------

async def test_adverse_tick_via_consumer_triggers_liquidation(redis_bus, caplog):
    d = make_perp_dispatcher()
    d.set_perp_position(PERP, D("0.04"), D("50000"), leverage=10, margin_mode="ISOLATED")  # P_liq ~45,180.72
    engine = make_engine(d)
    caplog.set_level(logging.CRITICAL, logger="cryptonite.orchestrator")

    task = asyncio.create_task(consume_perp_marks(
        redis_bus["redis"], engine, channel=redis_bus["channel"], key_pattern=redis_bus["key_pattern"],
        liquidation_interval_s=0.05,
    ))
    try:
        await publish_until_seen(redis_bus, mark_tick("48000"), engine)  # normal move: no liquidation
        await asyncio.sleep(0.15)
        assert PERP in d.perp_positions
        await publish_until_seen(redis_bus, mark_tick("45000"), engine)  # breach
        deadline = time.monotonic() + 5
        while PERP in d.perp_positions:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert d.cash_balance == D("9800")
    evt = engine.bridge.queue.get_nowait()
    assert evt["liquidation"] is True and evt["side"] == "SELL"
    assert any("Synthetic liquidation" in r.getMessage() for r in caplog.records)


async def test_stale_breaching_mark_does_not_liquidate(redis_bus):
    d = make_perp_dispatcher()
    d.set_perp_position(PERP, D("0.04"), D("50000"), leverage=10, margin_mode="ISOLATED")
    engine = make_engine(d)
    task = asyncio.create_task(consume_perp_marks(
        redis_bus["redis"], engine, channel=redis_bus["channel"], key_pattern=redis_bus["key_pattern"],
        liquidation_interval_s=0.05,
    ))
    try:
        await publish_until_seen(redis_bus, mark_tick("45000", age_s=60), engine)
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert d.get_perp_position_qty(PERP) == D("0.04")  # breach seen only on a 60s-old mark


# --- Restart rehydration ------------------------------------------------------------------

async def test_rehydration_restores_perp_shorts_as_shorts(ctx):  # noqa: F811 - fixture
    await fill(ctx, "S-1", "SELL", "0.04", "50000", leverage=10, margin_mode="ISOLATED")
    await fill(ctx, "SP-1", "BUY", "0.01", "50000", instrument="BTCUSDT.BINANCE")

    engine = make_engine(make_perp_dispatcher())
    engine.account_id = ACCOUNT
    engine.dispatcher.perp_positions.clear()
    await rehydrate_dispatcher_state(ctx["pool"], engine)

    d = engine.dispatcher
    pos = d.perp_positions[PERP]
    assert (pos.signed_qty, pos.entry_price, pos.leverage, pos.margin_mode, pos.instance_id) == (
        D("-0.04"), D("50000"), 10, "ISOLATED", INSTANCE,
    )
    assert d.locked_margin == D("200")
    assert d.positions == {"BTCUSDT.BINANCE": D("0.01")}  # the perp short is not loaded as a spot long
