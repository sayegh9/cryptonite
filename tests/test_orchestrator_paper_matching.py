"""Orchestrator paper matching: working orders fill only when the live quote reaches them.

Covers manual/AI orders from the command poller and strategy orders from the patched
Nautilus strategy, which previously reserved cash and never filled.
"""

import asyncio
import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptonite_execution.risk import OrderIntent
from cryptonite_orchestrator.server import (
    SimpleQuoteCache,
    _patch_strategy_for_paper_engine,
    command_poller,
    match_resting_orders,
)
from test_orchestrator_perp_wiring import command_outcomes, make_engine, run_commands
from test_paper_risk_guardrails import PERP, make_fresh_dispatcher, make_perp_dispatcher

D = Decimal
SPOT = "BTCUSDT.BINANCE"


def spot_engine():
    dispatcher, _ = make_fresh_dispatcher()  # 10,000 USDT; dispatcher quote 50,000 / 50,010
    engine = make_engine(dispatcher)
    engine.bridge = MagicMock()
    return engine


def pool_for(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    return pool


def order(cid, side, order_type, qty, price=None):
    p = {"client_order_id": cid, "instrument_id": "BTCUSDT", "side": side, "order_type": order_type, "quantity": qty}
    if price:
        p["price"] = price
    return p


async def test_non_marketable_limit_rests_then_fills_at_its_limit():
    engine = spot_engine()
    quotes = SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)

    conn, _ = await run_commands(engine, [order("L-1", "BUY", "LIMIT", "0.02", "49800")], quotes)

    assert command_outcomes(conn) == [("COMPLETED", None)]
    assert engine.dispatcher.open_orders["L-1"]["rested"] is True
    assert engine.dispatcher.get_position_qty(SPOT) == 0
    engine.bridge.enqueue_event.assert_not_called()
    assert any("'ACCEPTED'" in c.args[0] for c in conn.execute.call_args_list)

    # Ask still above the limit: keeps working.
    await match_resting_orders(pool_for(AsyncMock()), engine, SPOT, 49790.0, 49805.0)
    assert "L-1" in engine.dispatcher.open_orders

    # Ask gaps through the limit: fills at 49,800 (maker), not at the better 49,700.
    await match_resting_orders(pool_for(AsyncMock()), engine, SPOT, 49690.0, 49700.0)
    assert "L-1" not in engine.dispatcher.open_orders
    assert engine.dispatcher.get_position_qty(SPOT) == D("0.02")
    evt = engine.bridge.enqueue_event.call_args.args[0]
    assert (D(evt["price"]), evt["status"], evt["instance_id"]) == (D("49800"), "FILLED", "INST-001")
    # 10,000 - 0.02 * 49,800 - 0.1% fee (0.996)
    assert engine.dispatcher.cash_balance == D("9003.004")


async def test_marketable_limit_fills_immediately_at_the_ask():
    engine = spot_engine()
    quotes = SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)

    await run_commands(engine, [order("L-2", "BUY", "LIMIT", "0.02", "50100")], quotes)

    assert "L-2" not in engine.dispatcher.open_orders
    assert D(engine.bridge.enqueue_event.call_args.args[0]["price"]) == D("50010")


async def test_market_order_without_a_quote_waits_for_the_first_quote():
    engine = spot_engine()
    conn, _ = await run_commands(engine, [order("M-1", "BUY", "MARKET", "0.02")], SimpleQuoteCache())

    assert command_outcomes(conn) == [("COMPLETED", None)]
    assert "M-1" in engine.dispatcher.open_orders
    assert "rested" not in engine.dispatcher.open_orders["M-1"]

    await match_resting_orders(pool_for(AsyncMock()), engine, SPOT, 50000.0, 50010.0)
    assert engine.dispatcher.get_position_qty(SPOT) == D("0.02")
    assert D(engine.bridge.enqueue_event.call_args.args[0]["price"]) == D("50010")


async def test_sizing_without_a_quote_is_rejected_instead_of_assuming_a_price():
    engine = spot_engine()
    payload = {"client_order_id": "A-1", "instrument_id": "BTCUSDT", "side": "BUY", "allocation_pct": 5}
    conn, _ = await run_commands(engine, [payload], SimpleQuoteCache())

    assert command_outcomes(conn) == [("REJECTED", "NO_LIVE_QUOTE_FOR_SIZING")]
    engine.bridge.enqueue_event.assert_not_called()


