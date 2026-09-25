"""Strategy research contracts (AI-first blueprint Phase 4)."""

from typing import Literal

from pydantic import BaseModel, Field


class ParamSpec(BaseModel):
    """One tunable parameter of a drafted strategy; the optimizer searches [low, high]."""

    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    kind: Literal["int", "float"]
    low: float
    high: float


class StrategyDraft(BaseModel):
    """What the drafter returns: code plus the search space for its numeric parameters."""

    name: str = Field(max_length=80)
    description: str = Field(max_length=600)
    python_code: str
    parameter_space: list[ParamSpec] = Field(default_factory=list, max_length=6)


class StrategyReview(BaseModel):
    """Pro review of a candidate that already passed validation, backtests and the overfitting guard."""

    verdict: Literal["PROMOTE", "REVISE", "REJECT"]
    concerns: list[str] = Field(default_factory=list, max_length=8)
    summary: str = Field(max_length=1200)
