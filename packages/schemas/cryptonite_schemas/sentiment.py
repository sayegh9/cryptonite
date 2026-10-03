"""Sentiment tag contract (blueprint §4.7, A6.3): one label per headline or post."""

from typing import Literal

from pydantic import BaseModel, Field


class SentimentTag(BaseModel):
    label: Literal["bullish", "neutral", "bearish"]
    confidence: float = Field(ge=0.0, le=1.0)
