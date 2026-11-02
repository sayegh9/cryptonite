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


class RiskVerdict(BaseModel):
    """The verdict stored after the CRO, once ``clamp_risk_verdict`` has applied the PM's limits."""

    recommendation: Direction
    conviction_level: str
    allocation_pct: float
    conviction_score: float
    clamp_note: str | None = None


def clamp_risk_verdict(pm_recommendation: str, pm_conviction: float, pm_allocation: float,
                       cro_level: str, cro_allocation: float) -> RiskVerdict:
    """H7a: the CRO can only keep the PM's direction at the same or a smaller allocation, or turn it into HOLD.

    A PM HOLD stays HOLD at 0% (a CRO "Buy" on it is clamped); a CRO direction opposite to the PM's becomes HOLD;
    an allocation above the PM's is cut to the PM's. ``clamp_note`` says what was clamped (None when nothing was).
    The stored conviction is the PM's only while the stored direction is the PM's: a HOLD the PM did not choose
    stores 0.0, so one agent's confidence is never paired with another agent's verdict. Unknown wording raises."""
    pm_dir = normalize_verdict(pm_recommendation)
    cro_dir = normalize_verdict(cro_level)
    pm_alloc = max(0.0, float(pm_allocation or 0.0))
    alloc = max(0.0, float(cro_allocation or 0.0))
    note = None
    if pm_dir == "HOLD" and cro_dir != "HOLD":
        note = f"CRO {cro_level} {alloc:g}% on a PM HOLD clamped to HOLD 0%"
        cro_dir, level, alloc = "HOLD", "Hold", 0.0
    elif cro_dir != "HOLD" and cro_dir != pm_dir:
        note = f"CRO {cro_level} {alloc:g}% reverses the PM's {pm_dir}: clamped to HOLD 0%"
        cro_dir, level, alloc = "HOLD", "Hold", 0.0
    else:
        level = cro_level
        if cro_dir == "HOLD":
            alloc = 0.0
        elif alloc > pm_alloc:
            note = f"CRO allocation {alloc:g}% above the PM's {pm_alloc:g}%: clamped to {pm_alloc:g}%"
            alloc = pm_alloc
    conviction = float(pm_conviction or 0.0) if cro_dir == pm_dir else 0.0
    return RiskVerdict(recommendation=cro_dir, conviction_level=level, allocation_pct=alloc,
                       conviction_score=conviction, clamp_note=note)


class PortfolioDecision(BaseModel):
    """Research Manager (PM proxy) verdict after the bull/bear debate."""

    recommendation: Literal["BUY", "HOLD", "SELL"]
    conviction_score: float = Field(ge=0.0, le=1.0, description="0 = no conviction, 1 = certain")
    allocation_pct: float = Field(ge=0.0, le=MAX_ALLOCATION_PCT, description="Percent of equity, 0-25")
    reasoning: str
    preferred_market: Literal["SPOT", "SPOT_MARGIN", "PERP"] | None = None


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
    preferred_market: Literal["SPOT", "SPOT_MARGIN", "PERP"] | None = None
