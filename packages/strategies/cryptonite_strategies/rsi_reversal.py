"""RSI Oversold Reversal Strategy."""

from typing import Any
from nautilus_trader.indicators import RelativeStrengthIndex
from nautilus_trader.model import Bar
from cryptonite_strategies.base import CryptoniteStrategy, CryptoniteStrategyConfig


class RSIOversoldReversalConfig(CryptoniteStrategyConfig):
    """Configuration for RSI Oversold Reversal Strategy."""

    def __init__(
        self,
        instrument_id: str,
        bar_type: str,
        rsi_period: int = 14,
        oversold_threshold: float = 30.0,
        exit_threshold: float = 50.0,
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
        self.rsi_period = int(rsi_period)
        self.oversold_threshold = float(oversold_threshold)
        self.exit_threshold = float(exit_threshold)


class RSIOversoldReversalStrategy(CryptoniteStrategy):
    """Enters long on oversold recovery (RSI crosses above oversold_threshold), exits above exit_threshold."""

    def __init__(self, config: RSIOversoldReversalConfig) -> None:
        super().__init__(config)
        self.rsi_period = config.rsi_period
        self.oversold_threshold = config.oversold_threshold
        self.exit_threshold = config.exit_threshold
        self.rsi = RelativeStrengthIndex(self.rsi_period)
        self._prev_rsi: float | None = None

    def on_start(self) -> None:
        super().on_start()
        self.register_indicator_for_bars(self.bar_type, self.rsi)

    def on_bar(self, bar: Bar) -> None:
        if not self.rsi.initialized:
            return

        # Nautilus RelativeStrengthIndex is normalized in [0.0, 1.0]; scale to [0.0, 100.0] if threshold > 1.0
        curr_rsi = self.rsi.value * 100.0 if (self.oversold_threshold > 1.0 and self.rsi.value <= 1.0) else self.rsi.value

        if self._prev_rsi is not None:
            # Bullish reversal: crossed above oversold threshold
            if self._prev_rsi <= self.oversold_threshold and curr_rsi > self.oversold_threshold:

                if not self.is_in_warmup(bar):
                    if self.is_flat():
                        self.buy_market()
            # Exit long if RSI recovers above exit_threshold
            elif curr_rsi >= self.exit_threshold:
                if not self.is_in_warmup(bar):
                    if self.is_long():
                        self.close_all()

        self._prev_rsi = curr_rsi
