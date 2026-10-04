"""Rewrite Nautilus catalog bar series without duplicate bars (LE-1 repair).

Reads every bar of each series, keeps one bar per ``ts_event`` (the first written), and writes the
series back as one file per contiguous run. A file must never span a gap: the catalog treats a
file's [start, end] as covered, so a gap inside it would hide from gap detection and backfill.

Back up ``data/nautilus_catalog`` and stop ``cryptonite_ingest`` first, then:

    docker compose -f infra/docker-compose.yml run --rm --no-deps test \\
        uv run python scripts/repair_catalog_series.py BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL ...
"""

import argparse
import shutil
from pathlib import Path

from cryptonite_storage.nautilus_catalog import NautilusCatalogManager


def contiguous_runs(bars: list, step_ns: int) -> list[list]:
    runs: list[list] = []
    for bar in bars:
        if runs and bar.ts_event - runs[-1][-1].ts_event == step_ns:
            runs[-1].append(bar)
        else:
            runs.append([bar])
    return runs


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("bar_types", nargs="+")
    p.add_argument("--catalog", default="data/nautilus_catalog")
    args = p.parse_args()
    catalog = NautilusCatalogManager(args.catalog)
    for bar_type in args.bar_types:
        step_ns = (60 if "-1-MINUTE-" in bar_type else 3600) * 10**9
        bars = catalog.query_bars([bar_type])
        unique: dict[int, object] = {}
        for bar in bars:
            unique.setdefault(bar.ts_event, bar)
        ordered = [unique[t] for t in sorted(unique)]
        runs = contiguous_runs(ordered, step_ns)
        series_dir = Path(args.catalog) / "data" / "bars" / bar_type
        shutil.rmtree(series_dir)
        for run in runs:
            catalog.write_bars(run)
        print(f"{bar_type}: {len(bars)} bars -> {len(ordered)} unique in {len(runs)} contiguous files")


if __name__ == "__main__":
    main()
