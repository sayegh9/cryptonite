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


def test_unknown_top_size_keeps_the_touch():
    assert match_order("C1", order("BUY", "MARKET", qty="50"), D("100"), D("101")).price == D("101")


def test_order_within_top_size_takes_the_touch():
    fill = match_order("C1", order("BUY", "MARKET", qty="0.5"), D("100"), D("101"), D("2"), D("1"))
    assert fill.price == D("101")


def test_order_larger_than_top_size_pays_impact_against_it():
    # 3x the ask size: 2 extra multiples x 5 bps = 10 bps above the ask
    fill = match_order("C1", order("BUY", "MARKET", qty="3"), D("100"), D("101"), D("9"), D("1"))
    assert fill.price == D("101.10100000")
    # the bid side uses the bid size
    fill = match_order("C1", order("SELL", "MARKET", qty="3"), D("100"), D("101"), D("1"), D("9"))
    assert fill.price == D("99.90000000")


def test_impact_is_capped():
    fill = match_order("C1", order("BUY", "MARKET", qty="1000"), D("100"), D("101"), D("1"), D("1"))
    assert fill.price == D("101.50500000")  # 50 bps cap


def test_marketable_limit_impact_never_exceeds_the_limit():
    fill = match_order("C1", order("BUY", "LIMIT", "101.05", qty="3"), D("100"), D("101"), D("9"), D("1"))
    assert (fill.price, fill.liquidity) == (D("101.05"), TAKER)


def test_maker_fill_ignores_size():
    fill = match_order("C1", order("BUY", "LIMIT", "100.5", qty="50", rested=True), D("99"), D("100"), D("1"), D("1"))
    assert (fill.price, fill.liquidity) == (D("100.5"), MAKER)
