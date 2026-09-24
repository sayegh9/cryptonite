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
    # Start with 30,000 USDT
    dispatcher, _ = make_fresh_dispatcher(starting_cash=Decimal("30000.0"))

    # Market buy 0.1 BTC:
    # effective_price = ask(50010) * (1 + 0.02 + 0.005) = 50010 * 1.025 = 51260.25
    # required_cash = 0.1 * 51260.25 * 1.001 = 5131.151025 USDT
    # concentration = 0.1 * 51260.25 / 30000 = 17.1% (within the 25% limit)
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
    assert dispatcher.available_cash < Decimal("24900.0")

    # Second buy order requiring 25,656 USDT must be rejected due to insufficient available cash
    buy_intent_2 = OrderIntent(
        client_order_id="BUY-002",
        instance_id="INST-001",
        instrument_id="BTCUSDT.BINANCE",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.5"),
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
    assert dispatcher.cash_balance == Decimal("30000.0") - (fill_qty * fill_price + commission)
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


# --- Perpetual futures guards ---------------------------------------------------

PERP = "BTCUSDT-PERP.BINANCE"


def make_perp_dispatcher(mark: str = "50000") -> GuardedOrderDispatcher:
    """10,000 USDT account with perps enabled; quote bid 50,000 / ask 50,010 as above."""
    dispatcher, _ = make_fresh_dispatcher(starting_cash=Decimal("10000.0"))
    dispatcher.perp_trading_enabled = True
    dispatcher.update_mark_price(PERP, Decimal(mark))
    return dispatcher


def perp_intent(side: str, qty: str, cid: str = "PERP-001", **flags) -> OrderIntent:
    return OrderIntent(
        client_order_id=cid,
        instance_id="INST-001",
        instrument_id=PERP,
        side=side,
        order_type=flags.pop("order_type", "MARKET"),
        quantity=Decimal(qty),
        **flags,
    )


def test_perp_kill_flag_rejects_perp_orders():
    dispatcher, _ = make_fresh_dispatcher()
    assert dispatcher.perp_trading_enabled is True  # on by default since perp fill accounting
    dispatcher.perp_trading_enabled = False
    approved, reason, _ = dispatcher.evaluate_intent(perp_intent("SELL", "0.01"))
    assert (approved, reason) == (False, "PERP_TRADING_DISABLED")


@pytest.mark.parametrize("leverage", [0, -1, 21, 125])
def test_perp_rejects_leverage_outside_1_to_20(leverage):
    dispatcher = make_perp_dispatcher()
    approved, reason, _ = dispatcher.evaluate_intent(perp_intent("BUY", "0.01", leverage=leverage))
    assert approved is False and reason.startswith("LEVERAGE_OUT_OF_RANGE")


@pytest.mark.parametrize("leverage", [1, 20])
def test_perp_accepts_leverage_bounds(leverage):
    dispatcher = make_perp_dispatcher()
    approved, reason, _ = dispatcher.evaluate_intent(perp_intent("BUY", "0.01", leverage=leverage))
    assert (approved, reason) == (True, "APPROVED")


def test_perp_defaults_leverage_1_and_cross_and_validates_margin_mode():
    dispatcher = make_perp_dispatcher()
    intent = perp_intent("BUY", "0.01")
    assert dispatcher.evaluate_intent(intent)[0] is True
    assert (intent.leverage, intent.margin_mode) == (1, "CROSS")

    isolated = perp_intent("BUY", "0.01", margin_mode="isolated")
    assert dispatcher.evaluate_intent(isolated)[0] is True
    assert isolated.margin_mode == "ISOLATED"

    approved, reason, _ = dispatcher.evaluate_intent(perp_intent("BUY", "0.01", margin_mode="PORTFOLIO"))
    assert approved is False and reason.startswith("INVALID_MARGIN_MODE")


@pytest.mark.parametrize(
    ("flags", "code"),
    [
        ({"leverage": 5}, "SPOT_LEVERAGE_NOT_ALLOWED"),
        ({"margin_mode": "CROSS"}, "SPOT_DERIVATIVE_FLAG_NOT_ALLOWED"),
        ({"reduce_only": True}, "SPOT_DERIVATIVE_FLAG_NOT_ALLOWED"),
    ],
)
def test_spot_rejects_derivative_flags(flags, code):
    dispatcher, _ = make_fresh_dispatcher()
    intent = OrderIntent(
        client_order_id="SPOT-FLAGS", instance_id="INST-001", instrument_id="BTCUSDT.BINANCE",
        side="BUY", order_type="MARKET", quantity=Decimal("0.01"), **flags,
    )
    approved, reason, _ = dispatcher.evaluate_intent(intent)
    assert approved is False and reason.startswith(code)


def test_spot_leverage_1_is_accepted():
    dispatcher, _ = make_fresh_dispatcher()
    intent = OrderIntent(
        client_order_id="SPOT-L1", instance_id="INST-001", instrument_id="BTCUSDT.BINANCE",
        side="BUY", order_type="MARKET", quantity=Decimal("0.01"), leverage=1,
    )
    assert dispatcher.evaluate_intent(intent)[:2] == (True, "APPROVED")


def test_perp_concentration_uses_mark_price_notional():
    # Equity 10,000 -> cap 2,500 notional at mark
    dispatcher = make_perp_dispatcher(mark="50000")
    assert dispatcher.evaluate_intent(perp_intent("BUY", "0.05"))[0] is True  # exactly 25%
    approved, reason, _ = dispatcher.evaluate_intent(perp_intent("SELL", "0.0501"))
    assert approved is False and reason.startswith("CONCENTRATION_LIMIT_BREACHED")

    # Mark (not the 50,010 ask) drives the check: 0.0625 * 40,000 = 2,500
    dispatcher.update_mark_price(PERP, Decimal("40000"))
    assert dispatcher.evaluate_intent(perp_intent("BUY", "0.0625"))[0] is True


def test_perp_concentration_counts_existing_position():
    dispatcher = make_perp_dispatcher()
    dispatcher.set_perp_position(PERP, Decimal("0.04"), Decimal("50000"))
    approved, reason, _ = dispatcher.evaluate_intent(perp_intent("BUY", "0.02"))  # |0.06| * 50k = 30%
    assert approved is False and reason.startswith("CONCENTRATION_LIMIT_BREACHED")
    # Non-reduce-only flip from long 0.04 to short 0.02: projected |−0.02| * 50k = 10%
    assert dispatcher.evaluate_intent(perp_intent("SELL", "0.06"))[0] is True


def test_perp_min_order_floor_and_reduce_only_bypass():
    dispatcher = make_perp_dispatcher()  # 1% of 10,000 = 100 notional
    approved, reason, _ = dispatcher.evaluate_intent(perp_intent("BUY", "0.0019"))  # 95
    assert approved is False and reason.startswith("ORDER_BELOW_MIN_NOTIONAL")
    assert dispatcher.evaluate_intent(perp_intent("BUY", "0.002"))[0] is True  # exactly 100

    dispatcher.set_perp_position(PERP, Decimal("0.01"), Decimal("50000"))
    assert dispatcher.evaluate_intent(perp_intent("SELL", "0.0001", reduce_only=True))[:2] == (True, "APPROVED")


def test_reduce_only_rejections():
    dispatcher = make_perp_dispatcher()
    assert dispatcher.evaluate_intent(perp_intent("SELL", "0.01", reduce_only=True))[1] == "REDUCE_ONLY_NO_POSITION"

    dispatcher.set_perp_position(PERP, Decimal("0.04"), Decimal("50000"))  # long
    assert dispatcher.evaluate_intent(perp_intent("BUY", "0.01", reduce_only=True))[1] == "REDUCE_ONLY_INVALID_DIRECTION"
    approved, reason, _ = dispatcher.evaluate_intent(perp_intent("SELL", "0.05", reduce_only=True))
    assert approved is False and reason.startswith("REDUCE_ONLY_OVERSIZE")
    assert dispatcher.evaluate_intent(perp_intent("SELL", "0.04", reduce_only=True))[:2] == (True, "APPROVED")

    dispatcher.set_perp_position(PERP, Decimal("-0.04"), Decimal("50000"))  # short
    assert dispatcher.evaluate_intent(perp_intent("SELL", "0.01", reduce_only=True))[1] == "REDUCE_ONLY_INVALID_DIRECTION"
    assert dispatcher.evaluate_intent(perp_intent("BUY", "0.04", reduce_only=True))[0] is True


def test_reduce_only_oversize_counts_resting_reduce_only_orders():
    dispatcher = make_perp_dispatcher()
    dispatcher.set_perp_position(PERP, Decimal("0.04"), Decimal("50000"))
    ok, _ = dispatcher.dispatch(perp_intent("SELL", "0.03", "RO-1", reduce_only=True), MagicMock(), MagicMock())
    assert ok is True
    approved, reason, _ = dispatcher.evaluate_intent(perp_intent("SELL", "0.02", "RO-2", reduce_only=True))
    assert approved is False and reason.startswith("REDUCE_ONLY_OVERSIZE")  # only 0.01 still closable
    dispatcher.release_reservation("RO-1")
    assert dispatcher.evaluate_intent(perp_intent("SELL", "0.02", "RO-2", reduce_only=True))[0] is True


def test_reduce_only_skips_margin_reservation_concentration_and_conviction():
    dispatcher = make_perp_dispatcher()
    dispatcher.set_perp_position(PERP, Decimal("0.2"), Decimal("50000"))  # 100% of equity: over the cap
    dispatcher.reserved_cash = dispatcher.cash_balance  # no free margin at all
    intent = perp_intent("SELL", "0.2", reduce_only=True, conviction_level="OVERWEIGHT")
    ok, status = dispatcher.dispatch(intent, MagicMock(), MagicMock())
    assert (ok, status) == (True, "SUBMITTED")
    assert intent.quantity == Decimal("0.2")  # conviction multiplier not applied to a close
    assert dispatcher.reserved_cash == dispatcher.cash_balance  # nothing extra reserved


def test_perp_entry_reserves_initial_margin_and_releases_it():
    dispatcher = make_perp_dispatcher()
    # 0.04 BTC at mark 50,000, 10x: IM 200 + taker fee 2,000 * 0.001 = 202
    ok, _ = dispatcher.dispatch(perp_intent("SELL", "0.04", "SHORT-1", leverage=10), MagicMock(), MagicMock())
    assert ok is True
    assert dispatcher.reserved_cash == Decimal("202")
    dispatcher.release_reservation("SHORT-1")
    assert dispatcher.reserved_cash == Decimal("0")


def test_perp_flip_reserves_margin_only_for_opened_exposure():
    dispatcher = make_perp_dispatcher()
    dispatcher.set_perp_position(PERP, Decimal("0.02"), Decimal("50000"))
    # Sell 0.05: closes 0.02, opens 0.03 short -> IM 0.03 * 50k / 10 = 150, fee 0.05 * 50k * 0.001 = 2.5
    approved, _, margin = dispatcher.evaluate_intent(perp_intent("SELL", "0.05", leverage=10))
    assert approved is True and margin == Decimal("152.5")


def test_perp_rejects_insufficient_margin_and_missing_mark():
    dispatcher = make_perp_dispatcher()
    dispatcher.reserved_cash = Decimal("9900")  # 100 available
    approved, reason, _ = dispatcher.evaluate_intent(perp_intent("BUY", "0.04", leverage=1))  # needs 2,002
    assert approved is False and reason.startswith("INSUFFICIENT_MARGIN")

    dispatcher.mark_prices.clear()
    assert dispatcher.evaluate_intent(perp_intent("BUY", "0.01"))[1] == "NO_MARK_PRICE"

