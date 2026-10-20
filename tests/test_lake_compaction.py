"""``scripts/compact_lake.py`` rewrites each closed date partition as one file without changing a row, moves (never
deletes) the originals to quarantine, recompacts when stragglers or a backfill add files, and shares the lake
maintenance lock with the backfill scripts and the open-stamped repair."""

import importlib.util
import json
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest
from cryptonite_schemas.bar import EnrichedBar
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.lake_lock import EXIT_LOCKED, LakeLockBusy, lake_maintenance_lock
from cryptonite_storage.queries import AnalyticsQueryEngine

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compact_lake.py"
MIN = 60 * 10**9
DAY = 24 * 60 * MIN
D1 = 1_791_072_000 * 10**9  # 2026-10-04 00:00 UTC
NOW = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)  # 10-06 is the newest closed partition


def load_script():
    spec = importlib.util.spec_from_file_location("compact_lake", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


compact = load_script()


def bar(symbol: str, close_ns: int, *, received_ns: int | None = None, batch: str = "", label: str = "") -> EnrichedBar:
    price = Decimal("100") + Decimal(close_ns // MIN % 1000) / 4
    return EnrichedBar(
        instrument_id=f"{symbol}.BINANCE", bar_type=f"{symbol}.BINANCE-1-MINUTE-LAST-EXTERNAL", ingest_batch_id=batch,
        payload_checksum=f"sum-{batch}", ts_event_ns=close_ns, ts_init_ns=close_ns,
        received_at_ns=close_ns - 325_000_000 if received_ns is None else received_ns,
        price_precision=2, size_precision=5, open=price, high=price + 1, low=price - 1, close=price,
        volume=Decimal("1.5"), quote_volume=Decimal("150"), taker_buy_base_volume=Decimal("0"),
        taker_buy_quote_volume=Decimal("0"), trades_count=3, open_raw=str(price), high_raw=str(price + 1),
        low_raw=str(price - 1), close_raw=label or str(price), volume_raw="1.5",
    )


def write(lake: Path, symbol: str, first_close_ns: int, n: int, batch: str, tf: str = "1m", **kw) -> Path:
    bars = [bar(symbol, first_close_ns + i * MIN, batch=batch, **kw) for i in range(n)]
    path, _ = AnalyticsLakeWriter(lake).write_batch(bars, symbol=symbol, timeframe=tf, batch_id=batch)
    return Path(path)


def partition(lake: Path, symbol: str, day: str, tf: str = "1m") -> Path:
    return lake / "binance_klines_enriched" / "binance" / "spot" / tf / symbol / f"date={day}"


def visible(p: Path) -> list[str]:
    return sorted(f.name for f in p.glob("*.parquet"))


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    lake = tmp_path / "analytics_lake"
    # 10-04: ten small flushes with a re-sent minute (a duplicate bar, received later) and an open-stamped-like row
    for i in range(10):
        write(lake, "BTCUSDT", D1 + MIN + i * 30 * MIN, 30, f"btc-a{i}")
    write(lake, "BTCUSDT", D1 + 5 * MIN, 3, "btc-resend", received_ns=D1 + 9 * MIN, label="resent")
    # 10-05: three files; 10-06: two files; 10-07 (today): two files, never touched
    for i in range(3):
        write(lake, "BTCUSDT", D1 + DAY + MIN + i * 100 * MIN, 100, f"btc-b{i}")
    write(lake, "BTCUSDT", D1 + 2 * DAY + MIN, 50, "btc-c0")
    write(lake, "BTCUSDT", D1 + 2 * DAY + 51 * MIN, 50, "btc-c1")
    write(lake, "BTCUSDT", D1 + 3 * DAY + MIN, 10, "btc-today0")
    write(lake, "BTCUSDT", D1 + 3 * DAY + 11 * MIN, 10, "btc-today1")
    # a batch that started 23:50 on 10-05 and runs past midnight stays in 10-05 (its first bar's date)
    write(lake, "BTCUSDT", D1 + DAY + 23 * 60 * MIN + 50 * MIN, 20, "btc-midnight")
    # ETH 1m: one file only (nothing to do); BTC 1h: two files
    write(lake, "ETHUSDT", D1 + MIN, 60, "eth-a")
    write(lake, "BTCUSDT", D1 + 60 * MIN, 5, "btc-h0", tf="1h")
    write(lake, "BTCUSDT", D1 + 6 * 60 * MIN, 5, "btc-h1", tf="1h")
    return lake


def read_all(lake: Path, symbol: str = "BTCUSDT", tf: str = "1m"):
    return AnalyticsQueryEngine(lake).query_bars_df(symbol, tf)


def raw_rows(lake: Path, symbol: str = "BTCUSDT", tf: str = "1m") -> list[tuple]:
    """Every stored row, duplicates included, with the hive ``date`` column readers see."""
    glob = str(lake / "binance_klines_enriched" / "binance" / "spot" / tf / symbol / "date=*" / "*.parquet")
    return sorted(duckdb.connect().execute(f"SELECT * FROM read_parquet('{glob}')").fetchall(), key=repr)


def schema(lake: Path, symbol: str = "BTCUSDT", tf: str = "1m") -> list[tuple]:
    glob = str(lake / "binance_klines_enriched" / "binance" / "spot" / tf / symbol / "date=*" / "*.parquet")
    return duckdb.connect().execute(f"DESCRIBE SELECT * FROM read_parquet('{glob}')").fetchall()


def test_last_compactable_date_waits_until_0100_utc():
    assert compact.last_compactable_date(datetime(2026, 10, 7, 0, 59, tzinfo=UTC)) == date(2026, 10, 5)
    assert compact.last_compactable_date(datetime(2026, 10, 7, 1, 0, tzinfo=UTC)) == date(2026, 10, 6)
    assert compact.last_compactable_date(datetime(2026, 10, 7, 23, 59, tzinfo=UTC)) == date(2026, 10, 6)


def test_compaction_is_lossless_and_moves_the_originals_to_quarantine(lake):
    q = lake.parent / "analytics_lake_quarantine"
    before_bars, before_rows, before_schema = read_all(lake), raw_rows(lake), schema(lake)
    originals = {d: visible(partition(lake, "BTCUSDT", d)) for d in ("2026-10-04", "2026-10-05", "2026-10-06")}
    today_files = visible(partition(lake, "BTCUSDT", "2026-10-07"))

    results = compact.run(lake, q, now=NOW)

    done = {(r.partition.symbol, r.partition.timeframe, str(r.partition.day)): r for r in results}
    assert {k for k, r in done.items() if r.status == "compacted"} == {
        ("BTCUSDT", "1m", "2026-10-04"), ("BTCUSDT", "1m", "2026-10-05"), ("BTCUSDT", "1m", "2026-10-06"),
        ("BTCUSDT", "1h", "2026-10-04"),
    }
    assert all(r.status == "compacted" for r in results), [(r.status, r.detail) for r in results]
    for d, names in originals.items():
        part = partition(lake, "BTCUSDT", d)
        files = visible(part)
        assert len(files) == 1 and files[0].startswith("part-compacted-")
        assert pq.ParquetFile(part / files[0]).metadata.row_group(0).column(0).compression == "ZSTD"
        # every original moved, none deleted, with a manifest
        qdir = q / part.relative_to(lake)
        assert sorted(f.name for f in qdir.glob("*.parquet")) == names
        manifest = json.loads(next(qdir.glob("compaction-*.json")).read_text())
        assert manifest["originals"] == names and manifest["compacted_file"] == files[0]
    assert visible(partition(lake, "BTCUSDT", "2026-10-07")) == today_files  # today: untouched
    assert len(visible(partition(lake, "ETHUSDT", "2026-10-04"))) == 1  # one file: nothing to do

    # the same rows (duplicates included), the same schema incl. the hive date column, the same bars picked
    assert raw_rows(lake) == before_rows
    assert schema(lake) == before_schema
    assert read_all(lake).to_dicts() == before_bars.to_dicts()
    assert read_all(lake, tf="1h").height == 10
    # no temp file left behind
    assert not list(lake.rglob(".compact-tmp-*"))


def test_a_second_run_changes_nothing(lake):
    q = lake.parent / "q"
    compact.run(lake, q, now=NOW)
    snapshot = sorted(str(p) for p in lake.rglob("*"))
    assert compact.run(lake, q, now=NOW) == []
    assert sorted(str(p) for p in lake.rglob("*")) == snapshot


def test_a_straggler_or_backfill_is_recompacted_with_the_compacted_file(lake):
    q = lake.parent / "q"
    compact.run(lake, q, now=NOW)
    part = partition(lake, "BTCUSDT", "2026-10-06")
    first = visible(part)[0]
    # a straggler re-sends two minutes (received later, so it wins) and a backfill adds the rest of the day
    write(lake, "BTCUSDT", D1 + 2 * DAY + 10 * MIN, 2, "btc-straggler", received_ns=D1 + 2 * DAY + 30 * MIN,
          label="straggler")
    write(lake, "BTCUSDT", D1 + 2 * DAY + 200 * MIN, 30, "btc-backfill")
    before_bars, before_rows = read_all(lake), raw_rows(lake)

    results = compact.run(lake, q, now=NOW)

    assert [(str(r.partition.day), r.status) for r in results] == [("2026-10-06", "compacted")]
    files = visible(part)
    assert len(files) == 1 and files[0] != first
    qnames = sorted(f.name for f in (q / part.relative_to(lake)).glob("*.parquet"))
    assert first in qnames and len(qnames) == 2 + 3  # the first two originals, the old compacted file, two new
    assert raw_rows(lake) == before_rows
    assert read_all(lake).to_dicts() == before_bars.to_dicts()
    straggled = read_all(lake).filter(read_all(lake)["close_raw"] == "straggler")
    assert straggled.height == 2


def test_filters_and_dry_run(lake, capsys):
    q = lake.parent / "q"
    snapshot = sorted(str(p) for p in lake.rglob("*"))
    planned = compact.run(lake, q, now=NOW, dry_run=True)
    assert {r.status for r in planned} == {"planned"} and len(planned) == 4
    assert sum(r.rows for r in planned if r.partition.timeframe == "1m") == 300 + 3 + 300 + 20 + 100
    assert sorted(str(p) for p in lake.rglob("*")) == snapshot and not q.exists()

    assert os.path.exists(lake / "binance_klines_enriched")
    rc = compact.main(["--lake", str(lake), "--quarantine", str(q), "--dry-run"])
    assert rc == 0 and "dry run, nothing changed" in capsys.readouterr().out
    assert sorted(str(p) for p in lake.rglob("*")) == snapshot

    only_1h = compact.run(lake, q, now=NOW, timeframes={"1h"})
    assert [(r.partition.timeframe, r.status) for r in only_1h] == [("1h", "compacted")]
    assert compact.run(lake, q, now=NOW, symbols={"ETHUSDT"}) == []


def test_a_failed_check_leaves_the_partition_as_it_was(lake, monkeypatch):
    q = lake.parent / "q"
    first = partition(lake, "BTCUSDT", "2026-10-04")  # the first partition the run reaches
    names = visible(first)
    real = compact.fingerprint
    calls = []

    def lying_fingerprint(con, files):
        fp = real(con, files)
        calls.append(files)
        if len(calls) == 2:  # the first partition's compacted copy
            fp = {**fp, "row_hash": "0"}
        return fp

    monkeypatch.setattr(compact, "fingerprint", lying_fingerprint)
    results = compact.run(lake, q, now=NOW, symbols={"BTCUSDT"}, timeframes={"1m"})
    assert [(str(r.partition.day), r.status) for r in results] == [
        ("2026-10-04", "failed"), ("2026-10-05", "compacted"), ("2026-10-06", "compacted")]
    assert "row_hash" in results[0].detail
    assert visible(first) == names  # nothing added, nothing moved
    assert not (q / first.relative_to(lake)).exists()
    assert not list(lake.rglob(".compact-tmp-*"))


def test_a_failed_partition_makes_the_run_exit_1(lake, monkeypatch, capsys):
    monkeypatch.setattr(compact, "fingerprint", lambda con, files, n=iter(range(10**6)): {"columns": [], "rows": 0, "bars": 0, "row_hash": next(n)})
    assert compact.main(["--lake", str(lake), "--quarantine", str(lake.parent / "q"), "--timeframe", "1h"]) == 1
    assert "failed" in capsys.readouterr().out


def test_mixed_schemas_are_skipped(lake):
    part = partition(lake, "BTCUSDT", "2026-10-06")
    odd = part / "part-odd.parquet"
    duckdb.connect().execute(f"COPY (SELECT 1::BIGINT AS ts_init_ns) TO '{odd}' (FORMAT parquet)")
    results = {str(r.partition.day): r for r in compact.run(lake, lake.parent / "q", now=NOW)}
    assert results["2026-10-06"].status == "skipped" and "schemas" in results["2026-10-06"].detail
    assert "part-odd.parquet" in visible(part)
    assert results["2026-10-05"].status == "compacted"


def test_writer_temp_files_are_not_compacted(lake):
    part = partition(lake, "BTCUSDT", "2026-10-06")
    tmp = part / ".tmp.deadbeef.parquet"
    tmp.write_bytes(b"half-written")
    compact.run(lake, lake.parent / "q", now=NOW)
    assert tmp.exists()


def test_the_lock_is_shared_and_exclusive(lake, capsys):
    with lake_maintenance_lock(lake):
        with pytest.raises(LakeLockBusy):
            with lake_maintenance_lock(lake):
                pass
        with pytest.raises(SystemExit) as e:
            compact.main(["--lake", str(lake), "--quarantine", str(lake.parent / "q")])
        assert e.value.code == EXIT_LOCKED
        assert "holds" in capsys.readouterr().err
        # a dry run reads footers only and does not need the lock
        assert compact.main(["--lake", str(lake), "--quarantine", str(lake.parent / "q"), "--dry-run"]) == 0
    assert compact.main(["--lake", str(lake), "--quarantine", str(lake.parent / "q")]) == 0


@pytest.mark.parametrize("script", ["backfill_history.py", "backfill_gaps.py", "repair_lake_open_stamped_rows.py"])
def test_lake_writers_take_the_same_lock(script):
    src = (SCRIPT.parent / script).read_text()
    assert "lake_maintenance_lock_or_exit(" in src
