"""Read-only spot replay of scanner presets against the analytics lake (ADR 0011, P1.9).

The output is one row per preset × requested coin. DuckDB opens a temporary catalog in read-only
mode; bars are read from Parquet only, and this program never writes to the analytics lake.

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
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
from cryptonite_scanner.preset_stats import replay_entries, report_document, summarize_replay
from cryptonite_scanner.presets import _PROFILE_PRESETS


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


def _read_bars(lake: Path, coin: str, timeframe: str, since: date) -> pl.DataFrame:
    """Read deduplicated OHLCV bars from Parquet using a read-only DuckDB catalog."""
    coin_dir = lake / "binance_klines_enriched" / "binance" / "spot" / timeframe / coin
    source = coin_dir / "date=*" / "*.parquet"
    if not any(coin_dir.glob("date=*/*.parquet")):
        return pl.DataFrame()
    # DuckDB refuses a read-only in-memory database. Create an empty temporary catalog, then reopen it
    # read-only; the catalog is outside the lake and all queries are SELECTs over read-only Parquet files.
    with tempfile.TemporaryDirectory(prefix="cryptonite-preset-replay-") as temp_dir:
        catalog = Path(temp_dir) / "replay.duckdb"
        with duckdb.connect(
            str(catalog), config={"memory_limit": "1GB", "threads": 2}
        ) as initializer:
            initializer.execute("SELECT 1")
        connection = duckdb.connect(
            str(catalog), read_only=True, config={"memory_limit": "1GB", "threads": 2}
        )
        try:
            source_sql = str(source).replace("'", "''")
            since_ns = (
                int(datetime.combine(since, datetime.min.time(), UTC).timestamp()) * 1_000_000_000
            )
            rows = connection.execute(
                f"""
                SELECT ts_event_ns::BIGINT AS ts_event_ns,
                       open::DOUBLE AS open, high::DOUBLE AS high,
                       low::DOUBLE AS low, close::DOUBLE AS close,
                       volume::DOUBLE AS volume
                FROM read_parquet('{source_sql}')
                WHERE ts_event_ns >= ?
                QUALIFY row_number() OVER (
                    PARTITION BY ts_init_ns
                    ORDER BY
                        CASE
                            WHEN received_at_ns::BIGINT < ts_event_ns::BIGINT THEN 1
                            ELSE 0
                        END ASC,
                        ts_event_ns DESC, received_at_ns DESC,
                        ingest_batch_id DESC, payload_checksum DESC
                ) = 1
                ORDER BY ts_event_ns
                """,
                [since_ns],
            ).fetch_arrow_table()
        finally:
            connection.close()
    return pl.from_arrow(rows)


def _validate_coins(value: str) -> list[str]:
    """Normalize comma-separated Binance spot symbols and reject unsafe path components."""
    coins = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not coins or any(not re.fullmatch(r"[A-Z0-9]{3,20}USDT", coin) for coin in coins):
        raise argparse.ArgumentTypeError(
            "coins must be comma-separated USDT spot symbols, such as BTCUSDT"
        )
    return list(dict.fromkeys(coins))


def run(lake: Path, coins: list[str], since: date) -> dict[str, Any]:
    """Replay all registered presets for each requested coin, across preset timeframes."""
    results: list[dict[str, Any]] = []
    frame_cache: dict[tuple[str, str], pl.DataFrame] = {}
    for preset in _PROFILE_PRESETS.values():
        for coin in coins:
            all_trades = []
            timestamps: list[int] = []
            configured_coins = {
                item.split(".", maxsplit=1)[0].upper() for item in preset.universe.instrument_ids
            }
            timeframes = preset.universe.timeframes if coin in configured_coins else []
            for timeframe in timeframes:
                key = (coin, timeframe)
                if key not in frame_cache:
                    frame_cache[key] = _read_bars(lake, coin, timeframe, since)
                frame = frame_cache[key]
                if frame.is_empty():
                    continue
                timestamps.extend(int(value) for value in frame.get_column("ts_event_ns").to_list())
                all_trades.extend(replay_entries(frame, preset, coin, timeframe))
            results.append(
                summarize_replay(
                    preset.slug,
                    coin,
                    all_trades,
                    min(timestamps) if timestamps else None,
                    max(timestamps) if timestamps else None,
                )
            )
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
