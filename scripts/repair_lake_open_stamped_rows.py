"""Find and repair Analytics Lake rows the API wrote stamped at the bar OPEN (B1 / F-C1).

Ingest stamps every lake row at the bar CLOSE (``ts_event_ns = ts_init_ns = close``,
``EnrichedBar.from_binance_raw``). Until B1, the API's onboarding and backfill routes
(``services/api/cryptonite_api/routes/instruments.py``) stamped ``ts_event_ns = ts_init_ns = kline OPEN`` and
``received_at_ns = Binance close_time``, i.e. exactly ``ts_event_ns + timeframe - 1 ms``. No ingest row has
that lag (ingest takes ``received_at_ns`` from the wall clock), so it identifies them. Known on 2026-10-05:
BTC/ETH/BNB/SOL 2026-09-17 23:20 -> 09-18 16:39 UTC, WLD 09-21, XRP 09-20/21.

A second, older writer left MUBARAK/WLD/XRP 1m rows whose ``ts_event_ns`` is the Binance close_time itself
(``xx:xx:59.999``), one millisecond before the minute boundary.

For every flagged row the corrected close is computed (open-stamped: ``+ timeframe``; ``:59.999``: ``+ 1 ms``)
and the row is either rewritten at that close or quarantined:

- ``duplicate``: a good row of the same symbol/timeframe (unflagged, not recorded while still open) already
  has that close (ingest's copy wins);
- ``unclosed``: the file was written before that close, or the row is the newest open-stamped row of its
  file (the API fetched up to "now" and kept the forming kline), so the bar may be partial;
- ``unrecognized``: misaligned by something other than 1 ms; no safe correction;
- ``rewrite``: otherwise.

Dry run by default: prints what it would do and changes nothing. With ``--apply`` each affected file is
MOVED (never deleted) to ``<quarantine>/<path relative to the lake>.<UTC stamp>`` with a JSON manifest next
to it, and the kept + rewritten rows are written back to the same partition as ``part-<stem>-b1repair.parquet``.
Back up ``data/analytics_lake`` and stop ``cryptonite_ingest`` and ``cryptonite_scanner`` first:

    uv run python scripts/repair_lake_open_stamped_rows.py --lake data/analytics_lake           # dry run
    uv run python scripts/repair_lake_open_stamped_rows.py --lake data/analytics_lake --apply
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from cryptonite_storage.lake_lock import lake_maintenance_lock_or_exit
from cryptonite_storage.queries import (
    EARLY_RECEIPT_TOLERANCE_NS,
    open_stamped_receipt_lag_ns,
    timeframe_ns,
)

ROOT = "binance_klines_enriched"
# Only timeframes whose closes fall on multiples of their duration since the Unix epoch, so ``ts % tf``
# tests alignment. Binance 1w klines open on Monday (the epoch was a Thursday): every correct weekly row
# would look misaligned. The lake holds only 1m and 1h today.
REPAIRABLE_TIMEFRAMES = frozenset({"1m", "1h"})
CLOSE_TIME_REMAINDER_NS = -1_000_000  # Binance close_time = next boundary - 1 ms
REPAIRED_SUFFIX = "-b1repair"


@dataclass
class FileReport:
    path: Path
    symbol: str
    timeframe: str
    rows: int
    open_stamped: int = 0
    misaligned: int = 0
    disposition: Counter = field(default_factory=Counter)
    first_close_ns: int | None = None
    last_close_ns: int | None = None
    # per flagged row index: (corrected close ns or None, disposition)
    decisions: dict[int, tuple[int | None, str]] = field(default_factory=dict)

    @property
    def affected(self) -> bool:
        return bool(self.decisions)


def _fmt(ns: int | None) -> str:
    return "-" if ns is None else datetime.fromtimestamp(ns / 1e9, UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def lake_files(lake: Path, symbols: set[str] | None = None) -> dict[tuple[str, str], list[Path]]:
    """``{(symbol, timeframe): [parquet files]}`` under ``<lake>/binance_klines_enriched/*/*/<tf>/<sym>/date=*``."""
    out: dict[tuple[str, str], list[Path]] = {}
    for f in sorted((lake / ROOT).glob("*/*/*/*/date=*/*.parquet")):
        tf, sym = f.parent.parent.parent.name, f.parent.parent.name
        if symbols and sym not in symbols:
            continue
        out.setdefault((sym, tf), []).append(f)
    return out


def flag_rows(table: pa.Table, tf_ns: int) -> dict[int, tuple[str, int | None]]:
    """Row index -> (kind, corrected close ns or None) for every open-stamped or misaligned row."""
    ev = table.column("ts_event_ns").to_pylist()
    rcv = table.column("received_at_ns").to_pylist()
    lag = open_stamped_receipt_lag_ns(tf_ns)
    flagged: dict[int, tuple[str, int | None]] = {}
    for i, (e, r) in enumerate(zip(ev, rcv, strict=True)):
        if r - e == lag and e % tf_ns == 0:
            flagged[i] = ("open_stamped", e + tf_ns)
        elif e % tf_ns:
            fixable = e % tf_ns == tf_ns + CLOSE_TIME_REMAINDER_NS
            flagged[i] = ("misaligned", e - CLOSE_TIME_REMAINDER_NS if fixable else None)
    return flagged


def scan(lake: Path, symbols: set[str] | None = None) -> list[FileReport]:
    """Select the rows to repair. Reads only; changes nothing."""
    reports: list[FileReport] = []
    for (sym, tf), files in lake_files(lake, symbols).items():
        if tf not in REPAIRABLE_TIMEFRAMES:
            print(f"skipped {sym} {tf}: not a repairable timeframe {sorted(REPAIRABLE_TIMEFRAMES)}")
            continue
        tf_ns = timeframe_ns(tf)
        tables = {f: pq.read_table(f, columns=["ts_event_ns", "received_at_ns"]) for f in files}
        flags = {f: flag_rows(t, tf_ns) for f, t in tables.items()}
        # Closes already held by a good row: unflagged and recorded after (or just before) its close, the
        # rows query_bars_df ranks first. A row recorded while its kline was still open does not count, so
        # a REST copy colliding with it is rewritten and wins instead of being quarantined.
        clean_closes: set[int] = set()
        for f, t in tables.items():
            ev = t.column("ts_event_ns").to_pylist()
            rcv = t.column("received_at_ns").to_pylist()
            clean_closes.update(e for i, (e, r) in enumerate(zip(ev, rcv, strict=True))
                                if i not in flags[f] and r >= e - EARLY_RECEIPT_TOLERANCE_NS)
        for f, flagged in flags.items():
            if not flagged:
                continue
            written_ns = f.stat().st_mtime_ns
            # The API fetched up to "now" without dropping the forming kline, so each batch's newest
            # open-stamped row may be a partial bar; file mtimes are not trusted to say otherwise (copies
            # and restores reset them).
            newest_open = max((c for k, c in flagged.values() if k == "open_stamped"), default=None)
            rep = FileReport(path=f, symbol=sym, timeframe=tf, rows=tables[f].num_rows)
            for i, (kind, close) in sorted(flagged.items()):
                if kind == "open_stamped":
                    rep.open_stamped += 1
                else:
                    rep.misaligned += 1
                if close is None:
                    disp = "unrecognized"
                elif close in clean_closes:
                    disp = "duplicate"
                elif written_ns < close or (kind == "open_stamped" and close == newest_open):
                    disp = "unclosed"
                else:
                    disp = "rewrite"
                rep.decisions[i] = (close, disp)
                rep.disposition[disp] += 1
                if close is not None:
                    rep.first_close_ns = close if rep.first_close_ns is None else min(rep.first_close_ns, close)
                    rep.last_close_ns = close if rep.last_close_ns is None else max(rep.last_close_ns, close)
            reports.append(rep)
    return reports


def repaired_table(rep: FileReport) -> pa.Table:
    """The file's rows minus quarantined ones, with rewritten rows stamped at their close."""
    table = pq.read_table(rep.path)
    keep, ev = [], table.column("ts_event_ns").to_pylist()
    for i in range(table.num_rows):
        close, disp = rep.decisions.get(i, (None, "keep"))
        if disp == "keep":
            keep.append(i)
        elif disp == "rewrite":
            keep.append(i)
            ev[i] = close
    new_ev = pa.array(ev, type=table.schema.field("ts_event_ns").type)
    table = table.set_column(table.schema.get_field_index("ts_event_ns"), "ts_event_ns", new_ev)
    table = table.set_column(table.schema.get_field_index("ts_init_ns"), "ts_init_ns", new_ev)
    table = table.take(pa.array(keep, type=pa.int64()))
    return table.sort_by("ts_event_ns") if table.num_rows else table


