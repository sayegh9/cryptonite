"""Base Strategy and StrategyConfig classes for Cryptonite backtesting."""

from typing import Any
from decimal import Decimal

from nautilus_trader.model import Bar, BarType, InstrumentId, OrderSide, Quantity
from nautilus_trader.trading import Strategy, StrategyConfig


class CryptoniteStrategyConfig(StrategyConfig):
    """Base configuration model for all Cryptonite trading strategies."""

    def __init__(
        self,
        instrument_id: str,
        bar_type: str,
        trade_size: float = 0.01,
        evaluation_start_ns: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.instrument_id = instrument_id
        self.bar_type = bar_type
        self.trade_size = float(trade_size)
        self.evaluation_start_ns = int(evaluation_start_ns)


class CryptoniteStrategy(Strategy):
    """Base strategy providing canonical event routing and order lifecycle helpers."""

    EXECUTION_TIMING_DISCLOSURE: str = (
        "Under zero latency, market orders submitted in on_bar execute against the "
        "simulated book state at bar close, not at the open of the subsequent bar."
    )

    def __init__(self, config: CryptoniteStrategyConfig) -> None:
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.trade_size = config.trade_size
        self.evaluation_start_ns = int(getattr(config, "evaluation_start_ns", 0))
        self._instrument = None

    def on_start(self) -> None:
        """Cache the instrument and subscribe to the configured bar type."""
        self._instrument = self.cache.instrument(self.instrument_id)
        self.subscribe_bars(self.bar_type)
        self.log.info(
            f"Strategy {self.strategy_id} started. Subscribed to {self.bar_type}. "
            f"Evaluation start: {self.evaluation_start_ns}ns. "
            f"Disclosure: {self.EXECUTION_TIMING_DISCLOSURE}"
        )

    def is_in_warmup(self, bar: Bar | None = None) -> bool:
        """Check if the strategy is currently within the indicator warmup window."""
        if self.evaluation_start_ns <= 0:
            return False
        if bar is not None:
            return bar.ts_event < self.evaluation_start_ns
        try:
            return self.clock.timestamp_ns() < self.evaluation_start_ns
        except (RuntimeError, AttributeError):
            return True


    def get_trade_qty(self) -> Quantity:
        """Resolve the configured trade size against the instrument size precision."""
        if self._instrument is not None:
            return self._instrument.make_qty(self.trade_size)
        return Quantity.from_str(str(self.trade_size))

    def submit_order(self, order: Any) -> None:
        """Submit an order only if warmup period has elapsed."""
        if self.is_in_warmup():
            try:
                now_str = f"at {self.clock.timestamp_ns()}ns "
            except (RuntimeError, AttributeError):
                now_str = ""
            self.log.info(
                f"Warm-up active: suppressing order {getattr(order, 'client_order_id', '')} "
                f"{now_str}(evaluation_start_ns={self.evaluation_start_ns}ns)"
            )
            return
        super().submit_order(order)

    def submit_order_list(self, order_list: Any) -> None:
        """Submit an order list only if warmup period has elapsed."""
        if self.is_in_warmup():
            try:
                now_str = f"at {self.clock.timestamp_ns()}ns "
            except (RuntimeError, AttributeError):
                now_str = ""
            self.log.info(
                f"Warm-up active: suppressing order list {now_str}"
                f"(evaluation_start_ns={self.evaluation_start_ns}ns)"
            )
            return
        super().submit_order_list(order_list)


    def buy_market(self, quantity: Quantity | None = None) -> None:
        """Submit a market BUY order."""
        if self.is_in_warmup():
            return
        qty = quantity or self.get_trade_qty()
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.BUY,
            quantity=qty,
        )
        self.submit_order(order)

    def sell_market(self, quantity: Quantity | None = None) -> None:
        """Submit a market SELL order."""
        if self.is_in_warmup():
            return
        qty = quantity or self.get_trade_qty()
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.SELL,
            quantity=qty,
        )
        self.submit_order(order)

    def close_all(self) -> None:
        """Close any existing open position for the instrument."""
        if self.is_in_warmup():
            return
        self.close_all_positions(self.instrument_id)

    def close_all_positions(self, instrument_id: InstrumentId) -> None:
        """Close positions only if not in warmup."""
        if self.is_in_warmup():
            return
        super().close_all_positions(instrument_id)

    def is_flat(self) -> bool:
        """Return True if position is net flat."""
        try:
            return self.portfolio.is_net_flat(self.instrument_id)
        except RuntimeError:
            return True

    def is_long(self) -> bool:
        """Return True if position is net long."""
        try:
            return self.portfolio.is_net_long(self.instrument_id)
        except RuntimeError:
            return False

    def is_short(self) -> bool:
        """Return True if position is net short."""
        try:
            return self.portfolio.is_net_short(self.instrument_id)
        except RuntimeError:
            return False

