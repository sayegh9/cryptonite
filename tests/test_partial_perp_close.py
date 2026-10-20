"""Stage 1.5 (leverage plan): partial perp close. CLOSE_POSITION takes a quantity on perps, so the exit monitor's
TP1 50% scale-out reduces a perp instead of flattening it; the resting stop stays for what is left."""

import asyncio
import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptonite_orchestrator.server import SimpleQuoteCache, command_poller
from cryptonite_scanner_service.exit_monitor import Position, enqueue_tp1_reduce
from test_orchestrator_perp_wiring import command_outcomes, make_engine
from test_paper_risk_guardrails import PERP, make_perp_dispatcher, perp_intent

D = Decimal


def long_with_stop(qty="0.04"):
    d = make_perp_dispatcher()
    d.set_perp_position(PERP, D(qty), D("50000"), leverage=5, margin_mode="ISOLATED", instance_id="INST-001")
    assert d.place_resting_stop("STOP-1", PERP, D("48000"))[0]
    return d


async def run(engine, rows, quotes):
    conn = AsyncMock()
    remaining = list(rows)

    async def fake_fetchrow(query, *args):
        if remaining:
            return remaining.pop(0)
        raise asyncio.CancelledError()

    conn.fetchrow = fake_fetchrow
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    with pytest.raises(asyncio.CancelledError):
        await command_poller(pool, engine, quotes)
    return conn


def close_row(**payload):
    return {"command_id": uuid.uuid4(), "command_type": "CLOSE_POSITION", "target_instance_id": "INST-001",
            "payload": json.dumps({"instrument_id": "BTCUSDT-PERP", **payload})}


def test_dispatcher_partial_close_realizes_pro_rata_and_shrinks_the_stop():
    d = long_with_stop()
    side, qty, realized = d.flatten_perp_position(PERP, D("51000"), D("0"), "TP1-1", quantity=D("0.02"))
    assert (side, qty, realized) == ("SELL", D("0.02"), D("20"))
    pos = d.perp_positions[PERP]
    assert pos.signed_qty == D("0.02") and pos.initial_margin == D("200")  # 400 IM released half
    assert d.open_orders["STOP-1"]["quantity"] == D("0.02")


def test_dispatcher_partial_close_is_capped_at_the_position():
    d = long_with_stop()
    _, qty, _ = d.flatten_perp_position(PERP, D("51000"), D("0"), "C-1", quantity=D("1"))
    assert qty == D("0.04") and PERP not in d.perp_positions
    assert d.resting_stops(PERP) == {}  # dropped with the position


def test_a_flip_drops_the_old_sides_stop():
    d = long_with_stop()
    assert d.dispatch(perp_intent("SELL", "0.06", "FLIP", leverage=5, margin_mode="ISOLATED"), MagicMock(), MagicMock())[0]
    d.handle_fill("FLIP", D("0.06"), D("50000"), D("0"))
    assert d.get_perp_position_qty(PERP) == D("-0.02")
    assert d.resting_stops(PERP) == {}


async def test_close_position_with_quantity_reduces_a_perp_and_keeps_the_stop():
    engine = make_engine(long_with_stop())
    engine.bridge = MagicMock()
    quotes = SimpleQuoteCache()
    quotes.set_quote(PERP, 51000.0, 51010.0)
    conn = await run(engine, [close_row(quantity="0.02", reason="TAKE_PROFIT_1")], quotes)
    assert command_outcomes(conn) == [("COMPLETED", None)]
    d = engine.dispatcher
    assert d.get_perp_position_qty(PERP) == D("0.02")
    assert d.open_orders["STOP-1"]["quantity"] == D("0.02")
    evt = engine.bridge.enqueue_event.call_args.args[0]
    assert (evt["side"], evt["quantity"]) == ("SELL", "0.02")
    assert evt["position"]["signed_qty"] == "0.02"


async def test_close_position_without_quantity_still_flattens_and_drops_the_stop():
    engine = make_engine(long_with_stop())
    engine.bridge = MagicMock()
    quotes = SimpleQuoteCache()
    quotes.set_quote(PERP, 51000.0, 51010.0)
    await run(engine, [close_row()], quotes)
    assert PERP not in engine.dispatcher.perp_positions
    assert engine.dispatcher.open_orders == {}


async def test_exit_monitor_tp1_on_a_perp_is_a_close_with_half_the_quantity():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    conn.fetchval.return_value = 0
    pos = Position("BTCUSDT-PERP.BINANCE", "SHORT", 50000.0, 48000.0, True, 1, owner_instance="AI_MANDATE_x",
                   take_profit_price=48000.0, quantity=0.04)
    assert await enqueue_tp1_reduce(conn, pos)
    args = conn.execute.call_args.args
    assert args[6] == "CLOSE_POSITION"
    assert json.loads(args[4]) == {"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.02",
                                   "reason": "TAKE_PROFIT_1", "source": "exit_monitor_tp1"}


async def test_exit_monitor_tp1_on_spot_is_unchanged():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    conn.fetchval.return_value = 0
    pos = Position("XRPUSDT.BINANCE", "LONG", 0.5, 0.55, True, 1, owner_instance="AI_MANDATE_x",
                   take_profit_price=0.55, quantity=100.0)
    assert await enqueue_tp1_reduce(conn, pos)
    args = conn.execute.call_args.args
    assert args[6] == "SUBMIT_ORDER"
    assert json.loads(args[4])["side"] == "SELL" and json.loads(args[4])["quantity"] == "50"
