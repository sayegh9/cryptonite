"""H15 A3: the conditional watchlist, computed on read from the preset rules.

A preset gives an item when its setup is one trigger away on the newest closed bar: every trend condition holds and
exactly one trigger condition is unmet (for a cross, still on the side it would cross from). An item carries no levels,
because they are only computed when the rule fires, and no order action. When a monitored rule fires, the scanner
creates a candidate through its normal path; nothing converts an item. No model produces any part of it.
"""

from datetime import UTC, datetime
from typing import Any

import polars as pl

from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.models import RuleCondition, RuleGroup, RuleOperator

EXPIRY_BARS = 4
MAX_PER_COIN_SIDE = 3
MAX_TOTAL = 10

# Closed bars an indicator needs before it means anything. The EMAs return a value on any window, so short history
# would otherwise give a wrong reading, not an error.
_WARMUP = (("ema_200", 200), ("ema_50", 50), ("ema_20", 20), ("macd", 35), ("bb_", 20), ("donchian", 21),
           ("volume_ratio", 20), ("rsi", 15), ("atr", 15))
_ENGINE_FLOOR = 15  # ``evaluate`` drops rows without RSI and ATR


def _bars_for(field: Any) -> int:
    if not isinstance(field, str):
        return 1
    name = field.removeprefix("htf_")
    return next((bars for prefix, bars in _WARMUP if name.startswith(prefix)), 1)


def required_bars(rules: list[RuleGroup]) -> tuple[int, int]:
    """(closed bars on the preset's own timeframe, closed bars on its parent; 0: no parent field is read)."""
    own, parent = _ENGINE_FLOOR, 0
    for group in rules:
        for cond in group.conditions:
            for field in (cond.field, cond.value):
                if isinstance(field, str) and field.startswith("htf_"):
                    parent = max(parent, _bars_for(field))
                else:
                    own = max(own, _bars_for(field) + max(cond.lookback, 0))
    return own, parent


def _is_cross(cond: RuleCondition) -> tuple[bool, bool]:
    return cond.operator == RuleOperator.CROSS_ABOVE, cond.operator == RuleOperator.CROSS_BELOW


def _values(cond: RuleCondition, row: dict[str, Any]) -> tuple[float | None, float | None]:
    right = row.get(cond.value) if isinstance(cond.value, str) else cond.value
    left = row.get(cond.field)
    return (None if left is None else float(left)), (None if right is None else float(right))


def _armed(cond: RuleCondition, row: dict[str, Any]) -> bool:
    """A cross can still happen only from the side it crosses from."""
    above, below = _is_cross(cond)
    if not (above or below):
        return True
    left, right = _values(cond, row)
    if left is None or right is None:
        return False
    return left <= right if above else left >= right


def _describe(cond: RuleCondition, row: dict[str, Any]) -> dict[str, Any]:
    left, right = _values(cond, row)
    return {"field": cond.field, "operator": str(getattr(cond.operator, "value", cond.operator)), "compared_to": cond.value,
            "field_value": left, "compared_value": right}


def conditional_item(frame: pl.DataFrame, rules: list[RuleGroup]) -> dict[str, Any] | None:
    """The trigger, ``also_required`` and ``invalidation`` for a frame whose last row is the newest closed bar (with its
    ``htf_*`` columns), or None. Uses the engine's own condition expressions, so it reads a rule as ``evaluate`` does."""
    if frame.height < 2:
        return None
    groups = {g.category: g for g in rules if g.conditions}
    trend, trigger = groups.get("trend"), groups.get("trigger")
    if trend is None or trigger is None or trend.logic.upper() != "AND" or trigger.logic.upper() != "AND":
        return None

    def met(cond: RuleCondition) -> bool:
        return bool(frame.select(ScannerEngine._build_condition_expr(cond).alias("m")).tail(1).item())

    row = frame.tail(1).to_dicts()[0]
    try:
        if not all(met(c) for c in trend.conditions):
            return None
        unmet = [c for c in trigger.conditions if not met(c)]
    except pl.exceptions.PolarsError:  # a column this frame lacks: no reading, so no item
        return None
    if len(unmet) != 1 or not _armed(unmet[0], row):
        return None
    also = [_describe(c, row) for g in rules if g.category not in ("trend", "trigger") for c in g.conditions]
    return {"trigger": _describe(unmet[0], row), "also_required": also,
            "invalidation": [_describe(c, row) for c in trend.conditions]}


def expires_at(bar_close_ns: int, bar_ns: int) -> str:
    return datetime.fromtimestamp((bar_close_ns + EXPIRY_BARS * bar_ns) / 1e9, UTC).isoformat()


def capped(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """At most 3 per coin and side and 10 in all, in a stable order (timeframe, then slug)."""
    order = {"15m": 0, "1h": 1, "4h": 2}
    seen: dict[tuple[str, str], int] = {}
    out: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda i: (order.get(i["timeframe"], 9), i["preset_slug"], i["instrument_id"])):
        key = (item["instrument_id"], item["direction"])
        if seen.get(key, 0) < MAX_PER_COIN_SIDE and len(out) < MAX_TOTAL:
            seen[key] = seen.get(key, 0) + 1
            out.append(item)
    return out
