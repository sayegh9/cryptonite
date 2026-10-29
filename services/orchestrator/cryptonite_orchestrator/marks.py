"""Mark-to-market for the paper book (review-plan B3, F-H3).

Runs ``CoalescedMarkToMarketProjector`` in the orchestrator, fed by the live quotes the orchestrator
already holds: the quote cache filled from ``cryptonite:stream:ui_quote_tick`` (Binance book tickers)
for spot, the dispatcher's exchange mark prices (``consume_perp_marks``) for perps. A spot long is
marked at the bid, the price it would exit at, as ``_portfolio_equity`` and the positions snapshot do.
Only fresh prices mark: a quote older than ``MAX_MARK_QUOTE_AGE_S`` (or a stale perp mark) leaves the
stored mark alone, and readers flag it stale (``cryptonite_schemas.marks``).

Display and sizing only: the enforced checks (mandate check, dispatcher) value positions from their
own live inputs and do not read these rows' marks first.
"""

import logging
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from cryptonite_execution.bridge import CoalescedMarkToMarketProjector
from cryptonite_schemas.marks import MARKS_KEY, MAX_MARK_QUOTE_AGE_S, encode_mark
from cryptonite_schemas.profile import Market, market_of, underlying_spot

logger = logging.getLogger("cryptonite.orchestrator.marks")

MIN_INTERVAL_S, MAX_INTERVAL_S, DEFAULT_INTERVAL_S = 1.0, 5.0, 2.0


def mark_interval_s(raw: str | None = None) -> float:
    """MARK_TO_MARKET_INTERVAL_S, clamped to 1-5 s (default 2 s)."""
    raw = os.getenv("MARK_TO_MARKET_INTERVAL_S") if raw is None else raw
    try:
        value = float(raw) if raw else DEFAULT_INTERVAL_S
    except ValueError:
        value = DEFAULT_INTERVAL_S
    return min(MAX_INTERVAL_S, max(MIN_INTERVAL_S, value))


def live_mark_price(engine, quote_cache, instrument_id: str, now_ns: int | None = None) -> tuple[Decimal, int] | None:
    """(price, quote_ts_ns) from a fresh live input, else None."""
    now_ns = now_ns or time.time_ns()
    d = engine.dispatcher
    if market_of(instrument_id) is Market.PERP:
        mark = d.mark_prices.get(instrument_id)
        if mark is None or d.is_mark_stale(instrument_id):
            return None
        try:
            px = Decimal(str(mark))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not px.is_finite() or px <= 0:
            return None
        ts = int(d.mark_ts_ns.get(instrument_id) or now_ns)
        if ts > now_ns + 1_000_000_000:
            return None
        return px, ts
    quote = quote_cache.quote(underlying_spot(instrument_id))
    if quote is None:
        return None
    try:
        ts = int(quote.ts_init)
    except (TypeError, ValueError):
        return None
    if ts > now_ns + 1_000_000_000 or now_ns - ts > MAX_MARK_QUOTE_AGE_S * 1e9:
        return None
    try:
        bid = Decimal(str(quote.bid_price))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not bid.is_finite() or bid <= 0:
        return None
    return (bid, ts)


async def publish_marks(redis, marked: dict[str, tuple[Decimal, int]], now_ns: int | None = None) -> None:
    """Write the pass's marks to the ``paper:marks`` hash read by the API and the debate router."""
    if not marked:
        return
    now_ns = now_ns or time.time_ns()
    try:
        await redis.hset(MARKS_KEY, mapping={inst: encode_mark(px, ts, now_ns) for inst, (px, ts) in marked.items()})
    except Exception as e:
        logger.warning("Could not publish live marks: %s", e)


async def start_mark_to_market(pool, redis, engine, quote_cache, interval_s: float | None = None) -> Any:
    projector = CoalescedMarkToMarketProjector(
        pool,
        lambda inst: live_mark_price(engine, quote_cache, inst),
        interval_s=interval_s or mark_interval_s(),
        on_marked=lambda marked: publish_marks(redis, marked),
    )
    await projector.start()
    logger.info("Mark-to-market projector started (every %.1f s)", projector.interval_s)
    return projector
