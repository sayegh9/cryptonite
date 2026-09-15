"""EMA Crossover Strategy."""

from typing import Any
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model import Bar
from cryptonite_strategies.base import CryptoniteStrategy, CryptoniteStrategyConfig


class EMACrossoverConfig(CryptoniteStrategyConfig):
    """Configuration for EMA Crossover Strategy."""

    def __init__(
        self,
        instrument_id: str,
        bar_type: str,
        fast_period: int = 9,
        slow_period: int = 21,
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
        self.fast_period = int(fast_period)
        self.slow_period = int(slow_period)
        if self.fast_period >= self.slow_period:
            raise ValueError(
                f"fast_period ({self.fast_period}) must be less than slow_period ({self.slow_period})"
            )


class EMACrossoverStrategy(CryptoniteStrategy):
    """Executes long trades when fast EMA crosses above slow EMA; closes when crossing below."""

    def __init__(self, config: EMACrossoverConfig) -> None:
        super().__init__(config)
        self.fast_period = config.fast_period
        self.slow_period = config.slow_period
        self.fast_ema = ExponentialMovingAverage(self.fast_period)
        self.slow_ema = ExponentialMovingAverage(self.slow_period)
        self._prev_fast: float | None = None
        self._prev_slow: float | None = None

    def on_start(self) -> None:
        super().on_start()
        self.register_indicator_for_bars(self.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.bar_type, self.slow_ema)

    def on_bar(self, bar: Bar) -> None:
        if not self.fast_ema.initialized or not self.slow_ema.initialized:
            return

        curr_fast = self.fast_ema.value
        curr_slow = self.slow_ema.value

        if self._prev_fast is not None and self._prev_slow is not None:
            # Bullish cross: fast crossed above slow
            if self._prev_fast <= self._prev_slow and curr_fast > curr_slow:
                if not self.is_in_warmup(bar):
                    if self.is_flat():
                        self.buy_market()
                    elif self.is_short():
                        self.close_all()
                        self.buy_market()
            # Bearish cross: fast crossed below slow
            elif self._prev_fast >= self._prev_slow and curr_fast < curr_slow:
                if not self.is_in_warmup(bar):
                    if self.is_long():
                        self.close_all()

        self._prev_fast = curr_fast
        self._prev_slow = curr_slow
