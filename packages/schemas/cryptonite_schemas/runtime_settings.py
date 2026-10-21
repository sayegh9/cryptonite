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
    # Leverage (ADR 0009, leverage plan §2.1, §2.3): each setup grade's leverage ceiling, at most the 10x account
    # maximum (lower is tighter), and the slippage allowance in % (higher is tighter: the stop is priced further away)
    "leverage.grade_ceiling_a": Bound(10, 1, 10),
    "leverage.grade_ceiling_b": Bound(5, 1, 10),
    "leverage.grade_ceiling_c": Bound(2, 1, 10),
    "leverage.slippage_allowance_pct": Bound(0.3, 0.05, 2.0),
    # Auto-trade Phase B (ADR 0010 amendment): scanner debates routed to one perp mandate per UTC day (lower is
    # tighter; 0 stops them; 24 is one per closed 1h bar)
    "debate.perp_mandate_daily_cap": Bound(6, 0, 24),
}
PERP_DEBATE_CAP_KEY = "debate.perp_mandate_daily_cap"
GRADE_CEILING_KEYS = {"A": "leverage.grade_ceiling_a", "B": "leverage.grade_ceiling_b", "C": "leverage.grade_ceiling_c"}
SLIPPAGE_KEY = "leverage.slippage_allowance_pct"
LEVERAGE_KEYS = (*GRADE_CEILING_KEYS.values(), SLIPPAGE_KEY)


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


@dataclass(frozen=True)
class LeverageSettings:
    grade_ceilings: dict[str, int]  # A/B/C -> leverage; NO_TRADE is always 0
    slippage_pct: float  # in %, e.g. 0.3


def default_leverage_settings() -> LeverageSettings:
    return LeverageSettings({g: int(RUNTIME_BOUNDS[k].default) for g, k in GRADE_CEILING_KEYS.items()} | {"NO_TRADE": 0},
                            RUNTIME_BOUNDS[SLIPPAGE_KEY].default)


def read_leverage_settings(stored: dict[str, Any], last_good: LeverageSettings) -> tuple[LeverageSettings, list[str]]:
    """The grade ceilings and slippage allowance to apply (same rule as every runtime value), and the problems."""
    problems: list[str] = []
    ceilings: dict[str, int] = {"NO_TRADE": 0}
    for grade, key in GRADE_CEILING_KEYS.items():
        value, problem = read_number(stored.get(key), RUNTIME_BOUNDS[key], last_good.grade_ceilings.get(grade, 0))
        if problem:
            problems.append(f"{key}: {problem}")
        ceilings[grade] = int(value)
    slip, problem = read_number(stored.get(SLIPPAGE_KEY), RUNTIME_BOUNDS[SLIPPAGE_KEY], last_good.slippage_pct)
    if problem:
        problems.append(f"{SLIPPAGE_KEY}: {problem}")
    return LeverageSettings(ceilings, slip), problems
