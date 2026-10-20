"""Compaction of the Analytics Lake's closed date partitions (docs/runbooks/host-stability.md §6).

Ingest writes one small file per flush (about 700 a day for one 1m symbol), and every lake read opens every file
it globs. ``run`` rewrites each closed partition as one zstd file without changing a row; ``prune_quarantine``
deletes quarantined originals once they are old enough and their rows are proven to be in the lake. Used by
``scripts/compact_lake.py`` (by hand) and ``cryptonite_storage.lake_maintenance`` (the nightly service).

For every ``binance_klines_enriched/<venue>/<market>/<timeframe>/<symbol>/date=<D>`` with D at most yesterday
(UTC, counted from one hour ago, so before 01:00 UTC yesterday's stragglers still have time to land) and more
than one file:

1. copy all of its files, unchanged, into ``<symbol>/.compact-tmp-date=<D>.parquet.tmp`` (zstd, sorted by bar);
   the temp file sits outside every ``date=*/*.parquet`` glob, so no reader sees it half-written;
2. check the copy against the originals: same column names and types, same row count, same number of distinct
   ``ts_init_ns`` and the same order-independent hash of every row;
3. ``os.replace`` it into the partition as ``part-compacted-<stamp>.parquet``;
4. only then MOVE the originals to ``<quarantine>/<same path relative to the lake>``, with a
   ``compaction-<stamp>.json`` manifest beside them.

A partition that fails a check is left as it was. Between steps 3 and 4 a reader sees each row twice; every
bar read goes through ``query_bars_df``, which keeps one row per bar, so the answer is the same. A reader that
globbed before step 4 and opens a moved file gets an IOException and ``query_bars_df`` reads again.

Idempotent: a partition already down to one file is skipped. When a straggler or a backfill adds files to a
compacted partition, the next run compacts the compacted file and the new ones together and quarantines all
of them. Rows are never de-duplicated here; ``query_bars_df`` picks one row per bar at read time, as before.
"""


from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from cryptonite_storage.queries import lake_symbol, lake_timeframe

ROOT = "binance_klines_enriched"
COMPACTED_PREFIX = "part-compacted-"
TMP_PREFIX = ".compact-tmp-"
# a batch's partition is its first bar's date, so yesterday can still get a file just after midnight
SETTLE = timedelta(hours=1)
SORT_KEY = "ts_init_ns, ts_event_ns, received_at_ns, ingest_batch_id, payload_checksum"
DUCKDB_CONFIG = {"threads": 4, "memory_limit": "1GB", "temp_directory": "/tmp"}
# names the writers produce (part-<uuid>.parquet, part-<stem>-b1repair.parquet, part-compacted-<stamp>.parquet);
# anything else is interpolated into SQL nowhere and leaves its partition alone
_FILE_NAME = re.compile(r"[A-Za-z0-9_.=-]+\.parquet")


def last_compactable_date(now: datetime) -> date:
    """The newest partition date this run may compact."""
    return (now.astimezone(UTC) - SETTLE).date() - timedelta(days=1)


@dataclass
class Partition:
    symbol: str
    timeframe: str
    day: date
    path: Path
    files: list[Path]


@dataclass
class Result:
    partition: Partition
    status: str  # planned | compacted | skipped | failed
    detail: str = ""
    rows: int = 0
    compacted: Path | None = None
    quarantined: list[Path] = field(default_factory=list)


def find_partitions(lake: Path, cutoff: date, symbols: set[str] | None = None,
                    timeframes: set[str] | None = None) -> list[Partition]:
    """Closed partitions (date <= cutoff) holding more than one visible parquet file."""
    out: list[Partition] = []
    for d in sorted((lake / ROOT).glob("*/*/*/*/date=*")):
        if not d.is_dir():
            continue
        tf, sym = d.parent.parent.name, d.parent.name
        if (symbols and sym not in symbols) or (timeframes and tf not in timeframes):
            continue
        try:
            day = date.fromisoformat(d.name.removeprefix("date="))
        except ValueError:
            continue
        if day > cutoff:
            continue
        # a dot-file is a writer's temp file, not data until it is renamed
        files = sorted(f for f in d.glob("*.parquet") if not f.name.startswith("."))
        if len(files) > 1:
            out.append(Partition(sym, tf, day, d, files))
    return out


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _source(files: list[Path]) -> str:
    # hive_partitioning off: the files themselves carry no date column, and the copy must not gain one
    return f"read_parquet([{', '.join(_sql_str(str(f)) for f in files)}], hive_partitioning=false)"


