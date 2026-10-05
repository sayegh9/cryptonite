"""Paper order matching against the live top of book.

Pure functions: they decide *whether* and *at what price* a working order fills. Applying the
fill (dispatcher ledger, paper_orders, bridge events) stays with the caller.

Rules (paper, top of book only, full size, no queue position):
- MARKET fills at the ask (BUY) or bid (SELL): taker. When the top-of-book size is known and the
  order is larger, the price moves against the order by ``IMPACT_BPS_PER_TOP_SIZE`` per extra
  multiple of that size, capped at ``IMPACT_CAP_BPS`` (no deeper book is recorded, so this stands
  in for walking it). Unknown size: the touch, as before.
- LIMIT that is marketable when it first meets a quote fills at that quote (taker, price
  improvement allowed, never worse than the limit).
- LIMIT that rested fills at its limit price once the opposite side trades through it (maker).
  A gap through the limit gives no price improvement, as for a resting order on a real book.
- No quote, or a non-positive quote, never fills.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

TAKER = "TAKER"
MAKER = "MAKER"
IMPACT_BPS_PER_TOP_SIZE = Decimal("5")
IMPACT_CAP_BPS = Decimal("50")
PRICE_STEP = Decimal("0.00000001")


def taker_price(touch: Decimal, side: str, quantity: Decimal, top_size: Decimal | None) -> Decimal:
    """The touch, moved against the order when it is larger than the size shown at the touch."""
    if top_size is None or top_size <= 0 or quantity <= top_size:
        return touch
    bps = min(IMPACT_CAP_BPS, IMPACT_BPS_PER_TOP_SIZE * (quantity / top_size - 1))
    factor = 1 + bps / 10_000 if side == "BUY" else 1 - bps / 10_000
    return (touch * factor).quantize(PRICE_STEP)


@dataclass(frozen=True)
class PaperFill:
    client_order_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal
    liquidity: str


def match_order(
    client_order_id: str, order_info: dict[str, Any], bid: Decimal, ask: Decimal,
    bid_size: Decimal | None = None, ask_size: Decimal | None = None,
) -> PaperFill | None:
    """Return the fill for one working order at this quote, or None if it keeps working.

    ``order_info`` is a ``GuardedOrderDispatcher.open_orders`` entry; ``order_info["rested"]``
    is True once the order has been seen by a quote without filling.
    """
    intent = order_info.get("intent")
    if intent is None or bid <= 0 or ask <= 0:
        return None
    side = order_info["side"]
    order_type = intent.order_type.upper()
    touch = ask if side == "BUY" else bid
    top_size = ask_size if side == "BUY" else bid_size
    quantity = order_info["quantity"]

    if order_type == "MARKET":
        price, liquidity = taker_price(touch, side, quantity, top_size), TAKER
    elif order_type == "LIMIT":
        limit = intent.price
        if limit is None or limit <= 0:
            return None
        crossed = touch <= limit if side == "BUY" else touch >= limit
        if not crossed:
            return None
        if order_info.get("rested"):
            price, liquidity = limit, MAKER
        else:  # marketable on arrival: taker, never worse than the limit
            impacted = taker_price(touch, side, quantity, top_size)
            price, liquidity = (min(impacted, limit) if side == "BUY" else max(impacted, limit)), TAKER
    else:
        return None

    return PaperFill(
        client_order_id=client_order_id,
        instrument_id=order_info["instrument_id"],
        side=side,
        quantity=quantity,
        price=price,
        liquidity=liquidity,
    )


def match_working_orders(
    open_orders: dict[str, dict[str, Any]], instrument_id: str, bid: Decimal, ask: Decimal,
    bid_size: Decimal | None = None, ask_size: Decimal | None = None,
) -> list[PaperFill]:
    """Match every working order on ``instrument_id``; orders that don't fill are marked rested."""
    fills = []
    for cid, info in list(open_orders.items()):
        if info.get("instrument_id") != instrument_id or info.get("intent") is None:
            continue
        fill = match_order(cid, info, bid, ask, bid_size, ask_size)
        if fill:
            fills.append(fill)
        else:
            info["rested"] = True
    return fills
