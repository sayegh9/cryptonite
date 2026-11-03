"""H7a/H9: when a stored scanner candidate is fresh: its bar closed at most about 3 bars of its timeframe ago, never
more than SCANNER_MAX_AGE_S, and it came from a live scan (``scan_runs.trigger_reason``). A manual Scanner Studio run
or a rescan after a lake repair stores old bars' signals with a new ``created_at``: those are history, never fresh.

One rule for the debate evidence, ``get_scanner_context``, the Brain's recent setups and the scanner's own store.
"""

import datetime
from typing import Any

SCANNER_MAX_AGE_S = 4 * 3600
SCANNER_FRESH_BARS = 3
LIVE_SCAN_TRIGGER = "COMMITTED_BAR_EVENT"
_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def scanner_max_age_s(timeframe: str) -> int:
    """How old a candidate's bar close may be: 3 bars of its timeframe, capped at 4 h."""
    tf_s = _TF_SECONDS.get(str(timeframe or "").lower())
    return SCANNER_MAX_AGE_S if tf_s is None else min(SCANNER_FRESH_BARS * tf_s, SCANNER_MAX_AGE_S)


def fresh_row_age_s(row: dict[str, Any], now_s: float) -> float | None:
    """The age in seconds of a stored candidate's bar close when the row is fresh, else None. ``row`` needs
    ``timeframe`` and ``bar_close_inclusive_ns``; ``trigger_reason`` and ``created_at`` are checked when present."""
    close_s = int(row["bar_close_inclusive_ns"]) / 1e9
    age_s = now_s - close_s
    max_age = scanner_max_age_s(row["timeframe"])
    trigger = row.get("trigger_reason")
    if trigger is not None and trigger != LIVE_SCAN_TRIGGER:
        return None
    created = row.get("created_at")
    if isinstance(created, datetime.datetime) and created.timestamp() - close_s > max_age:
        return None  # stored long after its bar closed: a backfill, not a live signal
    if age_s > max_age:
        return None
    return age_s
