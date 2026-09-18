from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class TimeframeState(BaseModel):
    """OHLCV and indicator state for a specific timeframe."""
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    
    # EMAs
    ema_7: Optional[float] = None
    ema_25: Optional[float] = None
    ema_99: Optional[float] = None
    
    # Bollinger Bands
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    
    # Momentum
    rsi: Optional[float] = None
    stoch_rsi_k: Optional[float] = None
    stoch_rsi_d: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    
    # Volume Filter
    volume_ma5: Optional[float] = None
    volume_ma10: Optional[float] = None
    
    # ATR for stop sizing
    atr: Optional[float] = None


class StrategistConfig(BaseModel):
    """Dynamic thresholds for the strategist engine."""
    stop_hunt_wick_ratio: float = Field(default=0.6, description="Minimum ratio of wick to total candle range")
    stop_hunt_volume_mult: float = Field(default=2.0, description="Volume multiplier vs MA10")
    rsi_momentum_long_threshold: float = Field(default=50.0, description="RSI must be above this for longs")
    rsi_momentum_short_threshold: float = Field(default=50.0, description="RSI must be below this for shorts")


class MultiTimeframeSnapshot(BaseModel):
    """Full state snapshot across multiple timeframes."""
    instrument_id: str
    evaluated_at: datetime
    timeframes: dict[str, TimeframeState]
    config: StrategistConfig = Field(default_factory=StrategistConfig)


class WebhookAlert(BaseModel):
    """Actionable alert definition."""
    alert_trigger: str
    action: str
    price: Optional[Decimal] = None


class StrategistSignal(BaseModel):
    """Output actionable signal from the Strategist Engine."""
    id: UUID
    instrument_id: str
    evaluated_at: datetime
    market_state: str
    macro_floor: Decimal
    macro_ceiling: Decimal
    bias: Literal["Long", "Short", "NO_TRADE"]
    rationale: list[str]
    
    # Trade Plan
    entry_zone_low: Optional[Decimal] = None
    entry_zone_high: Optional[Decimal] = None
    hard_stop: Optional[Decimal] = None
    tp1: Optional[Decimal] = None
    tp2: Optional[Decimal] = None
    rr_ratio: Optional[float] = None
    
    webhook_alerts: list[WebhookAlert] = Field(default_factory=list)
    chop_reason: Optional[str] = None
