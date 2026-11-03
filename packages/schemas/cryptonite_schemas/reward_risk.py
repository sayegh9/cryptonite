"""H9: the one reward-to-risk floor check every path uses (gate, Policy A, scanner, Brain scan and cards).

The scanner places targets at exactly 1.5R, but float arithmetic on prices gives 1.4999999999999818 for the 11-02
BTC short (entry 76779.73, stop 77578.61, target 75581.41): a strict ``rr < 1.5`` rejected every such setup.
Comparing at RR_DECIMALS decimal places corrects float error only; the floor itself stays 1.5.
"""

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

MIN_REWARD_TO_RISK = Decimal("1.5")
RR_DECIMALS = 6
_QUANTUM = Decimal(1).scaleb(-RR_DECIMALS)


def _dec(value: Any) -> Decimal | None:
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return d if d.is_finite() else None


def reward_to_risk(entry: Any, stop: Any, target: Any) -> Decimal | None:
    """(target - entry) / (entry - stop) in Decimal: same sign for longs and shorts; negative = wrong side.
    None when an input is not a number or the risk is zero."""
    e, s, t = _dec(entry), _dec(stop), _dec(target)
    if e is None or s is None or t is None or e == s:
        return None
    return (t - e) / (e - s)


def meets_min_rr(rr: Any, floor: Any = MIN_REWARD_TO_RISK) -> bool:
    """True when ``rr`` is at least ``floor``, compared at RR_DECIMALS places. A missing or invalid ratio fails."""
    r, f = _dec(rr), _dec(floor)
    if r is None or f is None:
        return False
    if r >= f:
        return True
    if r < f - 1:  # far below: nothing to round (and no quantize of a huge negative ratio)
        return False
    return r.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN) >= f.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