async def test_strategy_orders_fill_on_the_next_quote():
    engine = spot_engine()
    strategy = MagicMock()
    strategy.trade_size = 0.02
    strategy.instrument_id = SPOT
    _patch_strategy_for_paper_engine(strategy, engine, "INST-STRAT")

    strategy.buy_market()
    assert len(engine.dispatcher.open_orders) == 1

    conn = AsyncMock()
    await match_resting_orders(pool_for(conn), engine, SPOT, 50000.0, 50010.0)

    assert engine.dispatcher.open_orders == {}
    assert engine.dispatcher.get_position_qty(SPOT) == D("0.02")
    evt = engine.bridge.enqueue_event.call_args.args[0]
    assert (evt["instance_id"], evt["side"], D(evt["price"])) == ("INST-STRAT", "BUY", D("50010"))
    assert "INSERT INTO paper_orders" in conn.execute.call_args.args[0]


async def test_latched_kill_switch_cancels_working_entries_but_exits_still_fill():
    engine = spot_engine()
    engine.dispatcher.positions[SPOT] = D("0.01")  # an existing long the exit can sell
    quotes = SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)
    await run_commands(engine, [
        order("E-1", "BUY", "LIMIT", "0.02", "49800"),   # entry, rests
        order("X-1", "SELL", "LIMIT", "0.01", "50200"),  # exit, rests
    ], quotes)
    assert {"E-1", "X-1"} <= set(engine.dispatcher.open_orders)

    engine.dispatcher.kill_switch.trip(latched_by="test", reason="test")
    conn = AsyncMock()
    # Would fill the entry: it is cancelled instead.
    await match_resting_orders(pool_for(conn), engine, SPOT, 49690.0, 49700.0)
    assert set(engine.dispatcher.open_orders) == {"X-1"}
    engine.bridge.enqueue_event.assert_not_called()
    # The exit still fills at its limit.
    await match_resting_orders(pool_for(AsyncMock()), engine, SPOT, 50250.0, 50260.0)

    assert engine.dispatcher.open_orders == {}
    assert engine.dispatcher.get_position_qty(SPOT) == D("0")
    evt = engine.bridge.enqueue_event.call_args.args[0]
    assert (evt["client_order_id"], D(evt["price"])) == ("X-1", D("50200"))
    assert any("status = 'CANCELED'" in c.args[0] for c in conn.execute.call_args_list)


def test_trip_via_mailbox_releases_working_entry_orders():
    engine = spot_engine()
    strategy = MagicMock()
    strategy.trade_size = 0.02
    strategy.instrument_id = SPOT
    _patch_strategy_for_paper_engine(strategy, engine, "INST-STRAT")
    strategy.buy_market()
    assert engine.dispatcher.reserved_cash > 0

    engine.mailbox.push({"command_type": "TRIP_KILL_SWITCH", "requester_id": "test", "payload": {"reason": "t"}})
    engine.process_mailbox_commands()

    assert engine.dispatcher.open_orders == {}
    assert engine.dispatcher.reserved_cash == 0


# --- Review regressions: working orders that outlive their position, retries, strategies ----------

async def run_rows(engine, rows, quote_cache, fetchval=None):
    """Drive command_poller over arbitrary command rows."""
    conn = AsyncMock()
    remaining = list(rows)

    async def fake_fetchrow(query, *args):
        if remaining:
            return remaining.pop(0)
        raise asyncio.CancelledError()

    conn.fetchrow = fake_fetchrow
    if fetchval is not None:
        conn.fetchval = fetchval
    with pytest.raises(asyncio.CancelledError):
        await command_poller(pool_for(conn), engine, quote_cache)
    return conn


def cmd(command_type, payload, command_id=None, target="INST-001"):
    return {"command_id": command_id or uuid.uuid4(), "command_type": command_type,
            "target_instance_id": target, "payload": json.dumps(payload)}


async def test_resting_reduce_only_perp_order_is_cancelled_once_its_position_is_gone():
    engine = make_engine(make_perp_dispatcher())
    engine.bridge = MagicMock()
    quotes = SimpleQuoteCache()
    quotes.set_quote(PERP, 50000.0, 50010.0)
    await run_commands(engine, [
        {"client_order_id": "P-OPEN", "instrument_id": "BTCUSDT-PERP", "side": "BUY", "quantity": "0.04", "leverage": 10},
        {"client_order_id": "P-RO", "instrument_id": "BTCUSDT-PERP", "side": "SELL", "order_type": "LIMIT",
         "price": "50500", "quantity": "0.04", "reduce_only": True},
    ], quotes)
    assert "P-RO" in engine.dispatcher.open_orders

    engine.dispatcher.flatten_perp_position(PERP, D("50000"), D("0"), "OTHER-EXIT")  # closed by another path
    await match_resting_orders(pool_for(AsyncMock()), engine, PERP, 50600.0, 50610.0)

    assert "P-RO" not in engine.dispatcher.open_orders
    assert engine.dispatcher.get_perp_position_qty(PERP) == 0
    assert engine.dispatcher.locked_margin == 0


