"""DuckDB analytical query engine over the date-partitioned Analytics Lake."""

import re
from pathlib import Path

import duckdb
import polars as pl

_UNIT_NS = {"m": 60 * 10**9, "h": 3600 * 10**9, "d": 86400 * 10**9, "w": 7 * 86400 * 10**9}
# A live kline arrives a few hundred ms before its rounded close; one recorded more than this before
# its close was still open when written.
EARLY_RECEIPT_TOLERANCE_NS = 5 * 10**9


def timeframe_ns(timeframe: str) -> int:
    """Duration of a Binance-style timeframe (``1m``, ``15m``, ``1h``, ``4h``, ``1d``, ``1w``) in ns.

    Binance's ``1M`` is a calendar month, not a fixed duration, and must not be lowercased into minutes."""
    tf = timeframe.strip()
    if tf.endswith("M"):
        raise ValueError(f"Unsupported timeframe: {timeframe!r} (calendar months have no fixed duration)")
    m = re.fullmatch(r"(\d+)([mhdw])", tf.lower())
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}")
    return int(m.group(1)) * _UNIT_NS[m.group(2)]


def open_stamped_receipt_lag_ns(tf_ns: int) -> int:
    """``received_at_ns - ts_event_ns`` of a row the API's REST path stamped at the kline OPEN: it stored
    ``ts_event_ns = open`` and ``received_at_ns = Binance close_time = open + duration - 1 ms`` (B1 / F-C1).
    No ingest-written row has this exact lag (ingest takes ``received_at_ns`` from the wall clock)."""
    return tf_ns - 1_000_000


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
        if symbol.upper().endswith(f".{venue.upper()}"):
            symbol = symbol.split(".")[0]
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
        """Query bars as a Polars DataFrame sorted by time, one row per bar.

        Lake rows are stamped at the bar CLOSE (``ts_event_ns = ts_init_ns = close``, see
        ``EnrichedBar.from_binance_raw``); callers that need the close (outcomes, reflection, betas) keep
        using ``ts_event_ns``. ``bar_open_ns = ts_event_ns - timeframe`` is the bar's OPEN, which is what
        the resampler windows on (B1 / F-C1: it used to take the close as the open, shifting every
        higher-timeframe bar by one minute).

        Where several rows share a ``ts_init_ns``, the pick is deterministic: a row recorded after its
        close wins over one recorded while still open, both win over a row the API's REST path stamped
        at the OPEN (its ``received_at_ns`` is exactly ``ts_event_ns + timeframe - 1 ms``; repaired by
        ``scripts/repair_lake_open_stamped_rows.py``), then the latest receipt, then batch and checksum.
        """
        tf_ns = timeframe_ns(timeframe)
        glob_path = self._get_glob_path(symbol, timeframe)
        where_clauses = []
        if start_ns is not None:
            where_clauses.append(f"ts_event_ns >= {start_ns}")
        if end_ns is not None:
            where_clauses.append(f"ts_event_ns <= {end_ns}")

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT DISTINCT ON (ts_init_ns) *, ts_event_ns::BIGINT - {tf_ns} AS bar_open_ns
            FROM read_parquet('{glob_path}')
            {where_sql}
            ORDER BY ts_init_ns ASC,
                CASE
                    WHEN received_at_ns::BIGINT - ts_event_ns::BIGINT = {open_stamped_receipt_lag_ns(tf_ns)} THEN 2
                    WHEN received_at_ns::BIGINT < ts_event_ns::BIGINT - {EARLY_RECEIPT_TOLERANCE_NS} THEN 1
                    ELSE 0
                END ASC,
                ts_event_ns DESC, received_at_ns DESC, ingest_batch_id DESC, payload_checksum DESC
        """
        # One cursor per call: DuckDB connections are not safe for concurrent use, and the API calls this from
        # worker threads (routes/bars.py).
        con = self._con.cursor()
        try:
            arrow_table = con.execute(query).to_arrow_table()
            return pl.from_arrow(arrow_table)  # type: ignore
        except duckdb.IOException:
            return pl.DataFrame()
        finally:
            con.close()

    def count_bars(self, symbol: str, timeframe: str = "1m") -> int:
        """Count total bars present in the Analytics Lake for a symbol/timeframe."""
        glob_path = self._get_glob_path(symbol, timeframe)
        query = f"SELECT count(*) FROM read_parquet('{glob_path}')"
        try:
            res = self._con.execute(query).fetchone()
            return res[0] if res else 0
        except duckdb.IOException:
            return 0


    def _get_derivatives_glob_path(self, symbol: str, timeframe: str = "1m") -> str:
        pattern = self.base_dir / "derivatives" / "binance" / "perpetual" / timeframe / symbol.upper() / "*.parquet"
        return str(pattern)

    def query_derivatives_df(
        self,
        symbol: str,
        timeframe: str = "1m",
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> pl.DataFrame:
        glob_path = self._get_derivatives_glob_path(symbol, timeframe)
        where_clauses = []
        if start_ns is not None:
            where_clauses.append(f"ts_event_ns >= {start_ns}")
        if end_ns is not None:
            where_clauses.append(f"ts_event_ns <= {end_ns}")

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT DISTINCT ON (ts_event_ns) * FROM read_parquet('{glob_path}')
            {where_sql}
            ORDER BY ts_event_ns DESC
        """
        try:
            arrow_table = self._con.execute(query).to_arrow_table()
            return pl.from_arrow(arrow_table)  # type: ignore
        except duckdb.IOException:
            return pl.DataFrame()

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