def fingerprint(con: duckdb.DuckDBPyConnection, files: list[Path]) -> dict:
    """Column names and types, row count, distinct bars, and an order-independent hash of every row."""
    src = _source(files)
    columns = [(r[0], r[1]) for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
    rows, bars, row_hash = con.execute(
        f"SELECT count(*), count(DISTINCT ts_init_ns), coalesce(sum(hash(t)::HUGEINT), 0) FROM {src} t"
    ).fetchone()
    return {"columns": columns, "rows": int(rows), "bars": int(bars), "row_hash": str(row_hash)}


def _check_names(part: Partition) -> str | None:
    try:
        if lake_symbol(part.symbol) != part.symbol or lake_timeframe(part.timeframe) != part.timeframe:
            return "not a lake symbol/timeframe"
    except ValueError as e:
        return str(e)
    bad = [f.name for f in part.files if not _FILE_NAME.fullmatch(f.name)]
    return f"unexpected file names {bad}" if bad else None


def _same_file_schemas(files: list[Path]) -> bool:
    first = pq.read_schema(files[0])
    return all(pq.read_schema(f).equals(first) for f in files[1:])


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def compact_partition(con: duckdb.DuckDBPyConnection, part: Partition, lake: Path, quarantine: Path,
                      stamp: str) -> Result:
    if reason := _check_names(part):
        return Result(part, "skipped", reason)
    if not _same_file_schemas(part.files):
        return Result(part, "skipped", "files have different schemas")

    before = fingerprint(con, part.files)
    tmp = part.path.parent / f"{TMP_PREFIX}{part.path.name}.parquet.tmp"
    try:
        con.execute(f"COPY (SELECT * FROM {_source(part.files)} ORDER BY {SORT_KEY}) "
                    f"TO {_sql_str(str(tmp))} (FORMAT parquet, COMPRESSION zstd)")
        _fsync_path(tmp)
        after = fingerprint(con, [tmp])
        if after != before:
            diff = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
            return Result(part, "failed", f"compacted copy differs from the originals: {diff}", before["rows"])
        target = part.path / f"{COMPACTED_PREFIX}{stamp}.parquet"
        if target.exists():
            return Result(part, "failed", f"{target.name} already exists", before["rows"])
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)  # our own temp copy; after a successful replace it is already gone
    _fsync_path(part.path)

    moved: list[Path] = []
    for f in part.files:
        dest = quarantine / f.relative_to(lake)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():  # an earlier run quarantined a file of the same name: keep both
            dest = dest.with_name(f"{dest.name}.{stamp}")
        os.rename(f, dest)  # moved, never deleted
        moved.append(dest)
    _fsync_path(part.path)
    manifest = {
        "partition": str(part.path.relative_to(lake)), "compacted_file": target.name, "stamp": stamp,
        "originals": [f.name for f in part.files], "quarantined_as": [str(m.relative_to(quarantine)) for m in moved],
        "rows": before["rows"], "bars": before["bars"], "row_hash": before["row_hash"],
        "columns": before["columns"],
    }
    (quarantine / part.path.relative_to(lake) / f"compaction-{stamp}.json").write_text(
        json.dumps(manifest, indent=2) + "\n")
    return Result(part, "compacted", rows=before["rows"], compacted=target, quarantined=moved)


def plan(part: Partition) -> Result:
    """Dry run: what compaction would do, from Parquet footers only."""
    if reason := _check_names(part):
        return Result(part, "skipped", reason)
    rows = sum(pq.ParquetFile(f).metadata.num_rows for f in part.files)
    size = sum(f.stat().st_size for f in part.files)
    return Result(part, "planned", f"{len(part.files)} files, {size / 1e6:.1f} MB", rows)


def run(lake: Path, quarantine: Path, *, now: datetime | None = None, symbols: set[str] | None = None,
        timeframes: set[str] | None = None, dry_run: bool = False) -> list[Result]:
    now = (now or datetime.now(UTC)).astimezone(UTC)  # also the manifests' stamp, which ages the quarantine
    cutoff = last_compactable_date(now)
    parts = find_partitions(lake, cutoff, symbols, timeframes)
    if dry_run:
        return [plan(p) for p in parts]
    results: list[Result] = []
    con = duckdb.connect(":memory:", config=DUCKDB_CONFIG)
    try:
        for part in parts:
            stamp = f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
            try:
                results.append(compact_partition(con, part, lake, quarantine, stamp))
            except (duckdb.Error, OSError) as e:
                results.append(Result(part, "failed", f"{type(e).__name__}: {e}"))
    finally:
        con.close()
    return results