def test_reduce_only_fill_without_a_position_never_opens_one():
    d = make_perp_dispatcher()
    d.open_orders["RO"] = {"intent": None, "reserved_notional": D("0"), "side": "SELL", "quantity": D("1"),
                           "instrument_id": PERP, "is_perp": True, "reduce_only": True}
    d.handle_fill("RO", D("1"), D("50000"), D("0"), side="SELL", instrument_id=PERP)
    assert d.get_perp_position_qty(PERP) == 0 and d.locked_margin == 0


async def test_resting_spot_exit_is_cancelled_after_the_position_was_sold_elsewhere():
    engine = spot_engine()
    engine.dispatcher.positions[SPOT] = D("0.01")
    quotes = SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)
    await run_commands(engine, [order("X-2", "SELL", "LIMIT", "0.01", "50200")], quotes)
    cash_before = engine.dispatcher.cash_balance

    engine.dispatcher.handle_fill("OTHER", D("0.01"), D("50000"), D("0"), side="SELL", instrument_id=SPOT)
    await match_resting_orders(pool_for(AsyncMock()), engine, SPOT, 50250.0, 50260.0)

    assert "X-2" not in engine.dispatcher.open_orders
    assert engine.dispatcher.cash_balance == cash_before + D("500")  # only the other sale, no phantom proceeds
    assert engine.dispatcher.reserved_positions.get(SPOT, D("0")) == 0


async def test_close_position_releases_the_instruments_working_orders_first():
    engine = spot_engine()
    engine.dispatcher.positions[SPOT] = D("0.01")
    quotes = SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)
    await run_commands(engine, [order("X-3", "SELL", "LIMIT", "0.01", "50200")], quotes)

    conn = await run_rows(engine, [cmd("CLOSE_POSITION", {"instrument_id": "BTCUSDT"})], quotes)

    assert engine.dispatcher.open_orders == {}
    assert engine.dispatcher.get_position_qty(SPOT) == 0
    assert any("status = 'CANCELED'" in c.args[0] for c in conn.execute.call_args_list)


async def test_close_position_without_a_quote_is_rejected_not_priced_by_the_caller():
    engine = spot_engine()
    engine.dispatcher.positions[SPOT] = D("0.01")
    conn = await run_rows(engine, [cmd("CLOSE_POSITION", {"instrument_id": "BTCUSDT", "price": "99999"})], SimpleQuoteCache())
    assert command_outcomes(conn) == [("REJECTED", "NO_LIVE_QUOTE")]
    assert engine.dispatcher.get_position_qty(SPOT) == D("0.01")


async def test_retried_command_does_not_place_a_second_order():
    engine = spot_engine()
    quotes = SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)
    command_id = uuid.uuid4()
    payload = {"instrument_id": "BTCUSDT", "side": "BUY", "order_type": "LIMIT", "quantity": "0.02", "price": "49800"}

    await run_rows(engine, [cmd("MANUAL_ORDER", payload, command_id)], quotes)
    reserved = engine.dispatcher.reserved_cash
    await run_rows(engine, [cmd("MANUAL_ORDER", payload, command_id)], quotes)  # claim retried

    assert len(engine.dispatcher.open_orders) == 1
    assert engine.dispatcher.reserved_cash == reserved


async def test_failed_transaction_releases_the_working_order():
    engine = spot_engine()
    quotes = SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)
    conn = AsyncMock()
    rows = [cmd("MANUAL_ORDER", {"client_order_id": "L-9", "instrument_id": "BTCUSDT", "side": "BUY",
                                 "order_type": "LIMIT", "quantity": "0.02", "price": "49800"})]

    async def fake_fetchrow(query, *args):
        if rows:
            return rows.pop(0)
        raise asyncio.CancelledError()

    async def failing_execute(sql, *args):
        if "'ACCEPTED'" in sql:
            raise RuntimeError("db down")

    conn.fetchrow, conn.execute = fake_fetchrow, failing_execute
    with pytest.raises(asyncio.CancelledError):
        await command_poller(pool_for(conn), engine, quotes)

    assert engine.dispatcher.open_orders == {}
    assert engine.dispatcher.reserved_cash == 0