def apply(rep: FileReport, lake: Path, quarantine: Path, stamp: str) -> Path | None:
    """Quarantine the original file, write the repaired rows back. Returns the new file, if any."""
    out = repaired_table(rep)
    rel = rep.path.relative_to(lake)
    target = quarantine / rel.parent / f"{rel.name}.{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)
    new_file = None
    if out.num_rows:
        new_file = rep.path.parent / f"{rep.path.stem}{REPAIRED_SUFFIX}.parquet"
        tmp = rep.path.parent / f".tmp.{uuid.uuid4().hex}.parquet"
        with open(tmp, "wb") as fh:
            pq.write_table(out, fh, compression="snappy")
            fh.flush()
            os.fsync(fh.fileno())
    os.replace(rep.path, target)  # moved, never deleted
    if new_file is not None:
        os.replace(tmp, new_file)
    manifest = {
        "original": str(rel), "quarantined_as": target.name, "repaired_file": new_file.name if new_file else None,
        "rows": rep.rows, "rows_written": out.num_rows, "open_stamped": rep.open_stamped,
        "misaligned": rep.misaligned, "disposition": dict(rep.disposition),
        "first_close": _fmt(rep.first_close_ns), "last_close": _fmt(rep.last_close_ns),
    }
    target.with_name(target.name + ".json").write_text(json.dumps(manifest, indent=2) + "\n")
    return new_file


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--lake", default="data/analytics_lake")
    p.add_argument("--quarantine", help="default: <lake parent>/catalog_quarantine/analytics_lake")
    p.add_argument("--symbol", action="append", help="limit to these symbols (repeatable)")
    p.add_argument("--apply", action="store_true", help="move affected files to quarantine and write repairs")
    args = p.parse_args()
    lake = Path(args.lake).resolve()
    quarantine = Path(args.quarantine).resolve() if args.quarantine else lake.parent / "catalog_quarantine" / lake.name
    reports = scan(lake, {s.upper() for s in args.symbol} if args.symbol else None)

    total: Counter = Counter()
    for rep in reports:
        total.update(rep.disposition)
        print(f"{rep.symbol:<12} {rep.timeframe:<4} {rep.path.relative_to(lake)}: {rep.rows} rows, "
              f"open-stamped {rep.open_stamped}, misaligned {rep.misaligned}, closes {_fmt(rep.first_close_ns)} -> "
              f"{_fmt(rep.last_close_ns)}; {dict(rep.disposition)}")
    print(f"{len(reports)} file(s) affected; rows: {dict(total)}")
    if not args.apply:
        print("dry run: nothing changed (pass --apply after backing up the lake)")
        return
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # never at the same time as lake compaction or a backfill. The scan above ran without the lock, so a job that
    # finished in between may have moved a file: apply() then fails on it and the run should be repeated.
    with lake_maintenance_lock_or_exit(lake):
        for rep in reports:
            new_file = apply(rep, lake, quarantine, stamp)
            print(f"quarantined {rep.path.relative_to(lake)} -> {quarantine}; "
                  f"wrote {new_file.name if new_file else 'nothing'}")


if __name__ == "__main__":
    main()
