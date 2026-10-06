"""The ingested universe: which symbols and timeframes have a live feed (D4, F-H7).

Ingest is the single source of truth. It publishes what it streams (its ``INGEST_SYMBOLS``,
``INGEST_TIMEFRAMES`` and ``INGEST_PERP_SYMBOLS`` config, plus symbols onboarded at runtime) to Redis
under ``ingest:universe`` and refreshes it well inside the TTL. Mandate and strategy-instance creation
read it and refuse what has no feed; a missing or unreadable key means "unknown", and creation fails
closed rather than allowing anything.
"""

import datetime
import json
import re
from typing import Any

from pydantic import BaseModel, Field

from cryptonite_schemas.identifiers import DEFAULT_VENUE

INGEST_UNIVERSE_KEY = "ingest:universe"
UNIVERSE_TTL_S = 180  # ingest refreshes every UNIVERSE_REFRESH_S; a dead ingest drops the key within 3 min
UNIVERSE_REFRESH_S = 60

# Reason codes, named in the 422 / 503 details
SYMBOL_NOT_INGESTED = "SYMBOL_NOT_INGESTED"
TIMEFRAME_NOT_INGESTED = "TIMEFRAME_NOT_INGESTED"
PERP_NOT_INGESTED = "PERP_NOT_INGESTED"
VENUE_NOT_INGESTED = "VENUE_NOT_INGESTED"
UNIVERSE_UNKNOWN = "INGEST_UNIVERSE_UNKNOWN"


_SHORT_TF = re.compile(r"^(\d+)([smhd])$")
_AGG_UNIT = {"SECOND": "s", "MINUTE": "m", "HOUR": "h", "DAY": "d"}


def normalize_timeframe(timeframe: str) -> str | None:
    """'1m' / '1H' / '15-MINUTE-LAST-EXTERNAL' / 'BTCUSDT.BINANCE-1-HOUR-LAST-EXTERNAL' -> '1m' / '1h' / '15m' / '1h'.
    None when it is not a timeframe at all."""
    s = timeframe.strip()
    if m := _SHORT_TF.match(s.lower()):
        return f"{int(m.group(1))}{m.group(2)}"
    parts = s.upper().split("-")
    for i in range(len(parts) - 1):
        if parts[i].isdigit() and parts[i + 1] in _AGG_UNIT:
            return f"{int(parts[i])}{_AGG_UNIT[parts[i + 1]]}"
    return None


def _bar_type_instrument(timeframe: str) -> str | None:
    """The instrument a full bar type string names ('BTCUSDT.BINANCE-1-MINUTE-...' -> 'BTCUSDT.BINANCE')."""
    parts = timeframe.strip().upper().split("-")
    for i in range(1, len(parts) - 1):
        if parts[i].isdigit() and parts[i + 1] in _AGG_UNIT:
            return "-".join(parts[:i])
    return None


class IngestUniverse(BaseModel):
    """What ingest streams right now: spot bars for ``symbols`` x ``timeframes`` (with spot quotes) and
    perp quotes (no bars) for ``perp_symbols``, all on ``venue``."""

    v: int = 1
    venue: str = DEFAULT_VENUE
    symbols: list[str] = Field(default_factory=list)
    timeframes: list[str] = Field(default_factory=list)
    perp_symbols: list[str] = Field(default_factory=list)
    published_at: str | None = None

    @classmethod
    def build(cls, symbols: list[str], timeframes: list[str], perp_symbols: list[str] | None = None) -> "IngestUniverse":
        return cls(
            symbols=sorted({s.strip().upper() for s in symbols if s.strip()}),
            timeframes=sorted({tf for t in timeframes if (tf := normalize_timeframe(t))}),
            perp_symbols=sorted({s.strip().upper() for s in perp_symbols or [] if s.strip()}),
            published_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )

    @classmethod
    def parse(cls, raw: str | bytes | None) -> "IngestUniverse | None":
        """None when the key is missing or unreadable: the universe is unknown."""
        if not raw:
            return None
        try:
            return cls.model_validate(json.loads(raw))
        except (ValueError, TypeError):
            return None

    @property
    def instruments(self) -> list[str]:
        return [f"{s}.{self.venue}" for s in self.symbols] + [f"{s}-PERP.{self.venue}" for s in self.perp_symbols]

    def view(self) -> dict[str, Any]:
        return {**self.model_dump(), "instruments": self.instruments}

    def refusal(self, instrument_id: str, timeframe: str | None = None) -> str | None:
        """Why ``instrument_id`` (and ``timeframe``, when given) cannot be traded: a reason line that starts
        with a reason code, or None when ingest feeds it."""
        s = instrument_id.strip().upper()
        symbol, _, venue = s.partition(".")
        venue = venue or self.venue
        if venue != self.venue:
            return f"{VENUE_NOT_INGESTED}: ingest streams {self.venue} only, not {venue}"
        if symbol.endswith("-PERP"):
            base = symbol.removesuffix("-PERP")
            if base not in self.perp_symbols:
                return (f"{PERP_NOT_INGESTED}: ingest has no perp quote feed for {base} "
                        f"(perps fed: {', '.join(self.perp_symbols) or 'none'})")
            if timeframe is not None:  # perp feeds are quotes only: nothing for a bar-driven instance to run on
                return f"{PERP_NOT_INGESTED}: ingest streams perp quotes only, no {timeframe} bars for {symbol}"
        elif symbol not in self.symbols:
            return (f"{SYMBOL_NOT_INGESTED}: ingest has no feed for {symbol} "
                    f"(fed: {', '.join(self.symbols) or 'none'}); onboard it first")
        if timeframe is not None:
            tf = normalize_timeframe(timeframe)
            if tf is None:
                return f"{TIMEFRAME_NOT_INGESTED}: {timeframe!r} is not a timeframe"
            named = _bar_type_instrument(timeframe)
            if named and named != f"{symbol}.{venue}":  # a full bar type must be the instrument's own
                return f"{TIMEFRAME_NOT_INGESTED}: bar type {timeframe!r} is for {named}, not {symbol}.{venue}"
            if tf not in self.timeframes:
                return (f"{TIMEFRAME_NOT_INGESTED}: ingest has no {tf} bars "
                        f"(fed: {', '.join(self.timeframes) or 'none'})")
        return None
