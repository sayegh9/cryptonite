"""Live position marks and their staleness (review-plan B3, F-H3): the shared contract.

The orchestrator's mark-to-market projector values every open ``paper_positions`` row from its live
quote cache (spot: the bid, the price a long exits at; perps: the dispatcher's exchange mark) every
1-5 s. It writes ``current_price`` / ``unrealized_pnl`` to the row and the mark itself to the Redis hash
``MARKS_KEY`` (instrument -> JSON ``{"price", "quote_ts_ns", "marked_at_ns"}``).

Readers outside the orchestrator (the debate router's room, Mission Control, the paper positions API)
prefer that live mark and fall back to the stored ``current_price``, saying so: ``resolve_mark`` returns
the price with ``source`` ``live`` or ``stored``, a ``stale`` flag and the age in seconds. The age of a
stored mark is the time since the projector last confirmed it, or ``None`` when no projector ever has.

Pure data plus one read helper that takes the caller's Redis client (no import of a Redis library), so
the debate worker image, which carries only ``cryptonite_schemas``, can use it.
"""

import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger("cryptonite.marks")

MARKS_KEY = "paper:marks"
# A mark is live while its quote is at most this old. The projector only marks from quotes up to 10 s
# old (``MAX_MARK_QUOTE_AGE_S``) and runs every 1-5 s, so a running projector keeps marks under this.
MAX_MARK_QUOTE_AGE_S = 10.0
LIVE_MARK_MAX_AGE_S = 15.0


@dataclass(frozen=True)
class LiveMark:
    price: Decimal
    quote_ts_ns: int  # when the quote (or exchange mark) the price came from arrived
    marked_at_ns: int  # when the projector last wrote this price to paper_positions


@dataclass(frozen=True)
class MarkView:
    price: Decimal
    source: str  # live | stored
    stale: bool
    age_s: float | None  # live: quote age; stored: time since the projector last confirmed it (None: never)

    def fields(self) -> dict[str, Any]:
        """The staleness fields an API response carries next to the price."""
        return {"mark_source": self.source, "mark_stale": self.stale,
                "mark_age_s": None if self.age_s is None else round(self.age_s, 1)}


def _canonical(instrument_id: str) -> str:
    s = str(instrument_id).strip().upper()
    return s if "." in s else f"{s}.BINANCE"


def encode_mark(price: Decimal, quote_ts_ns: int, marked_at_ns: int) -> str:
    return json.dumps({"price": str(price), "quote_ts_ns": int(quote_ts_ns), "marked_at_ns": int(marked_at_ns)})


def parse_marks(raw: dict[Any, Any] | None) -> dict[str, LiveMark]:
    """``HGETALL MARKS_KEY`` (bytes or str) -> canonical instrument -> LiveMark; bad entries are skipped."""
    out: dict[str, LiveMark] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            key = k.decode() if isinstance(k, bytes) else str(k)
            data = json.loads(v.decode() if isinstance(v, bytes) else v)
            price = Decimal(str(data["price"]))
            if not price.is_finite() or price <= 0:
                continue
            quote_ts = int(data["quote_ts_ns"])
            marked_at = int(data["marked_at_ns"])
            if quote_ts <= 0 or marked_at <= 0:
                continue
            out[_canonical(key)] = LiveMark(price, quote_ts, marked_at)
        except (ValueError, KeyError, TypeError, InvalidOperation):
            continue
    return out


async def read_marks(redis: Any) -> dict[str, LiveMark]:
    """The projector's marks, or ``{}`` (everything then reads as stored) when Redis is missing or down."""
    if redis is None:
        return {}
    try:
        return parse_marks(await redis.hgetall(MARKS_KEY))
    except Exception as e:
        logger.warning("Could not read live marks: %s", e)
        return {}


def resolve_mark(instrument_id: str, stored_price: Any, marks: dict[str, LiveMark] | None,
                 now_ns: int | None = None) -> MarkView:
    """The live mark when its quote is fresh, else the stored price flagged stale."""
    now_ns = now_ns or time.time_ns()
    m = (marks or {}).get(_canonical(instrument_id))
    if m is not None and m.price.is_finite() and m.price > 0:
        raw_age_s = (now_ns - m.quote_ts_ns) / 1e9
        # Accept if not in the future (with 1.0s clock skew tolerance) and within freshness window
        if -1.0 <= raw_age_s <= LIVE_MARK_MAX_AGE_S:
            quote_age = max(0.0, raw_age_s)
            return MarkView(m.price, "live", False, quote_age)
    try:
        stored = Decimal(str(stored_price if stored_price is not None else 0))
        if not stored.is_finite() or stored < 0:
            stored = Decimal("0")
    except (InvalidOperation, TypeError, ValueError):
        stored = Decimal("0")
    age = max(0.0, (now_ns - m.marked_at_ns) / 1e9) if (m is not None and m.marked_at_ns > 0) else None
    return MarkView(stored, "stored", True, age)


def unrealized_pnl(side: str, quantity: Any, entry_price: Any, mark: Decimal) -> Decimal:
    direction = Decimal("-1") if str(side).upper() == "SHORT" else Decimal("1")
    return direction * Decimal(str(quantity)) * (mark - Decimal(str(entry_price)))


def summarize(views: list[MarkView]) -> dict[str, Any]:
    """Collection-level flags: stale when any mark is; the age is the oldest one (None when a stale mark
    was never confirmed, or there are no positions)."""
    if any(v.age_s is None for v in views) or not views:
        age = None
    else:
        age = round(max(v.age_s for v in views), 1)
    return {"marks_stale": any(v.stale for v in views), "marks_age_s": age}


def entry_room_pct(budget: Decimal, max_position_pct: Decimal, positions: list[Any], instrument_id: str,
                   marks: dict[str, LiveMark] | None, now_ns: int | None = None) -> Decimal:
    """Room for a new entry, as % of the mandate budget: under the instrument's position limit and under
    the budget, with the mandate's open positions (rows with ``instrument_id``, ``quantity``,
    ``current_price``) at their live mark, else their stored mark."""
    target = _canonical(instrument_id)
    gross = inst = Decimal("0")
    for r in positions:
        value = Decimal(str(r["quantity"])) * resolve_mark(r["instrument_id"], r["current_price"], marks, now_ns).price
        gross += value
        if _canonical(r["instrument_id"]) == target:
            inst += value
    room = min(budget * max_position_pct / 100 - inst, budget - gross)
    return max(Decimal("0"), room / budget * 100)
