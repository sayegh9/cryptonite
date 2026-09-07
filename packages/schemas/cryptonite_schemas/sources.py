"""Abstract data source interfaces for Cryptonite market data."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from cryptonite_schemas.bar import RawBinanceKline


class InstrumentSource(ABC):
    """Abstract interface for instrument definitions."""

    @abstractmethod
    async def fetch_instruments(self) -> list[dict[str, Any]]:
        """Fetch all available instrument definitions from exchange."""


class BarSource(ABC):
    """Abstract interface for historical and live bar data."""

    @abstractmethod
    async def fetch_historical_bars(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int | None = None
    ) -> list[RawBinanceKline]:
        """Fetch historical kline records."""

    @abstractmethod
    def subscribe_live_bars(
        self, symbols: list[str], timeframes: list[str]
    ) -> AsyncIterator[RawBinanceKline]:
        """Stream live kline updates and closed bars."""


class TradeSource(ABC):
    """Abstract interface for tick/trade stream (future milestone)."""

    @abstractmethod
    def subscribe_trades(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]:
        pass


class QuoteSource(ABC):
    """Abstract interface for top-of-book quotes (future milestone)."""

    @abstractmethod
    def subscribe_quotes(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]:
        pass


class OrderBookDeltaSource(ABC):
    """Abstract interface for L2/L3 order book deltas (future milestone)."""

    @abstractmethod
    def subscribe_order_book_deltas(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]:
        pass
