"""Settings the services read every cycle (ADR 0007). Pure: a service reads the stored value from ``system_settings``
and passes it here, so the parsing and the fall-back rule are the same in every service and in the API.

Rule: a missing value takes the default; a valid stored value is applied; an invalid one (does not parse, or is
outside its bounds) keeps the last valid value and is reported as a problem.
"""

import json
import math
from dataclasses import dataclass
from typing import Any


# registry / system_settings key -> bounds and the code default (the env default, where a service has one)
@dataclass(frozen=True)
class Bound:
    default: float
    low: float
    high: float


RUNTIME_BOUNDS: dict[str, Bound] = {
    "alerts.heartbeat_grace_s": Bound(300, 60, 3600),
    "alerts.sentiment_stale_s": Bound(86400, 3600, 604800),
    "scanner.max_active_specs": Bound(10, 1, 50),
    "exit.monitor_interval_s": Bound(30, 5, 300),
    # Operator session and login policy (ADR 0007, Phase 2). The ceilings are the current defaults, so a session
    # can only be made shorter from the default; the lockout window is the one whose higher value is the tighter.
    "auth.session_ttl_s": Bound(43200, 900, 43200),
    "auth.idle_timeout_s": Bound(1800, 300, 1800),
    "auth.lockout_s": Bound(300, 60, 3600),
    "auth.max_login_attempts": Bound(5, 3, 10),
}


def _parse(raw: Any) -> float:
    """A stored value: a JSON string (as the API writes it), a bare number or text."""
    if isinstance(raw, str) and raw.strip().startswith('"'):
        raw = json.loads(raw)
    return float(str(raw).strip())


def read_number(raw: Any, bound: Bound, last_good: float) -> tuple[float, str | None]:
    """The value to apply for a stored ``raw``, and a problem to report when the stored value was refused."""
    if raw is None:
        return bound.default, None
    try:
        value = _parse(raw)
    except (TypeError, ValueError):
        return last_good, f"stored value {raw!r} is not a number"
    if not math.isfinite(value) or not bound.low <= value <= bound.high:
        return last_good, f"stored value {raw!r} is outside {bound.low:g}..{bound.high:g}"
    return value, None
