"""Debate verdict contracts.

Models produce these as validated structured output (the gateway re-asks on invalid output and
fails closed if it never validates), so no free-text JSON parsing or HOLD-on-failure defaults.
"""

from typing import Literal

from pydantic import BaseModel, Field

MAX_ALLOCATION_PCT = 25.0  # mirrors the dispatcher's 25% equity clamp; the dispatcher still enforces it

Direction = Literal["BUY", "SELL", "HOLD"]

# One verdict vocabulary (review-plan B2, F-C2, F-M6): the PM / lite verdicts, the CRO's conviction
# level and the routing words, upper-cased, to the direction they mean. Migration 037's ``VERDICTS``
# backfilled ``portfolio_decisions.recommendation`` with this same mapping; keep the two in step.
VERDICT_DIRECTIONS: dict[str, Direction] = {
    "BUY": "BUY", "OVERWEIGHT": "BUY", "STRONG_BUY": "BUY", "LONG": "BUY",
    "SELL": "SELL", "UNDERWEIGHT": "SELL", "STRONG_SELL": "SELL", "SHORT": "SELL",
    "HOLD": "HOLD",
}
# The surrounding characters a verdict may carry; migration 037 trims the same set
# (``btrim(recommendation, E' \t\r\n')``), so the backfill and this function agree.
VERDICT_WHITESPACE = " \t\r\n"


def normalize_verdict(value: str) -> Direction:
    """A verdict in any case, surrounding spaces / tabs / line breaks ignored, as BUY / SELL / HOLD
    (Buy / Overweight / STRONG_BUY / LONG -> BUY, Sell / Underweight / STRONG_SELL / SHORT -> SELL,
    Hold -> HOLD). Anything else raises ``ValueError``: an unknown verdict is never guessed."""
    direction = VERDICT_DIRECTIONS.get(value.strip(VERDICT_WHITESPACE).upper()) if isinstance(value, str) else None
    if direction is None:
        raise ValueError(f"unknown verdict {value!r}; expected one of {sorted(VERDICT_DIRECTIONS)}")
    return direction


def verdict_direction(value: object) -> Direction | None:
    """``normalize_verdict`` for callers that treat an unknown verdict as "no direction" (no order
    side, nothing to score) instead of an error."""
    try:
        return normalize_verdict(value)  # type: ignore[arg-type]
    except ValueError:
        return None


class PortfolioDecision(BaseModel):
    """Research Manager (PM proxy) verdict after the bull/bear debate."""

    recommendation: Literal["BUY", "HOLD", "SELL"]
    conviction_score: float = Field(ge=0.0, le=1.0, description="0 = no conviction, 1 = certain")
    allocation_pct: float = Field(ge=0.0, le=MAX_ALLOCATION_PCT, description="Percent of equity, 0-25")
    reasoning: str


class RiskDecision(BaseModel):
    """Chief Risk Officer verdict after the risk panel: confirms or overrides the PM sizing."""

    final_allocation_pct: float = Field(ge=0.0, le=MAX_ALLOCATION_PCT, description="Percent of equity, 0-25")
    conviction_level: Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
    risk_reasoning: str


class ExitDecision(BaseModel):
    """Exit Manager verdict for an open position."""

    recommendation: Literal["REDUCE", "HOLD", "CLOSE"]
    conviction_score: float = Field(ge=0.0, le=1.0)
    target_stop_price: float | None = Field(default=None, gt=0)
    reasoning: str


class TriageDecision(BaseModel):
    """Triage verdict: is a scanner candidate worth a full (paid) debate?"""

    worth_debate: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=400)


class LiteDebateDecision(BaseModel):
    """One-call debate for low-stakes candidates: both sides argued, then a portfolio decision."""

    bull_case: str = Field(max_length=1200)
    bear_case: str = Field(max_length=1200)
    recommendation: Literal["BUY", "HOLD", "SELL"]
    conviction_score: float = Field(ge=0.0, le=1.0)
    allocation_pct: float = Field(ge=0.0, le=MAX_ALLOCATION_PCT, description="Percent of equity, 0-25")
    reasoning: str
