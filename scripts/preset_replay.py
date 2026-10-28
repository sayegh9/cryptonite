"""Read-only spot replay of scanner presets against the analytics lake (ADR 0011, P1.9).

The output is one row per preset × requested coin. DuckDB streams Parquet rows from a read-only
catalog in bounded batches; this program never writes to the analytics lake.

Run inside the scanner image with the live lake mounted read-only (after building the scanner image):

    mkdir -p replay && docker run --rm --memory 2g \
      -v /home/samx/Documents/projects/cryptonite/deploy-main/data/analytics_lake:/lake:ro \
      -v "$PWD/scripts/preset_replay.py:/app/preset_replay.py:ro" \
      -v "$PWD/replay:/out" \
      "$(docker compose -f infra/docker-compose.yml images -q scanner)" \
      python /app/preset_replay.py --lake /lake --coins BTCUSDT,ETHUSDT \
      --since 2026-04-15 --out /out/preset-replay.json
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
from cryptonite_scanner.preset_stats import (
    replay_batches,
    report_document,
    summarize_replay,
)
from cryptonite_scanner.presets import _PROFILE_PRESETS

DUCKDB_MEMORY_LIMIT = (
    "700MB"  # both DuckDB connections plus Polars/Python must fit under the 2 GB container cap
)
DUCKDB_BATCH_ROWS = 2048
INDICATOR_CONTEXT_BARS = 300  # matches the scanner's EMA-200 plus warmup window
HTF_MAP = {"15m": "1h", "1h": "4h", "4h": "1d"}
EARLY_RECEIPT_TOLERANCE_NS = 5_000_000_000
OPEN_STAMP_RANK_LAG_NS = 1_000_000


def _parser() -> argparse.ArgumentParser:
    """Build the command line interface for the read-only replay."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake", required=True, type=Path, help="Analytics lake root")
    parser.add_argument(
        "--coins", required=True, help="Comma-separated spot symbols, e.g. BTCUSDT,ETHUSDT"
    )
    parser.add_argument(
        "--since",
        required=True,
        type=date.fromisoformat,
        help="First included UTC date (YYYY-MM-DD)",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output JSON file")
    return parser


def _validate_coins(value: str) -> list[str]:
    """Normalize comma-separated Binance spot symbols and reject unsafe path components."""
    coins = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not coins or any(not re.fullmatch(r"[A-Z0-9]{3,20}USDT", coin) for coin in coins):
        raise argparse.ArgumentTypeError(
            "coins must be comma-separated USDT spot symbols, such as BTCUSDT"
        )
    return list(dict.fromkeys(coins))


class LakeReader:
    """Bounded, read-only Parquet reader backed by two DuckDB connections to a temporary catalog."""

    def __init__(self, lake: Path, batch_rows: int = DUCKDB_BATCH_ROWS) -> None:
        self.lake = lake
        self.batch_rows = batch_rows
        self._temp: tempfile.TemporaryDirectory[str] | None = None
        self._stream_connection: duckdb.DuckDBPyConnection | None = None
        self._window_connection: duckdb.DuckDBPyConnection | None = None

    def __enter__(self) -> LakeReader:
        self._temp = tempfile.TemporaryDirectory(prefix="cryptonite-preset-replay-")
        catalog = Path(self._temp.name) / "replay.duckdb"
        with duckdb.connect(
            str(catalog), config={"memory_limit": DUCKDB_MEMORY_LIMIT, "threads": 2}
        ) as initializer:
            initializer.execute("SELECT 1")
        self._stream_connection = duckdb.connect(
            str(catalog), read_only=True, config={"memory_limit": DUCKDB_MEMORY_LIMIT, "threads": 2}
        )
        self._window_connection = duckdb.connect(
            str(catalog), read_only=True, config={"memory_limit": DUCKDB_MEMORY_LIMIT, "threads": 2}
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._stream_connection is not None:
            self._stream_connection.close()
        if self._window_connection is not None:
            self._window_connection.close()
        if self._temp is not None:
            self._temp.cleanup()

    def iter_batches(self, coin: str, timeframe: str, since_ns: int) -> Iterator[pl.DataFrame]:
        """Yield a stable, deduplicated stream of bars from the requested date onward."""
        coin_dir = self.lake / "binance_klines_enriched" / "binance" / "spot" / timeframe / coin
        if not any(coin_dir.glob("date=*/*.parquet")):
            return
        connection = self._required(self._stream_connection)
        query, _ = self._query(coin_dir, timeframe)
        sql = (
            query
            + " WHERE ts_event_ns >= ?"
            + self._dedupe_order(timeframe)
            + " ORDER BY ts_event_ns"
        )
        result = connection.execute(sql, [since_ns])
        reader = result.to_arrow_reader(batch_size=self.batch_rows)
        for batch in reader:
            yield pl.from_arrow(batch)

    def read_window(
        self,
        coin: str,
        timeframe: str,
        start_ns: int,
        end_ns: int,
        *,
        limit: int | None = None,
        descending: bool = False,
    ) -> pl.DataFrame:
        """Read a bounded time window or recent tail for base/HTF indicator context."""
        coin_dir = self.lake / "binance_klines_enriched" / "binance" / "spot" / timeframe / coin
        if not any(coin_dir.glob("date=*/*.parquet")):
            return pl.DataFrame()
        connection = self._required(self._window_connection)
        query, source = self._query(coin_dir, timeframe)
        order = "DESC" if descending else "ASC"
        sql = (
            query
            + " WHERE ts_event_ns >= ? AND ts_event_ns <= ?"
            + self._dedupe_order(timeframe)
            + f" ORDER BY ts_event_ns {order}"
        )
        params: list[Any] = [start_ns, end_ns]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        table = connection.execute(sql, params).to_arrow_table()
        result = pl.from_arrow(table)
        return result.sort("ts_event_ns") if descending else result

    @staticmethod
    def _required(connection: duckdb.DuckDBPyConnection | None) -> duckdb.DuckDBPyConnection:
        if connection is None:
            raise RuntimeError("LakeReader must be used as a context manager")
        return connection

    @staticmethod
    def _query(coin_dir: Path, timeframe: str) -> tuple[str, str]:
        """Build a Parquet SELECT with its path escaped as a SQL string literal."""
        source = str(coin_dir / "date=*" / "*.parquet")
        source_sql = source.replace("'", "''")
        return (
            "SELECT ts_event_ns::BIGINT AS ts_event_ns, open::DOUBLE AS open, "
            "high::DOUBLE AS high, low::DOUBLE AS low, close::DOUBLE AS close, volume::DOUBLE AS volume "
            f"FROM read_parquet('{source_sql}')",
            source,
        )

    @staticmethod
    def _dedupe_order(timeframe: str) -> str:
        """Select the best received record per catalog event timestamp, matching AnalyticsQueryEngine."""
        lag = timeframe_ns(timeframe) - OPEN_STAMP_RANK_LAG_NS
        early = EARLY_RECEIPT_TOLERANCE_NS
        return f""" QUALIFY row_number() OVER (
            PARTITION BY ts_init_ns
            ORDER BY CASE
                WHEN received_at_ns::BIGINT - ts_event_ns::BIGINT = {lag} THEN 2
                WHEN received_at_ns::BIGINT < ts_event_ns::BIGINT - {early} THEN 1
                ELSE 0
            END ASC,
            ts_event_ns DESC, received_at_ns DESC, ingest_batch_id DESC, payload_checksum DESC
        ) = 1"""


def timeframe_ns(timeframe: str) -> int:
    """Convert the supported lake timeframe syntax to nanoseconds."""
    units = {"m": 60, "h": 3600, "d": 86400}
    if not timeframe[:-1].isdigit() or timeframe[-1:] not in units:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    amount = int(timeframe[:-1])
    if amount <= 0:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    return amount * units[timeframe[-1]] * 1_000_000_000


def _requires_htf(preset) -> bool:
    """Return whether a registered preset uses higher-timeframe fields."""
    return any(
        condition.field.startswith("htf_")
        or (isinstance(condition.value, str) and condition.value.startswith("htf_"))
        for group in preset.rules
        for condition in group.conditions
    )


def run(lake: Path, coins: list[str], since: date) -> dict[str, Any]:
    """Replay each registered preset without retaining full histories in Python memory."""
    since_ns = int(datetime.combine(since, datetime.min.time(), UTC).timestamp()) * 1_000_000_000
    summary_state: dict[tuple[str, str], dict[str, Any]] = {
        (preset.slug, coin): {"trades": [], "first": None, "last": None}
        for preset in _PROFILE_PRESETS.values()
        for coin in coins
    }

    with LakeReader(lake) as reader:
        for preset in _PROFILE_PRESETS.values():
            configured_coins = {
                item.split(".", maxsplit=1)[0].upper() for item in preset.universe.instrument_ids
            }
            for coin in coins:
                if coin not in configured_coins:
                    continue
                summary = summary_state[(preset.slug, coin)]
                for timeframe in preset.universe.timeframes:
                    initial_context = reader.read_window(
                        coin,
                        timeframe,
                        since_ns - INDICATOR_CONTEXT_BARS * timeframe_ns(timeframe),
                        since_ns - 1,
                        limit=INDICATOR_CONTEXT_BARS,
                        descending=True,
                    )
                    htf_loader = None
                    if _requires_htf(preset):
                        htf_timeframe = HTF_MAP[timeframe]

                        def load_htf(start: int, end: int) -> pl.DataFrame:
                            return reader.read_window(coin, htf_timeframe, start, end)

                        htf_loader = load_htf
                    trades, first_ns, last_ns = replay_batches(
                        reader.iter_batches(coin, timeframe, since_ns),
                        preset,
                        coin,
                        timeframe,
                        htf_loader=htf_loader,
                        initial_context=initial_context,
                        context_bars=INDICATOR_CONTEXT_BARS,
                    )
                    summary["trades"].extend(trades)
                    if first_ns is not None:
                        summary["first"] = (
                            first_ns
                            if summary["first"] is None
                            else min(summary["first"], first_ns)
                        )
                        summary["last"] = (
                            last_ns if summary["last"] is None else max(summary["last"], last_ns)
                        )

    results = [
        summarize_replay(slug, coin, data["trades"], data["first"], data["last"])
        for (slug, coin), data in summary_state.items()
    ]
    return report_document(results)


def main() -> None:
    """Parse arguments, run the replay and write the report outside the lake."""
    parser = _parser()
    args = parser.parse_args()
    try:
        coins = _validate_coins(args.coins)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if args.out.resolve().is_relative_to(args.lake.resolve()):
        parser.error("--out must resolve outside --lake; the analytics lake is read-only")
    report = run(args.lake, coins, args.since)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
