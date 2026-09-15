"""Read-only data frame provider interface for the scanner.

Enforces structural isolation: scanner engine depends strictly on this protocol
and cannot access Nautilus execution or live exchange trade APIs.
"""

from typing import Protocol

import polars as pl


class BarFrameProvider(Protocol):
    """Protocol for accessing historical bar DataFrames for scanning."""

    def get_bars_df(
        self,
        symbol: str,
        timeframe: str = "1m",
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> pl.DataFrame:
        """Fetch historical bars as a sorted Polars DataFrame."""
        ...