# --- quarantine prune -------------------------------------------------------------------------------------------

MANIFEST_GLOB = "compaction-*.json"
_STAMP = re.compile(r"\d{8}T\d{6}Z")
_MAX_CHAIN = 100


@dataclass
class PruneResult:
    manifest: Path
    status: str  # pruned | unverified
    detail: str = ""
    files: int = 0


def manifest_time(manifest: dict, path: Path) -> datetime:
    """When the compaction ran: the manifest's stamp, else the manifest file's mtime (the originals keep theirs)."""
    if match := _STAMP.match(str(manifest.get("stamp", ""))):
        return datetime.strptime(match.group(0), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def _why_not_in_lake(con: duckdb.DuckDBPyConnection, lake: Path, path: Path, manifest: dict,
                     depth: int = 0) -> str | None:
    """None when the rows of this manifest's originals are provably in the lake now, else the reason.

    The compacted file the manifest names must still be in its partition and match the row count, distinct bars
    and row hash recorded when it was checked against the originals. If a later run recompacted the partition,
    that file was quarantined in turn, and the later manifest that lists it among its originals must pass the
    same test."""
    if depth > _MAX_CHAIN:
        return "manifest chain too long"
    compacted = lake / manifest["partition"] / manifest["compacted_file"]
    if compacted.exists():
        fp = fingerprint(con, [compacted])
        want = (manifest["rows"], manifest["bars"], str(manifest["row_hash"]))
        if (fp["rows"], fp["bars"], fp["row_hash"]) != want:
            return f"{compacted.name} no longer matches its manifest"
        return None
    for other in sorted(path.parent.glob(MANIFEST_GLOB)):
        if other == path:
            continue
        later = json.loads(other.read_text())
        if manifest["compacted_file"] in later.get("originals", []):
            return _why_not_in_lake(con, lake, other, later, depth + 1)
    return f"{manifest['compacted_file']} is neither in the lake nor recompacted by a later run"


def prune_quarantine(lake: Path, quarantine: Path, *, now: datetime, keep_days: int = 14,
                     con: duckdb.DuckDBPyConnection | None = None) -> list[PruneResult]:
    """Delete the quarantined originals of every compaction older than ``keep_days`` whose rows are provably in the
    lake (``_why_not_in_lake``), oldest first, with their manifest. Anything unproven is kept and reported as
    ``unverified``. Recent compactions are not looked at."""
    if not quarantine.is_dir():
        return []
    cutoff = now.astimezone(UTC) - timedelta(days=keep_days)
    root = quarantine.resolve()
    entries: list[tuple[datetime, Path, dict | None]] = []
    for path in quarantine.rglob(MANIFEST_GLOB):
        try:
            manifest = json.loads(path.read_text())
            entries.append((manifest_time(manifest, path), path, manifest))
        except (OSError, ValueError):
            entries.append((datetime.fromtimestamp(path.stat().st_mtime, UTC), path, None))
    own = con is None
    con = con or duckdb.connect(":memory:", config=DUCKDB_CONFIG)
    results: list[PruneResult] = []
    try:
        for when, path, manifest in sorted(entries, key=lambda e: (e[0], str(e[1]))):
            if when > cutoff:
                continue
            if manifest is None:
                results.append(PruneResult(path, "unverified", "manifest is not readable JSON"))
                continue
            try:
                reason = _why_not_in_lake(con, lake, path, manifest)
                files = [(quarantine / rel).resolve() for rel in manifest["quarantined_as"]]
            except (KeyError, TypeError, ValueError, duckdb.Error, OSError) as e:
                reason = f"{type(e).__name__}: {e}"
            if reason is None and not all(f.is_relative_to(root) for f in files):
                reason = "manifest names a file outside the quarantine"
            if reason:
                results.append(PruneResult(path, "unverified", reason))
                continue
            removed = 0
            for f in files:
                if f.exists():
                    f.unlink()
                    removed += 1
            path.unlink()
            for d in path.parents:  # drop the directories this emptied, never the quarantine root
                if d.resolve() == root or not d.is_relative_to(quarantine) or any(d.iterdir()):
                    break
                d.rmdir()
            results.append(PruneResult(path, "pruned", files=removed))
    finally:
        if own:
            con.close()
    return results
