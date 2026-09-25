"""Paper order matching rules (cryptonite_execution.matching)."""

from decimal import Decimal

from cryptonite_execution.matching import MAKER, TAKER, match_order, match_working_orders
from cryptonite_execution.risk import OrderIntent

D = Decimal
SPOT = "BTCUSDT.BINANCE"


def order(side: str, order_type: str, price: str | None = None, qty: str = "0.1", inst: str = SPOT, rested=False):
    intent = OrderIntent(
        client_order_id="C1", instance_id="I1", instrument_id=inst, side=side,
        order_type=order_type, quantity=D(qty), price=D(price) if price else None,
    )
    info = {"intent": intent, "side": side, "quantity": D(qty), "instrument_id": inst}
    if rested:
        info["rested"] = True
    return info


def test_market_orders_take_the_touch():
    assert match_order("C1", order("BUY", "MARKET"), D("100"), D("101")).price == D("101")
    fill = match_order("C1", order("SELL", "MARKET"), D("100"), D("101"))
    assert (fill.price, fill.liquidity) == (D("100"), TAKER)


def test_marketable_limit_on_arrival_fills_at_the_quote_not_the_limit():
    fill = match_order("C1", order("BUY", "LIMIT", "105"), D("100"), D("101"))
    assert (fill.price, fill.liquidity) == (D("101"), TAKER)
    fill = match_order("C1", order("SELL", "LIMIT", "95"), D("100"), D("101"))
    assert (fill.price, fill.liquidity) == (D("100"), TAKER)


def test_non_marketable_limit_does_not_fill():
    assert match_order("C1", order("BUY", "LIMIT", "99"), D("100"), D("101")) is None
    assert match_order("C1", order("SELL", "LIMIT", "102"), D("100"), D("101")) is None


def test_rested_limit_fills_at_its_limit_even_on_a_gap():
    # Ask gaps from 101 down to 90 through a 99 buy limit: filled at 99, no price improvement.
    fill = match_order("C1", order("BUY", "LIMIT", "99", rested=True), D("89"), D("90"))
    assert (fill.price, fill.liquidity) == (D("99"), MAKER)
    fill = match_order("C1", order("SELL", "LIMIT", "102", rested=True), D("110"), D("111"))
    assert (fill.price, fill.liquidity) == (D("102"), MAKER)


def test_touching_the_limit_fills():
    assert match_order("C1", order("BUY", "LIMIT", "101", rested=True), D("100"), D("101")).price == D("101")


def test_invalid_quotes_and_orders_never_fill():
    assert match_order("C1", order("BUY", "MARKET"), D("0"), D("0")) is None
    assert match_order("C1", order("BUY", "LIMIT"), D("100"), D("101")) is None  # limit without price
    assert match_order("C1", {"intent": None, "side": "SELL", "quantity": D("1"), "instrument_id": SPOT},
                       D("100"), D("101")) is None


def test_match_working_orders_filters_by_instrument_and_marks_rest():
    open_orders = {
        "A": order("BUY", "LIMIT", "99"),
        "B": order("BUY", "MARKET"),
        "C": order("BUY", "MARKET", inst="ETHUSDT.BINANCE"),
    }
    fills = match_working_orders(open_orders, SPOT, D("100"), D("101"))
    assert [f.client_order_id for f in fills] == ["B"]
    assert open_orders["A"]["rested"] is True
    assert "rested" not in open_orders["C"]
    # The rested limit later fills as maker at its limit.
    fills = match_working_orders(open_orders, SPOT, D("97"), D("98"))
    assert [(f.client_order_id, f.price, f.liquidity) for f in fills if f.client_order_id == "A"] == [("A", D("99"), MAKER)]
