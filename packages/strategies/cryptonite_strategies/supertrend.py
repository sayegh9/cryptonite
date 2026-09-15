"""Supertrend Breakout Strategy."""

from typing import Any
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model import Bar
from cryptonite_strategies.base import CryptoniteStrategy, CryptoniteStrategyConfig


class SupertrendBreakoutConfig(CryptoniteStrategyConfig):
    """Configuration for Supertrend Breakout Strategy."""

    def __init__(
        self,
        instrument_id: str,
        bar_type: str,
        atr_period: int = 10,
        multiplier: float = 3.0,
        trade_size: float = 0.01,
        evaluation_start_ns: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_size=trade_size,
            evaluation_start_ns=evaluation_start_ns,
            **kwargs,
        )
        self.atr_period = int(atr_period)
        self.multiplier = float(multiplier)


class SupertrendBreakoutStrategy(CryptoniteStrategy):
    """Calculates canonical Supertrend bands and enters long on bullish flip, exits on bearish flip."""

    def __init__(self, config: SupertrendBreakoutConfig) -> None:
        super().__init__(config)
        self.atr_period = config.atr_period
        self.multiplier = config.multiplier
        self.atr = AverageTrueRange(self.atr_period)

        self._prev_close: float | None = None
        self._final_upper_band: float | None = None
        self._final_lower_band: float | None = None
        self._is_bullish: bool = True

    def on_start(self) -> None:
        super().on_start()
        self.register_indicator_for_bars(self.bar_type, self.atr)

    def on_bar(self, bar: Bar) -> None:
        if not self.atr.initialized:
            self._prev_close = bar.close.as_double()
            return

        c = bar.close.as_double()
        h = bar.high.as_double()
        l = bar.low.as_double()
        atr_val = self.atr.value

        hl2 = (h + l) / 2.0
        basic_upper = hl2 + (self.multiplier * atr_val)
        basic_lower = hl2 - (self.multiplier * atr_val)

        # Initialize bands
        if self._final_upper_band is None:
            self._final_upper_band = basic_upper
            self._final_lower_band = basic_lower
            self._prev_close = c
            return

        prev_upper = self._final_upper_band
        prev_lower = self._final_lower_band
        prev_close = self._prev_close if self._prev_close is not None else c

        # Final bands calculation
        final_upper = basic_upper if (basic_upper < prev_upper or prev_close > prev_upper) else prev_upper
        final_lower = basic_lower if (basic_lower > prev_lower or prev_close < prev_lower) else prev_lower

        prev_trend = self._is_bullish
        if prev_trend and c < final_lower:
            curr_trend = False
        elif not prev_trend and c > final_upper:
            curr_trend = True
        else:
            curr_trend = prev_trend

        # Signal execution
        if not prev_trend and curr_trend:
            # Bullish flip
            if not self.is_in_warmup(bar):
                if self.is_flat():
                    self.buy_market()
        elif prev_trend and not curr_trend:
            # Bearish flip
            if not self.is_in_warmup(bar):
                if self.is_long():
                    self.close_all()

        self._final_upper_band = final_upper
        self._final_lower_band = final_lower
        self._prev_close = c
        self._is_bullish = curr_trend