async def test_failed_order_row_write_still_emits_the_fill_event():
    engine = spot_engine()
    intent = OrderIntent(client_order_id="S-1", instance_id="INST-STRAT", instrument_id=SPOT,
                         side="BUY", order_type="MARKET", quantity=D("0.02"))
    engine.dispatcher.dispatch(intent, lambda o: None, nautilus_order=None)
    conn = AsyncMock()
    conn.execute.side_effect = RuntimeError("db down")
    await match_resting_orders(pool_for(conn), engine, SPOT, 50000.0, 50010.0)

    assert engine.dispatcher.get_position_qty(SPOT) == D("0.02")
    evt = engine.bridge.enqueue_event.call_args.args[0]
    assert (evt["client_order_id"], evt["order_type"]) == ("S-1", "MARKET")


async def test_strategies_see_paper_fills_and_close_them():
    engine = spot_engine()
    strategy = MagicMock()
    strategy.trade_size = 0.02
    strategy.instrument_id = SPOT
    strategy.is_in_warmup.return_value = False
    _patch_strategy_for_paper_engine(strategy, engine, "INST-STRAT")
    assert strategy.is_flat() and not strategy.is_long()

    strategy.buy_market()
    await match_resting_orders(pool_for(AsyncMock()), engine, SPOT, 50000.0, 50010.0)
    assert strategy.is_long() and not strategy.is_flat()

    strategy.close_all()
    await match_resting_orders(pool_for(AsyncMock()), engine, SPOT, 50100.0, 50110.0)
    assert strategy.is_flat()
    assert engine.bridge.enqueue_event.call_args.args[0]["side"] == "SELL"


def test_unsupported_strategy_order_types_are_not_dispatched():
    engine = spot_engine()
    strategy = MagicMock()
    strategy.instrument_id = SPOT
    _patch_strategy_for_paper_engine(strategy, engine, "INST-STRAT")
    stop_order = MagicMock()
    stop_order.order_type.value = 3  # STOP_MARKET
    strategy.submit_order(stop_order)
    assert engine.dispatcher.open_orders == {}


