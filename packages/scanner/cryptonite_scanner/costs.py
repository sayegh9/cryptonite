"""Cost model v1 (Phase 2 plan, operator decision O1): round-trip trading costs expressed in R.

Pure functions shared by the candidate labeller, the Setups inbox and ``scripts/policy_sim.py``.
Costs are fractions of notional; R is the entry-to-stop distance, so ``cost_r = cost_fraction * entry / risk``.
"""

from dataclasses import dataclass

COST_MODEL_VERSION = "v1"

# Funding when no stored rate is known: 0.01% per 8 h, charged against the side.
DEFAULT_FUNDING_PER_8H = 0.0001


@dataclass(frozen=True, slots=True)
class MarketCosts:
    fee_per_side: float
    slippage_per_side: float


COSTS: dict[str, MarketCosts] = {
    "spot": MarketCosts(fee_per_side=0.0010, slippage_per_side=0.0005),
    "perp": MarketCosts(fee_per_side=0.0005, slippage_per_side=0.0005),
    "margin": MarketCosts(fee_per_side=0.0010, slippage_per_side=0.0005),
}


def default_market(direction: str | None) -> str:
    """Spot cannot short: longs are costed as spot, shorts as perp."""
    return "perp" if (direction or "").upper() in ("SHORT", "SELL") else "spot"


def cost_model_name(market: str) -> str:
    return f"{market}-{COST_MODEL_VERSION}"


def round_trip_cost_fraction(
    market: str,
    hold_hours: float = 0.0,
    *,
    funding_rate_per_8h: float | None = None,
    borrow_rate_per_hour: float = 0.0,
) -> float:
    """Fees and slippage on both sides plus carry for the hold, as a fraction of entry notional.

    ``funding_rate_per_8h`` is the cost to the position's side (positive = paid); ``None`` uses the default.
    """
    c = COSTS[market]
    cost = 2 * (c.fee_per_side + c.slippage_per_side)
    hours = max(hold_hours, 0.0)
    if market == "perp":
        rate = DEFAULT_FUNDING_PER_8H if funding_rate_per_8h is None else funding_rate_per_8h
        cost += rate * hours / 8.0
    elif market == "margin":
        cost += borrow_rate_per_hour * hours
    return cost


def cost_in_r(entry: float, stop: float, cost_fraction: float) -> float | None:
    """Convert a cost fraction of notional to R through the entry-to-stop distance; None when risk is zero."""
    risk = abs(entry - stop)
    if entry <= 0 or risk <= 0:
        return None
    return cost_fraction * entry / risk
