"""Entry sizing by setup grade, shared by debate routing and Brain order cards (H2, O2)."""

import math

GRADE_SIZE_FACTOR = {"A": 1.0, "B": 1.0, "C": 0.5}
UNKNOWN_GRADE_FACTOR = 0.5  # C, NO_TRADE and an unknown grade size at half


def grade_size_pct(grade: str | None, max_position_pct: float, room: float) -> float:
    """The entry size, in percent of the mandate: the profile's max position scaled by the setup grade (A or B full, C
    half, an unknown grade half), never more than the room left. The model's allocation does not size an entry."""
    return round(min(float(max_position_pct) * GRADE_SIZE_FACTOR.get(str(grade), UNKNOWN_GRADE_FACTOR), float(room)), 2)


# --- Entry feasibility (H3): can a profile's budget place an order at all? ---------------------------------------
MIN_ORDER_EQUITY_PCT = 1.0  # the risk engine's fixed floor for a margin or perp order, percent of account equity
ROUND_TRIP_COST_PCT = 0.45  # fees and slippage folded into the typical stop when sizing by risk
_TYPICAL_STOP_PCT = {"15m": 2.0, "1h": 3.5}
_DEFAULT_STOP_PCT = 3.5


def typical_stop_for(timeframes: list[str] | None) -> float:
    """The typical stop distance, percent of price, for the profile's smallest bar timeframe (2% on 15m, 3.5% else)."""
    return 2.0 if timeframes and all(t in ("5m", "15m") for t in timeframes) else _DEFAULT_STOP_PCT


def entry_feasibility(
    budget_usd: float,
    max_position_pct: float,
    max_risk_pct: float,
    max_leverage: int,
    markets: list[str],
    equity_usd: float | None,
    typical_stop_pct: float = _DEFAULT_STOP_PCT,
) -> dict[str, dict]:
    """Per market (spot, margin, perp), whether the profile can place an entry above the risk engine's minimum order.

    ``max_notional`` is the budget times the position limit (times leverage for a perp); ``risk_notional`` is the order a
    typical stop sizes to at the risk limit; ``min_notional`` is 1% of equity for margin and perp, 0 for spot. Codes:
    OK, ORDER_MIN_UNREACHABLE (even the position limit is below the minimum), ORDER_MIN_LIKELY_REFUSED (the
    risk-sized order is). ``suggested_budget_usd`` is the smallest budget (rounded up to $100) that clears both."""
    budget, pos, risk = float(budget_usd), float(max_position_pct) / 100, float(max_risk_pct) / 100
    cost_stop = (float(typical_stop_pct) + ROUND_TRIP_COST_PCT) / 100
    held = {"margin" if str(m).lower() == "spot_margin" else str(m).lower() for m in markets}
    out: dict[str, dict] = {}
    for market in ("spot", "margin", "perp"):
        if market not in held:
            continue
        leverage = max(1, int(max_leverage)) if market == "perp" else 1
        max_notional = budget * pos * leverage
        risk_notional = budget * risk / cost_stop
        min_notional = (MIN_ORDER_EQUITY_PCT / 100 * float(equity_usd)) if market != "spot" and equity_usd else 0.0
        code = "OK"
        if max_notional < min_notional:
            code = "ORDER_MIN_UNREACHABLE"
        elif risk_notional < min_notional:
            code = "ORDER_MIN_LIKELY_REFUSED"
        needed = max(min_notional / (pos * leverage), min_notional * cost_stop / risk) if pos > 0 and risk > 0 else None
        suggested = math.ceil(needed / 100) * 100 if code != "OK" and needed is not None else None
        out[market] = {
            "max_notional": round(max_notional, 2), "risk_notional": round(risk_notional, 2),
            "min_notional": round(min_notional, 2), "code": code, "suggested_budget_usd": suggested,
        }
    return out
