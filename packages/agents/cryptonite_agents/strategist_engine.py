import logging
from decimal import Decimal
from typing import Literal, Tuple
from uuid import uuid4

from cryptonite_schemas.strategist import (
    MultiTimeframeSnapshot,
    StrategistSignal,
    TimeframeState,
    WebhookAlert,
)

logger = logging.getLogger(__name__)


class StrategistEngine:
    """
    Deterministic rule-engine for the Execution Strategist Agent.
    Implements the 5-rule hierarchical analytical protocol.
    """

    def analyze(self, snapshot: MultiTimeframeSnapshot) -> StrategistSignal:
        tf_1d = snapshot.timeframes.get("1D")
        tf_4h = snapshot.timeframes.get("4H")
        tf_1h = snapshot.timeframes.get("1H")
        tf_15m = snapshot.timeframes.get("15m")
        tf_5m = snapshot.timeframes.get("5m")

        if not all([tf_1d, tf_4h, tf_1h, tf_15m, tf_5m]):
            return self._build_chop(snapshot, "Missing required timeframes for full hierarchy.")

        # 1. Macro Context (Floor & Ceiling)
        floor, ceiling = self._macro_levels(tf_1d, tf_4h, current_price=tf_5m.close)

        # 2. Volume Filter (Bull Trap / Stop Hunt)
        vol_flag, vol_rationale = self._volume_filter(tf_5m, tf_15m, snapshot.config)
        if vol_flag != "CLEAN":
            # If we hit a volume trap, we fade it (counter-trend)
            bias = "Short" if vol_flag == "BULL_TRAP" else "Long"
            return self._build_signal(
                snapshot=snapshot,
                bias=bias,
                rationale=[vol_rationale],
                floor=floor,
                ceiling=ceiling,
                tf_5m=tf_5m,
            )

        # 3. Momentum & Squeeze
        momentum = self._momentum_state(tf_1h, tf_15m, snapshot.config)

        # 4. Trigger
        trigger, trigger_rationale = self._trigger_check(tf_5m)

        if momentum == "LONG" and trigger == "LONG":
            rationale = [trigger_rationale, "Momentum layer aligned LONG (1H/15m)."]
            return self._build_signal(snapshot, "Long", rationale, floor, ceiling, tf_5m)
        elif momentum == "SHORT" and trigger == "SHORT":
            rationale = [trigger_rationale, "Momentum layer aligned SHORT (1H/15m)."]
            return self._build_signal(snapshot, "Short", rationale, floor, ceiling, tf_5m)

        # 5. No clear advantage -> CHOP
        return self._build_chop(snapshot, f"No advantage. Momentum={momentum}, Trigger={trigger}.")

    def _macro_levels(
        self, tf_1d: TimeframeState, tf_4h: TimeframeState, current_price: float
    ) -> Tuple[Decimal, Decimal]:
        # Simple macro derivation:
        # Floor is min(1D low, 4H low) that is below current price (if any, else just 4H low)
        # Ceiling is max(1D high, 4H high) that is above current price (if any, else just 4H high)
        floor = min(tf_1d.low, tf_4h.low)
        ceiling = max(tf_1d.high, tf_4h.high)
        
        # Fallbacks to ensure floor <= price <= ceiling logically
        if floor >= current_price:
            floor = current_price * 0.95
        if ceiling <= current_price:
            ceiling = current_price * 1.05
            
        return Decimal(str(round(floor, 4))), Decimal(str(round(ceiling, 4)))

    def _volume_filter(
        self, tf_5m: TimeframeState, tf_15m: TimeframeState, config
    ) -> Tuple[str, str]:
        # Bull Trap: price pushing bb_upper on declining volume
        if (
            tf_15m.bb_upper and tf_15m.close > tf_15m.bb_upper
            and tf_15m.volume_ma5 and tf_15m.volume_ma10
            and tf_15m.volume < tf_15m.volume_ma5
            and tf_15m.volume < tf_15m.volume_ma10
        ):
            return "BULL_TRAP", "Bull Trap: Pushing overhead resistance (BB upper) on declining volume."

        # Stop Hunt: Massive volume spike with long wick
        candle_range = tf_5m.high - tf_5m.low
        if candle_range > 0 and tf_5m.volume_ma10:
            real_body_top = max(tf_5m.open, tf_5m.close)
            real_body_bottom = min(tf_5m.open, tf_5m.close)
            upper_wick = tf_5m.high - real_body_top
            lower_wick = real_body_bottom - tf_5m.low
            
            upper_ratio = upper_wick / candle_range
            lower_ratio = lower_wick / candle_range
            
            vol_spike = tf_5m.volume > (tf_5m.volume_ma10 * config.stop_hunt_volume_mult)
            
            if vol_spike and upper_ratio > config.stop_hunt_wick_ratio:
                return "STOP_HUNT_HIGH", "Stop Hunt (High): Massive volume spike absorbed into long upper wick."
            if vol_spike and lower_ratio > config.stop_hunt_wick_ratio:
                return "STOP_HUNT_LOW", "Stop Hunt (Low): Massive volume spike absorbed into long lower wick."
                
        return "CLEAN", ""

    def _momentum_state(self, tf_1h: TimeframeState, tf_15m: TimeframeState, config) -> str:
        # Check alignment of StochRSI and RSI across 1H and 15m
        def is_long(tf: TimeframeState):
            return (
                tf.stoch_rsi_k and tf.stoch_rsi_d and tf.stoch_rsi_k > tf.stoch_rsi_d
                and tf.rsi and tf.rsi > config.rsi_momentum_long_threshold
                and tf.macd_hist and tf.macd_hist > 0
            )
            
        def is_short(tf: TimeframeState):
            return (
                tf.stoch_rsi_k and tf.stoch_rsi_d and tf.stoch_rsi_k < tf.stoch_rsi_d
                and tf.rsi and tf.rsi < config.rsi_momentum_short_threshold
                and tf.macd_hist and tf.macd_hist < 0
            )
            
        if is_long(tf_1h) and is_long(tf_15m):
            return "LONG"
        if is_short(tf_1h) and is_short(tf_15m):
            return "SHORT"
        return "MIXED"

    def _trigger_check(self, tf_5m: TimeframeState) -> Tuple[str, str]:
        # 5m trigger logic (EMA tangles / crosses)
        if tf_5m.ema_7 and tf_5m.ema_25:
            # Simplistic proxy for "crosses above" based on state snapshot
            # (In reality, we'd look at prev bar, but we use strict positioning here)
            if tf_5m.close > tf_5m.ema_7 and tf_5m.ema_7 > tf_5m.ema_25:
                return "LONG", "5m Trigger: Price and fast EMA cleared EMA-25."
            if tf_5m.close < tf_5m.ema_7 and tf_5m.ema_7 < tf_5m.ema_25:
                return "SHORT", "5m Trigger: Price and fast EMA broke below EMA-25."
                
        # Alternative: BB pinch breakout
        if tf_5m.bb_upper and tf_5m.bb_lower and tf_5m.close:
            if tf_5m.close > tf_5m.bb_upper:
                return "LONG", "5m Trigger: Price broke upper Bollinger Band."
            if tf_5m.close < tf_5m.bb_lower:
                return "SHORT", "5m Trigger: Price broke lower Bollinger Band."

        return "NONE", ""

    def _build_signal(
        self,
        snapshot: MultiTimeframeSnapshot,
        bias: Literal["Long", "Short"],
        rationale: list[str],
        floor: Decimal,
        ceiling: Decimal,
        tf_5m: TimeframeState,
    ) -> StrategistSignal:
        price = Decimal(str(round(tf_5m.close, 4)))
        atr = Decimal(str(round(tf_5m.atr or (tf_5m.close * 0.005), 4)))
        
        if bias == "Long":
            entry_low = Decimal(str(round(tf_5m.ema_25 or tf_5m.close * 0.99, 4)))
            entry_high = price
            hard_stop = floor - (atr * Decimal("0.5"))
            tp2 = ceiling
            tp1 = (price + ceiling) / Decimal("2.0")
        else:
            entry_low = price
            entry_high = Decimal(str(round(tf_5m.ema_25 or tf_5m.close * 1.01, 4)))
            hard_stop = ceiling + (atr * Decimal("0.5"))
            tp2 = floor
            tp1 = (price + floor) / Decimal("2.0")
            
        # Ensure logical values
        hard_stop = round(hard_stop, 4)
        tp1 = round(tp1, 4)
        tp2 = round(tp2, 4)
        
        risk = abs(price - hard_stop)
        reward = abs(tp1 - price)
        rr_ratio = float(reward / risk) if risk > 0 else 0.0

        webhooks = [
            WebhookAlert(alert_trigger=f"price_crosses_{hard_stop}", action="close_position", price=hard_stop),
            WebhookAlert(alert_trigger=f"price_crosses_{tp1}", action="take_profit_50pct", price=tp1),
        ]

        return StrategistSignal(
            id=uuid4(),
            instrument_id=snapshot.instrument_id,
            evaluated_at=snapshot.evaluated_at,
            market_state=f"{bias} setup identified.",
            macro_floor=floor,
            macro_ceiling=ceiling,
            bias=bias,
            rationale=rationale,
            entry_zone_low=entry_low,
            entry_zone_high=entry_high,
            hard_stop=hard_stop,
            tp1=tp1,
            tp2=tp2,
            rr_ratio=round(rr_ratio, 2),
            webhook_alerts=webhooks,
        )

    def _build_chop(self, snapshot: MultiTimeframeSnapshot, reason: str) -> StrategistSignal:
        tf_1d = snapshot.timeframes.get("1D")
        tf_4h = snapshot.timeframes.get("4H")
        price = snapshot.timeframes.get("5m").close if snapshot.timeframes.get("5m") else 0.0
        
        floor = min(tf_1d.low if tf_1d else price, tf_4h.low if tf_4h else price)
        ceiling = max(tf_1d.high if tf_1d else price, tf_4h.high if tf_4h else price)
        
        return StrategistSignal(
            id=uuid4(),
            instrument_id=snapshot.instrument_id,
            evaluated_at=snapshot.evaluated_at,
            market_state="Low-volume chop box / No clear setup.",
            macro_floor=Decimal(str(round(floor, 4))),
            macro_ceiling=Decimal(str(round(ceiling, 4))),
            bias="NO_TRADE",
            rationale=[],
            chop_reason=reason,
            webhook_alerts=[],
        )
