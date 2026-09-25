"""Debate verdict contracts.

Models produce these as validated structured output (the gateway re-asks on invalid output and
fails closed if it never validates), so no free-text JSON parsing or HOLD-on-failure defaults.
"""

from typing import Literal

from pydantic import BaseModel, Field

MAX_ALLOCATION_PCT = 25.0  # mirrors the dispatcher's 25% equity clamp; the dispatcher still enforces it


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
