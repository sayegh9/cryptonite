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
# A debate decision price is only valid from a quote at most this old (review-plan B5, F-M14): the
# worker debates on the same live source as the orchestrator and declares NO_PRICE without any model
# or order side effect when the quote is absent or older than this.
DECISION_PRICE_MAX_AGE_S = 10.0


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


@dataclass(frozen=True)
class DecisionPrice:
    """A debate decision price resolved from the per-instrument quote snapshot (B5).

    ``price`` is the finite positive quote for exactly this instrument, or None with
    ``source`` ``"NO_PRICE"`` when the quote is absent, stale, invalid, or for another symbol.
    ``quote_ts_ns``/``age_s`` declare what was used; None when there was nothing usable.
    """

    price: Decimal | None
    source: str  # quote | NO_PRICE
    quote_ts_ns: int | None
    age_s: float | None


# All-market latest quotes (review-plan B5, F-M14): ``paper:marks`` only covers instruments with
# open positions, and a shared stream is retention-bounded but not age-bounded — under a busy
# multi-symbol feed an instrument's still-fresh tick can be evicted before it ages out. So the ingest
# writes a per-instrument snapshot beside the stream, in the same quote pipeline, from the same tick:
# key ``quote:tick:<canonical instrument>`` holding the ui_record JSON
# ``{instrument_id, bid_price, ask_price, bid_size, ask_size, arrival_ns}``. The TTL only bounds
# garbage for delisted symbols; freshness is always decided by the timestamp check below. Readers do
# one constant-time GET: presence never depends on other symbols' volume.
QUOTE_TICK_KEY_PREFIX = "quote:tick:"
QUOTE_TICK_TTL_S = 60


def quote_tick_key(instrument_id: str) -> str:
    return f"{QUOTE_TICK_KEY_PREFIX}{_canonical(instrument_id)}"


def _field(fields: dict, key: str) -> Any:
    if key in fields:
        return fields[key]
    return fields.get(key.encode())


def parse_quote_record(data: Any) -> tuple[str, Decimal, Decimal, int] | None:
    """A snapshot value -> (canonical instrument, bid, ask, arrival_ns); None when unusable.

    Accepts the ui_record JSON (str or bytes, as with or without decode_responses) or a bare dict.
    """
    if isinstance(data, dict):
        record = data
    else:
        try:
            record = json.loads(data.decode() if isinstance(data, bytes) else data)
        except (ValueError, TypeError, AttributeError):
            return None
        if not isinstance(record, dict):
            return None
    try:
        inst = _field(record, "instrument_id")
        inst = inst.decode() if isinstance(inst, bytes) else str(inst)
        bid = Decimal(str(_field(record, "bid_price")))
        ask = Decimal(str(_field(record, "ask_price")))
        arrival_ns = int(_field(record, "arrival_ns"))  # type: ignore[arg-type]
    except (TypeError, ValueError, InvalidOperation, AttributeError):
        return None
    if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask <= 0 or arrival_ns <= 0:
        return None
    return _canonical(inst), bid, ask, arrival_ns


async def read_live_quote(redis: Any, instrument_id: str) -> tuple[str, Decimal, Decimal, int] | None:
    """This instrument's latest snapshot tick, or None when absent/unusable (never raises)."""
    if redis is None:
        return None
    try:
        raw = await redis.get(quote_tick_key(instrument_id))
    except Exception as e:
        logger.warning("Could not read live quote: %s", e)
        return None
    if raw is None:
        return None
    tick = parse_quote_record(raw)
    if tick is not None and tick[0] != _canonical(instrument_id):
        logger.warning("Quote snapshot for %s holds another instrument (%s): ignoring",
                       instrument_id, tick[0])
        return None
    return tick


def resolve_quote_price(instrument_id: str, tick: tuple[str, Decimal, Decimal, int] | None,
                        now_ns: int | None = None) -> DecisionPrice:
    """The tick's mid price when it is this instrument's fresh arrival, else NO_PRICE (never a guess)."""
    now_ns = now_ns or time.time_ns()
    if tick is not None and tick[0] == _canonical(instrument_id):
        _, bid, ask, arrival_ns = tick
        raw_age_s = (now_ns - arrival_ns) / 1e9
        if -1.0 <= raw_age_s <= DECISION_PRICE_MAX_AGE_S:
            return DecisionPrice((bid + ask) / 2, "quote", arrival_ns, max(0.0, raw_age_s))
        return DecisionPrice(None, "NO_PRICE", arrival_ns, max(0.0, raw_age_s))
    quote_ts = tick[3] if tick is not None else None
    age = max(0.0, (now_ns - quote_ts) / 1e9) if quote_ts else None
    return DecisionPrice(None, "NO_PRICE", quote_ts, age)


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
