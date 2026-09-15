"""Test LatchedKillSwitch state machine, emergency exit bypass, and net-zero reset constraints."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from cryptonite_execution.ingress import IngressHealthMonitor
from cryptonite_execution.kill_switch import LatchedKillSwitch
from cryptonite_execution.risk import GuardedOrderDispatcher, OrderIntent
from nautilus_trader.model import InstrumentId, Price, Quantity, QuoteTick


def test_kill_switch_lifecycle_and_reset_constraints():
    """Verify latched state machine, emergency exit bypass, and strict reset audit."""
    kill_switch = LatchedKillSwitch()
    assert not kill_switch.is_latched

    # 1. Trip kill switch
    kill_switch.trip(latched_by="operator-alice", reason="Flash crash detected")
    assert kill_switch.is_latched is True
    snap = kill_switch.snapshot()
    assert snap["latched_by"] == "operator-alice"
    assert snap["reason"] == "Flash crash detected"

    # 2. Verify GuardedOrderDispatcher rejects regular entries
    monitor = IngressHealthMonitor()
    monitor.is_fresh = MagicMock(return_value=True)
    mock_cache = MagicMock()
    inst = InstrumentId.from_str("BTCUSDT.BINANCE")
    mock_cache.quote.return_value = QuoteTick(
        instrument_id=inst,
        bid_price=Price.from_str("50000.00"),
        ask_price=Price.from_str("50010.00"),
        bid_size=Quantity.from_str("1.0"),
        ask_size=Quantity.from_str("1.0"),
        ts_event=1_700_000_000_000_000_000,
        ts_init=1_700_000_000_000_000_000,
    )

    dispatcher = GuardedOrderDispatcher(
        account_id="PAPER-ACCOUNT-001",
        starting_cash=Decimal("10000.0"),
        kill_switch=kill_switch,
        ingress_health=monitor,
        node_cache=mock_cache,
    )
    dispatcher.positions["BTCUSDT.BINANCE"] = Decimal("0.5")

    # Regular buy -> rejected
    buy_intent = OrderIntent(
        client_order_id="BUY-WHILE-LATCHED",
        instance_id="INST-001",
        instrument_id="BTCUSDT.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.1"),
        is_kill_switch_exit=False,
    )
    approved, reason, _ = dispatcher.evaluate_intent(buy_intent)
    assert approved is False
    assert reason == "KILL_SWITCH_ENGAGED"

    # Internal reduce-only exit order -> APPROVED bypass!
    flatten_intent = OrderIntent(
        client_order_id="FLATTEN-EXIT",
        instance_id="INST-001",
        instrument_id="BTCUSDT.BINANCE",
        side="SELL",
        order_type="MARKET",
        quantity=Decimal("0.5"),
        is_kill_switch_exit=True,
    )
    approved, reason, _ = dispatcher.evaluate_intent(flatten_intent)
    assert approved is True
    assert reason == "APPROVED"

    # 3. Attempt reset while positions or orders remain open -> FAILS
    assert not kill_switch.can_reset(open_orders_count=0, open_positions_count=1)
    reset_ok = kill_switch.reset("operator-bob", open_orders_count=0, open_positions_count=1)
    assert reset_ok is False
    assert kill_switch.is_latched is True

    # 4. Attempt reset while orders remain open -> FAILS
    assert not kill_switch.can_reset(open_orders_count=2, open_positions_count=0)
    reset_ok = kill_switch.reset("operator-bob", open_orders_count=2, open_positions_count=0)
    assert reset_ok is False
    assert kill_switch.is_latched is True

    # 5. When open orders == 0 AND open positions == 0 -> RESET SUCCEEDS!
    assert kill_switch.can_reset(open_orders_count=0, open_positions_count=0)
    reset_ok = kill_switch.reset("operator-bob", open_orders_count=0, open_positions_count=0)
    assert reset_ok is True
    assert not kill_switch.is_latched
    snap = kill_switch.snapshot()
    assert snap["reset_by"] == "operator-bob"
