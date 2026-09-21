"""Test GuardedOrderDispatcher synchronous risk guardrails, spot cash invariant, and collars."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from cryptonite_execution.ingress import IngressHealthMonitor
from cryptonite_execution.kill_switch import LatchedKillSwitch
from cryptonite_execution.risk import GuardedOrderDispatcher, OrderIntent
from nautilus_trader.model import InstrumentId, Price, Quantity, QuoteTick


def make_fresh_dispatcher(starting_cash: Decimal = Decimal("10000.0")) -> tuple[GuardedOrderDispatcher, MagicMock]:
    kill_switch = LatchedKillSwitch()
    monitor = IngressHealthMonitor(max_staleness_s=5.0)
    monitor.is_fresh = MagicMock(return_value=True)

    mock_cache = MagicMock()
    inst = InstrumentId.from_str("BTCUSDT.BINANCE")
    quote = QuoteTick(
        instrument_id=inst,
        bid_price=Price.from_str("50000.00"),
        ask_price=Price.from_str("50010.00"),
        bid_size=Quantity.from_str("1.0"),
        ask_size=Quantity.from_str("1.0"),
        ts_event=1_700_000_000_000_000_000,
        ts_init=1_700_000_000_000_000_000,
    )
    mock_cache.quote.return_value = quote

    dispatcher = GuardedOrderDispatcher(
        account_id="PAPER-ACCOUNT-001",
        starting_cash=starting_cash,
        kill_switch=kill_switch,
        ingress_health=monitor,
        node_cache=mock_cache,
        collar_pct=Decimal("0.02"),           # 2% collar
        slippage_buffer_pct=Decimal("0.005"),  # 0.5% slippage buffer
        taker_fee=Decimal("0.001"),            # 0.1% fee
    )
    return dispatcher, mock_cache


def test_spot_cash_invariant_rejects_short_selling():
    """Verify that selling without a long position (or selling more than held) is rejected."""
    dispatcher, _ = make_fresh_dispatcher(starting_cash=Decimal("10000.0"))

    # No position exists
    sell_intent = OrderIntent(
        client_order_id="SELL-001",
        instance_id="INST-001",
        instrument_id="BTCUSDT.BINANCE",
        side="SELL",
        order_type="MARKET",
        quantity=Decimal("0.1"),
    )
    approved, reason, _ = dispatcher.evaluate_intent(sell_intent)
    assert approved is False
    assert "SPOT_INVARIANT_VIOLATION" in reason

    # Grant position of 0.05 BTC and attempt to sell 0.10 BTC
    dispatcher.positions["BTCUSDT.BINANCE"] = Decimal("0.05")
    approved, reason, _ = dispatcher.evaluate_intent(sell_intent)
    assert approved is False
    assert "SPOT_INVARIANT_VIOLATION" in reason

    # Attempt to sell 0.05 BTC -> approved!
    valid_sell = OrderIntent(
        client_order_id="SELL-002",
        instance_id="INST-001",
        instrument_id="BTCUSDT.BINANCE",
        side="SELL",
        order_type="MARKET",
        quantity=Decimal("0.05"),
    )
    approved, reason, _ = dispatcher.evaluate_intent(valid_sell)
    assert approved is True
    assert reason == "APPROVED"


def test_asymmetric_price_collars():
    """Verify that buy orders exceeding ask*(1+collar) and sell orders below bid*(1-collar) are rejected."""
    dispatcher, _ = make_fresh_dispatcher(starting_cash=Decimal("10000.0"))
    dispatcher.positions["BTCUSDT.BINANCE"] = Decimal("1.0")

    # Ask is 50010.00, collar is 2% -> max allowable buy price = 50010 * 1.02 = 51010.20
    bad_buy = OrderIntent(
        client_order_id="BUY-COLLAR",
        instance_id="INST-001",
        instrument_id="BTCUSDT.BINANCE",
        side="BUY",
        order_type="LIMIT",
        price=Decimal("51500.00"),  # breaches collar
        quantity=Decimal("0.1"),
    )
    approved, reason, _ = dispatcher.evaluate_intent(bad_buy)
    assert approved is False
    assert "PRICE_COLLAR_REJECT" in reason

    # Bid is 50000.00, collar is 2% -> min allowable sell price = 50000 * 0.98 = 49000.00
    bad_sell = OrderIntent(
        client_order_id="SELL-COLLAR",
        instance_id="INST-001",
        instrument_id="BTCUSDT.BINANCE",
        side="SELL",
        order_type="LIMIT",
        price=Decimal("48500.00"),  # breaches collar
        quantity=Decimal("0.1"),
    )
    approved, reason, _ = dispatcher.evaluate_intent(bad_sell)
    assert approved is False
    assert "PRICE_COLLAR_REJECT" in reason


def test_conservative_cash_reservation_and_fill_reconciliation():
    """Verify conservative market buy cash reservation, subsequent rejection on insufficient cash,
    and fill reconciliation.
    """
    # Start with 6,000 USDT
    dispatcher, _ = make_fresh_dispatcher(starting_cash=Decimal("6000.0"))

    # Market buy 0.1 BTC:
    # effective_price = ask(50010) * (1 + 0.02 + 0.005) = 50010 * 1.025 = 51260.25
    # required_cash = 0.1 * 51260.25 * 1.001 = 5131.151025 USDT
    buy_intent = OrderIntent(
        client_order_id="BUY-001",
        instance_id="INST-001",
        instrument_id="BTCUSDT.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.1"),
    )

    mock_submit = MagicMock()
    success, status = dispatcher.dispatch(buy_intent, mock_submit, nautilus_order=MagicMock())
    assert success is True
    assert status == "SUBMITTED"
    assert dispatcher.reserved_cash > Decimal("5100.0")
    assert dispatcher.available_cash < Decimal("900.0")

    # Second buy order requiring 5,131 USDT must be rejected due to insufficient available cash
    buy_intent_2 = OrderIntent(
        client_order_id="BUY-002",
        instance_id="INST-001",
        instrument_id="BTCUSDT.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.1"),
    )
    approved, reason, _ = dispatcher.evaluate_intent(buy_intent_2)
    assert approved is False
    assert "INSUFFICIENT_CASH" in reason

    # When BUY-001 fills at 50,010.00 with 5.001 commission:
    fill_price = Decimal("50010.00")
    fill_qty = Decimal("0.1")
    commission = Decimal("5.001")
    dispatcher.handle_fill(
        client_order_id="BUY-001",
        fill_qty=fill_qty,
        fill_price=fill_price,
        commission=commission,
    )

    # Cash reservation released, cash deducted, position credited
    assert dispatcher.reserved_cash == Decimal("0")
    assert dispatcher.cash_balance == Decimal("6000.0") - (fill_qty * fill_price + commission)
    assert dispatcher.positions["BTCUSDT.BINANCE"] == fill_qty


def test_utc_daily_loss_latch():
    """Verify that exceeding allowable daily drawdown immediately engages daily loss latch."""
    dispatcher, _ = make_fresh_dispatcher(starting_cash=Decimal("10000.0"))
    dispatcher.max_daily_loss_pct = Decimal("0.05")  # 5% max drawdown

    # 10,000 max -> 9,600 (4% drawdown) -> latch remains open
    # We call it with 10000 first to establish the HWM
    dispatcher.check_daily_drawdown(Decimal("10000.0"))
    tripped = dispatcher.check_daily_drawdown(Decimal("9600.0"))
    assert tripped is False
    assert not dispatcher.daily_loss_latched

    # 10,000 -> 9,450 (5.5% drawdown) -> trips latch!
    tripped = dispatcher.check_daily_drawdown(Decimal("9450.0"))
    assert tripped is True
    assert dispatcher.daily_loss_latched is True

    # Subsequent entry order is rejected
    buy_intent = OrderIntent(
        client_order_id="BUY-AFTER-DRAWDOWN",
        instance_id="INST-001",
        instrument_id="BTCUSDT.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.01"),
    )
    approved, reason, _ = dispatcher.evaluate_intent(buy_intent)
    assert approved is False
    assert reason == "DAILY_LOSS_LIMIT_BREACHED"