class AbortingConn:
    """Models asyncpg inside a transaction: after a failed statement every later one fails until a
    savepoint (nested transaction) rolls back. The paper_orders FILLED upsert always fails."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.aborted = False
        self.executed = []

    async def fetchrow(self, query, *args):
        if self.rows:
            return self.rows.pop(0)
        raise asyncio.CancelledError()

    async def fetchval(self, query, *args):
        return None  # the bridge never wrote a paper_orders row

    async def execute(self, sql, *args):
        if self.aborted:
            raise RuntimeError("current transaction is aborted")
        if "'FILLED'" in sql and "INSERT INTO paper_orders" in sql:
            self.aborted = True
            raise RuntimeError("fk violation")
        self.executed.append((sql, args))

    def transaction(self):
        conn = self

        class Tx:
            async def __aenter__(self):
                self.was_aborted = conn.aborted

            async def __aexit__(self, exc_type, exc, tb):
                if exc_type is not None:
                    conn.aborted = self.was_aborted  # roll back to the savepoint
                return False

        return Tx()


async def test_failed_fill_upsert_in_the_poller_keeps_the_claim_and_fills_once():
    engine = spot_engine()
    quotes = SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)
    command_id = uuid.uuid4()
    payload = {"instrument_id": "BTCUSDT", "side": "BUY", "order_type": "MARKET", "quantity": "0.02"}

    conn = AbortingConn([cmd("MANUAL_ORDER", payload, command_id)])
    with pytest.raises(asyncio.CancelledError):
        await command_poller(pool_for(conn), engine, quotes)
    assert any("SET status = 'COMPLETED'" in sql for sql, _ in conn.executed)

    # Even if the claim were retried, the in-memory record stops a second fill.
    conn = AbortingConn([cmd("MANUAL_ORDER", payload, command_id)])
    with pytest.raises(asyncio.CancelledError):
        await command_poller(pool_for(conn), engine, quotes)

    assert engine.bridge.enqueue_event.call_count == 1
    assert engine.dispatcher.get_position_qty(SPOT) == D("0.02")


async def test_reused_client_order_id_from_another_command_is_rejected():
    engine = spot_engine()
    quotes = SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)
    await run_rows(engine, [cmd("MANUAL_ORDER", order("DUP-1", "BUY", "MARKET", "0.02"))], quotes)
    conn = await run_rows(engine, [cmd("MANUAL_ORDER", order("DUP-1", "BUY", "MARKET", "0.02"))], quotes)
    assert command_outcomes(conn) == [("REJECTED", "DUPLICATE_CLIENT_ORDER_ID")]
    assert engine.bridge.enqueue_event.call_count == 1


async def test_rejected_close_position_keeps_the_working_orders():
    engine = spot_engine()
    engine.dispatcher.positions[SPOT] = D("0.01")
    quotes = SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)
    await run_commands(engine, [order("X-4", "SELL", "LIMIT", "0.01", "50200")], quotes)
    await run_rows(engine, [cmd("CLOSE_POSITION", {"instrument_id": "BTCUSDT"})], SimpleQuoteCache())
    assert "X-4" in engine.dispatcher.open_orders


def test_perp_strategy_close_all_sends_a_reduce_only_order():
    engine = make_engine(make_perp_dispatcher())
    engine.bridge = MagicMock()
    engine.dispatcher.set_perp_position(PERP, D("-0.04"), D("50000"), 10, "CROSS", "INST-STRAT")
    strategy = MagicMock()
    strategy.instrument_id = PERP
    strategy.is_in_warmup.return_value = False
    _patch_strategy_for_paper_engine(strategy, engine, "INST-STRAT")
    assert strategy.is_short()

    strategy.close_all()
    (info,) = engine.dispatcher.open_orders.values()
    assert (info["side"], info["quantity"], info["reduce_only"]) == ("BUY", D("0.04"), True)


async def test_retry_after_a_failed_accept_places_the_working_order_again():
    engine = spot_engine()
    quotes = SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)
    command_id = uuid.uuid4()
    payload = {"instrument_id": "BTCUSDT", "side": "BUY", "order_type": "LIMIT", "quantity": "0.02", "price": "49800"}

    class FailingAccept(AbortingConn):
        async def execute(self, sql, *args):
            if "'ACCEPTED'" in sql:  # transient: retried. A generic error is settled instead (test_command_queue_guard)
                raise ConnectionResetError("connection lost")
            await super().execute(sql, *args)

    conn = FailingAccept([cmd("MANUAL_ORDER", payload, command_id)])
    with pytest.raises(asyncio.CancelledError):
        await command_poller(pool_for(conn), engine, quotes)
    assert engine.dispatcher.open_orders == {} and engine.dispatcher.reserved_cash == 0

    conn = AbortingConn([cmd("MANUAL_ORDER", payload, command_id)])  # the claim is retried
    with pytest.raises(asyncio.CancelledError):
        await command_poller(pool_for(conn), engine, quotes)
    assert len(engine.dispatcher.open_orders) == 1
    assert engine.dispatcher.reserved_cash > 0


# --- R7.3: real top-of-book sizes reach the matcher ----------------------------------------------


async def test_resting_market_order_larger_than_the_ask_size_pays_impact():
    engine = spot_engine()
    await run_commands(engine, [order("M-2", "BUY", "MARKET", "0.02")], SimpleQuoteCache())
    # ask shows 0.01: the order is 2x the size, one extra multiple = 5 bps above the ask
    await match_resting_orders(pool_for(AsyncMock()), engine, SPOT, 50000.0, 50010.0, "3.0", "0.01")
    assert D(engine.bridge.enqueue_event.call_args.args[0]["price"]) == D("50035.00500000")


async def test_quote_without_sizes_keeps_the_touch():
    engine = spot_engine()
    await run_commands(engine, [order("M-3", "BUY", "MARKET", "0.02")], SimpleQuoteCache())
    await match_resting_orders(pool_for(AsyncMock()), engine, SPOT, 50000.0, 50010.0)
    assert D(engine.bridge.enqueue_event.call_args.args[0]["price"]) == D("50010")


async def test_command_poller_uses_sizes_only_when_the_feed_sent_them():
    with_sizes, defaults = SimpleQuoteCache(), SimpleQuoteCache()
    with_sizes.set_quote(SPOT, 50000.0, 50010.0, 3.0, 0.01, sizes_known=True)
    defaults.set_quote(SPOT, 50000.0, 50010.0)  # 1.0 placeholders, not real sizes
    assert with_sizes.quote(SPOT).sizes_known is True and defaults.quote(SPOT).sizes_known is False

    engine = spot_engine()
    await run_commands(engine, [order("L-3", "BUY", "LIMIT", "0.02", "50100")], with_sizes)
    assert D(engine.bridge.enqueue_event.call_args.args[0]["price"]) == D("50035.00500000")

    engine = spot_engine()
    await run_commands(engine, [order("L-4", "BUY", "LIMIT", "0.02", "50100")], defaults)
    assert D(engine.bridge.enqueue_event.call_args.args[0]["price"]) == D("50010")
