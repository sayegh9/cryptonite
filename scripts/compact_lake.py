"""Compact each closed date partition of the Analytics Lake into one zstd Parquet file, by hand.

The ``lake_maintenance`` compose service already does this every night at 01:30 UTC, and prunes the quarantine
after 14 days (docs/runbooks/host-stability.md §6). Use this script for a dry run, for one symbol, or to catch
up at once. What compaction does and how it is checked: ``cryptonite_storage.lake_compaction``.

Run it against the LIVE lake, the one the stack mounts (``deploy-main/data``), from a checkout on ``main``.
It holds the lake maintenance lock (shared with the nightly service, the backfill scripts and the open-stamped
repair) and exits 75 while another of them runs:

    uv run python scripts/compact_lake.py --lake ../deploy-main/data/analytics_lake --dry-run
    uv run python scripts/compact_lake.py --lake ../deploy-main/data/analytics_lake --symbol BTCUSDT --timeframe 1m
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cryptonite_storage.lake_compaction import ROOT, run
from cryptonite_storage.lake_lock import lake_maintenance_lock_or_exit


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--lake", default=os.getenv("ANALYTICS_LAKE_PATH", "data/analytics_lake"))
    p.add_argument("--quarantine", help="default: <lake parent>/analytics_lake_quarantine")
    p.add_argument("--symbol", action="append", help="only this symbol (repeatable)")
    p.add_argument("--timeframe", action="append", help="only this timeframe (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="list what would be compacted; change nothing")
    args = p.parse_args(argv)
    lake = Path(args.lake).resolve()
    quarantine = Path(args.quarantine).resolve() if args.quarantine else lake.parent / "analytics_lake_quarantine"
    if not (lake / ROOT).is_dir():
        print(f"no lake at {lake / ROOT}", file=sys.stderr)
        return 2
    symbols = {s.upper() for s in args.symbol} if args.symbol else None
    timeframes = set(args.timeframe) if args.timeframe else None

    if args.dry_run:  # reads Parquet footers only
        results = run(lake, quarantine, symbols=symbols, timeframes=timeframes, dry_run=True)
    else:
        with lake_maintenance_lock_or_exit(lake):  # exits 75 while a backfill or the repair runs
            results = run(lake, quarantine, symbols=symbols, timeframes=timeframes)

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        files = len(r.partition.files)
        line = f"{r.status:<9} {r.partition.symbol:<12} {r.partition.timeframe:<4} {r.partition.day} {files} files {r.rows} rows"
        if r.compacted:
            line += f" -> {r.compacted.name}"
        print(line + (f": {r.detail}" if r.detail else ""))
    print(f"{len(results)} partition(s): {counts or 'nothing to compact'}"
          + ("; dry run, nothing changed" if args.dry_run else f"; originals in {quarantine}"))
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
