"""Resolve and validate protective stops before an operator can approve an entry."""

from decimal import Decimal, InvalidOperation
from typing import Any

from cryptonite_execution.leverage import stop_distance
from cryptonite_execution.stop_ratchet import to_tick
from cryptonite_schemas.marks import read_live_quote, resolve_quote_price
from cryptonite_schemas.profile import Market, market_of, underlying_spot
from fastapi import HTTPException

from cryptonite_api.leverage_card import perp_mark


def _stop_error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"{code}: {message}")


async def fresh_reference_price(redis: Any, instrument_id: str) -> Decimal:
    """Return the current quote/mark price for an entry, refusing absent or stale data."""
    market = market_of(instrument_id)
    if market is Market.PERP:
        mark, _ = await perp_mark(redis, instrument_id)
        if mark is None or not mark.is_finite() or mark <= 0:
            raise _stop_error("DATA_STALE", "a fresh quote is required before approving this entry")
        return mark

    quote_instrument = underlying_spot(instrument_id) if market is Market.SPOT_MARGIN else instrument_id
    decision = resolve_quote_price(quote_instrument, await read_live_quote(redis, quote_instrument))
    if decision.price is None:
        raise _stop_error("DATA_STALE", "a fresh quote is required before approving this entry")
    return decision.price


async def resolve_entry_stop(
    pool: Any,
    redis: Any,
    instrument_id: str,
    side: str,
    *,
    explicit_stop: Any = None,
    stop_loss_pct: Any = None,
    order_reference: Any = None,
) -> Decimal:
    """Use an explicit structural stop or derive one from a percentage on a fresh quote.

    Percentage-derived stops are rounded away from the entry (down for a long, up for a short)
    on the instrument's price tick. ``stop_distance`` is the shared side check used by the
    leverage planner; reusing it keeps the approval path aligned with the execution plans.
    """
    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        raise _stop_error("STOP_REQUIRED", "the order side cannot be matched to a protective stop")
    if explicit_stop is None and stop_loss_pct is None:
        raise _stop_error("NO_STOP_AVAILABLE", "no stop available, cannot approve")

    reference = await fresh_reference_price(redis, instrument_id)
    stop: Decimal
    if explicit_stop is not None:
        try:
            stop = Decimal(str(explicit_stop))
        except (InvalidOperation, TypeError, ValueError):
            raise _stop_error("STOP_REQUIRED", "the structural stop is not a valid price")
    elif stop_loss_pct is not None:
        try:
            pct = Decimal(str(stop_loss_pct))
        except (InvalidOperation, TypeError, ValueError):
            raise _stop_error("STOP_REQUIRED", "the stop-loss percentage is not valid")
        if not pct.is_finite() or pct <= 0 or pct >= 100:
            raise _stop_error("STOP_REQUIRED", "the stop-loss percentage must be greater than 0 and below 100")

        tick_instrument = underlying_spot(instrument_id) if market_of(instrument_id) is not Market.SPOT else instrument_id
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT price_increment FROM instruments WHERE instrument_id = $1 AND is_active = true",
                tick_instrument,
            )
        if row is None:
            raise _stop_error("NO_STOP_AVAILABLE", "instrument tick size is unavailable; cannot approve")
        tick = Decimal(str(row["price_increment"]))
        if not tick.is_finite() or tick <= 0:
            raise _stop_error("NO_STOP_AVAILABLE", "instrument tick size is invalid; cannot approve")

        fraction = pct / Decimal("100")
        raw_stop = reference * (Decimal("1") - fraction if side == "BUY" else Decimal("1") + fraction)
        stop = to_tick(raw_stop, tick, long=side == "BUY")
    else:
        raise _stop_error("NO_STOP_AVAILABLE", "no stop available, cannot approve")

    if not stop.is_finite() or stop_distance(side, reference, stop) is None:
        raise _stop_error("STOP_REQUIRED", "the structural stop is not on the protective side of the fresh price")
    if order_reference is not None:
        try:
            order_price = Decimal(str(order_reference))
        except (InvalidOperation, TypeError, ValueError):
            raise _stop_error("STOP_REQUIRED", "the submitted limit price is not valid")
        if not order_price.is_finite() or order_price <= 0 or stop_distance(side, order_price, stop) is None:
            raise _stop_error("STOP_REQUIRED", "the structural stop is not on the protective side of the submitted limit")
    return stop


def entry_requires_stop(instrument_id: str, side: str, *, reduce_only: bool = False) -> bool:
    """Whether this side adds risk in its market; spot SELLs are exits, derivatives can short."""
    if reduce_only:
        return False
    market = market_of(instrument_id)
    return market is not Market.SPOT or str(side).upper() == "BUY"
