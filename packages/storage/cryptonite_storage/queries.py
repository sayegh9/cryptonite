"""DuckDB analytical query engine over the date-partitioned Analytics Lake."""

from pathlib import Path

import duckdb
import polars as pl


class AnalyticsQueryEngine:
    """Fast analytical queries over date-partitioned Parquet data using DuckDB."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir).resolve()
        self._con = duckdb.connect(":memory:")

    def _get_glob_path(
        self,
        symbol: str,
        timeframe: str = "1m",
        venue: str = "binance",
        market_type: str = "spot",
    ) -> str:
        pattern = (
            self.base_dir
            / "binance_klines_enriched"
            / venue.lower()
            / market_type.lower()
            / timeframe
            / symbol.upper()
            / "date=*"
            / "*.parquet"
        )
        return str(pattern)

    def query_bars_df(
        self,
        symbol: str,
        timeframe: str = "1m",
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> pl.DataFrame:
        """Query bars as a sorted Polars DataFrame with converted float indicators if needed."""
        glob_path = self._get_glob_path(symbol, timeframe)
        where_clauses = []
        if start_ns is not None:
            where_clauses.append(f"ts_event_ns >= {start_ns}")
        if end_ns is not None:
            where_clauses.append(f"ts_event_ns <= {end_ns}")

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT * FROM read_parquet('{glob_path}')
            {where_sql}
            ORDER BY ts_event_ns ASC
        """
        try:
            arrow_table = self._con.execute(query).to_arrow_table()
            return pl.from_arrow(arrow_table)  # type: ignore
        except duckdb.IOException:
            return pl.DataFrame()

    def count_bars(self, symbol: str, timeframe: str = "1m") -> int:
        """Count total bars present in the Analytics Lake for a symbol/timeframe."""
        glob_path = self._get_glob_path(symbol, timeframe)
        query = f"SELECT count(*) FROM read_parquet('{glob_path}')"
        try:
            res = self._con.execute(query).fetchone()
            return res[0] if res else 0
        except duckdb.IOException:
            return 0

    def get_timestamp_range(
        self, symbol: str, timeframe: str = "1m"
    ) -> tuple[int | None, int | None]:
        """Get minimum and maximum ts_event_ns in the Lake."""
        glob_path = self._get_glob_path(symbol, timeframe)
        query = f"SELECT min(ts_event_ns), max(ts_event_ns) FROM read_parquet('{glob_path}')"
        try:
            res = self._con.execute(query).fetchone()
            if res and res[0] is not None:
                return int(res[0]), int(res[1])
            return None, None
        except duckdb.IOException:
            return None, None

    def detect_gaps(
        self, symbol: str, timeframe: str = "1m", step_seconds: int = 60
    ) -> list[tuple[int, int]]:
        """Identify missing gaps in the sequence of bars."""
        glob_path = self._get_glob_path(symbol, timeframe)
        step_ns = step_seconds * 1_000_000_000
        query = f"""
            WITH ranked AS (
                SELECT ts_event_ns,
                       LAG(ts_event_ns) OVER (ORDER BY ts_event_ns) AS prev_ts
                FROM read_parquet('{glob_path}')
            )
            SELECT prev_ts, ts_event_ns
            FROM ranked
            WHERE prev_ts IS NOT NULL AND ts_event_ns - prev_ts > {step_ns}
            ORDER BY prev_ts ASC
        """
        try:
            rows = self._con.execute(query).fetchall()
            return [(r[0], r[1]) for r in rows]
        except duckdb.IOException:
            return []
