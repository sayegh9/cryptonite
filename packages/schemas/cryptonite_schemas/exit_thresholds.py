"""Exit thresholds (ADR 0007): the five numbers the exit monitor applies, their bounds and the rules that tie them
together. Pure: the scanner service reads a stored set with ``settled``, and the API checks a write with
``violations``, so both apply the same rules.

For every key a lower value makes exits fire sooner (tighter), so lowering is free and raising needs the
operator's TOTP, as for the LLM budget.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass

# registry / system_settings key -> field of ExitThresholds
SETTING_KEYS: dict[str, str] = {
    "exit.stop_loss_pct": "stop_loss_pct",
    "exit.trail_activate_pct": "trail_activate_pct",
    "exit.trail_pct": "trail_pct",
    "exit.debate_drawdown_pct": "debate_drawdown_pct",
    "exit.funding_spike": "funding_spike",
}
KEY_OF_FIELD: dict[str, str] = {field: key for key, field in SETTING_KEYS.items()}

# (minimum, maximum) as strings, the same text the registry shows. Percentages are of the position.
BOUNDS: dict[str, tuple[str, str]] = {
    "stop_loss_pct": ("0.01", "20"),
    "trail_activate_pct": ("0.01", "20"),
    "trail_pct": ("0.01", "20"),
    "debate_drawdown_pct": ("0.01", "20"),
    "funding_spike": ("0.0001", "0.01"),
}
DECIMALS: dict[str, int] = {"funding_spike": 4}  # every other key takes two decimal places


@dataclass(frozen=True)
class ExitThresholds:
    stop_loss_pct: float  # hard stop: loss from entry, percent
    trail_activate_pct: float  # trailing stop arms once the gain reaches this, percent
    trail_pct: float  # give-back from the best price that closes a trailed position, percent
    debate_drawdown_pct: float  # first loss crossing this starts an exit debate, percent
    funding_spike: float  # funding per interval against the position that starts an exit debate


def violations(t: ExitThresholds) -> list[str]:
    """Every rule the set breaks, in the setting keys the operator sees. Empty when the set may apply."""
    found: list[str] = []
    for field, (low, high) in BOUNDS.items():
        value = getattr(t, field)
        if not float(low) <= value <= float(high):  # NaN fails both comparisons
            found.append(f"{KEY_OF_FIELD[field]} must be between {low} and {high}")
    if t.trail_pct > t.trail_activate_pct:
        found.append(f"{KEY_OF_FIELD['trail_pct']} must not exceed {KEY_OF_FIELD['trail_activate_pct']}")
    if t.debate_drawdown_pct >= t.stop_loss_pct:
        found.append(f"{KEY_OF_FIELD['debate_drawdown_pct']} must be below {KEY_OF_FIELD['stop_loss_pct']}")
    return found


def _number(raw: object) -> float:
    """A stored value: a JSON string ("3.00", as the API writes it), a bare number or text."""
    if isinstance(raw, str) and raw.strip().startswith('"'):
        raw = json.loads(raw)
    value = float(str(raw).strip())
    if not math.isfinite(value):
        raise ValueError("not finite")
    return value


def settled(stored: Mapping[str, object], defaults: ExitThresholds,
            last_good: ExitThresholds) -> tuple[ExitThresholds, list[str]]:
    """The set a stored state means. A missing key takes its default. A stored value that does not parse, or a set
    that breaks a rule, is refused as a whole: the last valid set stays in force and the problems are returned."""
    values = {field: getattr(defaults, field) for field in SETTING_KEYS.values()}
    problems: list[str] = []
    for key, field in SETTING_KEYS.items():
        raw = stored.get(key)
        if raw is None:
            continue
        try:
            values[field] = _number(raw)
        except (TypeError, ValueError):
            problems.append(f"{key}: stored value {raw!r} is not a number")
    if problems:
        return last_good, problems
    candidate = ExitThresholds(**values)
    found = violations(candidate)
    if found:
        return last_good, found
    return candidate, []
