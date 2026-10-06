"""Export the B1 / F-C1 regression fixture: one real UTC day of lake 1m bars plus ingest's native 1h bars.

Read-only against the local Analytics Lake. Writes a miniature lake (same directory layout and parquet
schema as ``AnalyticsLakeWriter``) under this directory, so the test reads it through the production
``AnalyticsQueryEngine.query_bars_df``:

    uv run python tests/fixtures/b1_lake/export_b1_fixture.py --lake data/analytics_lake

1m rows: ``ts_event_ns`` (the bar CLOSE) in (DAY - 1h, DAY + 24h], i.e. every minute that opens on DAY plus
one hour of lead-in. 1h rows: ``ts_event_ns`` in (DAY, DAY + 24h], the 24 native bars that open on DAY.
``payload_checksum`` and ``ingest_batch_id`` are blanked to keep the fixture small; prices, volumes and
all timestamps are copied unchanged.
"""

import argparse
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from cryptonite_schemas.bar import ANALYTICS_PYARROW_SCHEMA

HERE = Path(__file__).resolve().parent
DAY = "2026-10-04"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
HOUR_NS = 3600 * 10**9
COLUMNS = [f.name for f in ANALYTICS_PYARROW_SCHEMA]


def export(lake: Path, symbol: str, timeframe: str, lo_ns: int, hi_ns: int) -> int:
    src = lake / "binance_klines_enriched" / "binance" / "spot" / timeframe / symbol / "date=*" / "*.parquet"
    con = duckdb.connect(":memory:")
    rows = con.execute(
        f"SELECT {', '.join(COLUMNS)} FROM read_parquet('{src}', hive_partitioning = false) "
        f"WHERE ts_event_ns > {lo_ns} AND ts_event_ns <= {hi_ns} ORDER BY ts_event_ns, received_at_ns"
    ).to_arrow_table()
    conflicts = con.execute(
        "SELECT count(*) FROM (SELECT ts_event_ns FROM rows GROUP BY 1 HAVING count(DISTINCT close) > 1)"
    ).fetchone()[0]
    if conflicts:
        raise SystemExit(f"{symbol} {timeframe}: {conflicts} timestamps with conflicting duplicates; pick another day")
    # one row per close timestamp (identical duplicates from overlapping batches add nothing)
    rows = con.execute("SELECT DISTINCT ON (ts_event_ns) * FROM rows ORDER BY ts_event_ns, received_at_ns DESC").to_arrow_table()
    rows = rows.set_column(COLUMNS.index("payload_checksum"), "payload_checksum", pa.array([""] * rows.num_rows))
    rows = rows.set_column(COLUMNS.index("ingest_batch_id"), "ingest_batch_id", pa.array([""] * rows.num_rows))
    rows = rows.cast(ANALYTICS_PYARROW_SCHEMA)

    dates = [datetime.fromtimestamp(ts / 1e9, tz=UTC).strftime("%Y-%m-%d") for ts in rows["ts_event_ns"].to_pylist()]
    for date in sorted(set(dates)):
        part = rows.filter(pa.array([d == date for d in dates]))
        out = HERE / "binance_klines_enriched" / "binance" / "spot" / timeframe / symbol / f"date={date}"
        out.mkdir(parents=True, exist_ok=True)
        pq.write_table(part, out / "part-b1-fixture.parquet", compression="zstd")
    return rows.num_rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lake", default="data/analytics_lake")
    args = ap.parse_args()
    day_ns = int(datetime.fromisoformat(f"{DAY}T00:00:00+00:00").timestamp()) * 10**9
    for sym in SYMBOLS:
        n1m = export(Path(args.lake), sym, "1m", day_ns - HOUR_NS, day_ns + 24 * HOUR_NS)
        n1h = export(Path(args.lake), sym, "1h", day_ns, day_ns + 24 * HOUR_NS)
        print(f"{sym}: {n1m} 1m rows, {n1h} 1h rows")


if __name__ == "__main__":
    main()
