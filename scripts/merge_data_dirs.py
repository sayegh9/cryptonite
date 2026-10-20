"""One-off: bring everything that only the old data folder holds into the live one (host-stability runbook §7).

Since 2026-10-19 the stack writes ``deploy-main/data``, while ``repo/data`` kept the only full Nautilus catalog
history (2026-03-18 to 2026-10-19 11:26 UTC), a few lake files written there in the last minutes before the
switch, and a few small state files. This links (or, across filesystems, copies) each file that exists only in
``--src`` to the same path under ``--dst``:

- never overwrites: a file already in ``--dst`` is left alone (a different size is reported as a conflict);
- refuses a Nautilus bar file whose time interval overlaps a bar file already in ``--dst`` for the same bar
  type (the catalog must not hold two copies of a bar), and reports it;
- skips dot-files (writers' temp files, the lake lock);
- holds the lake maintenance lock while it writes, so compaction never runs at the same time.

Dry run by default; ``--apply`` links. Re-running is safe: what is already there is skipped. Safe while the stack
runs: every file appears in one atomic link, and the lake and catalog only ever gain whole files.

    uv run python scripts/merge_data_dirs.py --src ../repo/data --dst ../deploy-main/data
    uv run python scripts/merge_data_dirs.py --src ../repo/data --dst ../deploy-main/data --apply
"""

from __future__ import annotations

import argparse
import errno
import os
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from cryptonite_storage.lake_lock import lake_maintenance_lock_or_exit

BARS = Path("nautilus_catalog") / "data" / "bars"


@dataclass
class Plan:
    link: list[Path] = field(default_factory=list)  # relative paths to bring over
    present: int = 0  # already in dst, same size
    conflicts: list[str] = field(default_factory=list)


def bar_interval(name: str) -> tuple[str, str] | None:
    """``<start>_<end>.parquet`` (fixed-width UTC stamps, so they compare as strings), else None."""
    stem = name.removesuffix(".parquet")
    parts = stem.split("_")
    return (parts[0], parts[1]) if len(parts) == 2 and parts[0] <= parts[1] else None


def _area(rel: Path) -> str:
    return "/".join(rel.parts[:3]) if rel.parts[:3] == BARS.parts else "/".join(rel.parts[:2])


def plan(src: Path, dst: Path) -> Plan:
    out = Plan()
    dst_bars: dict[str, list[tuple[str, str]]] = {}
    if (dst / BARS).is_dir():
        for d in (dst / BARS).iterdir():
            dst_bars[d.name] = [iv for f in d.glob("*.parquet") if (iv := bar_interval(f.name))]
    for f in sorted(src.rglob("*")):
        if not f.is_file() or f.is_symlink() or any(p.startswith(".") for p in f.relative_to(src).parts):
            continue
        rel = f.relative_to(src)
        target = dst / rel
        if target.exists():
            if target.stat().st_size == f.stat().st_size:
                out.present += 1
            else:
                out.conflicts.append(f"{rel}: already in dst with a different size; left alone")
            continue
        if rel.parts[:3] == BARS.parts and len(rel.parts) == 5:
            iv = bar_interval(rel.name)
            if iv is None:
                out.conflicts.append(f"{rel}: not a <start>_<end>.parquet bar file; left alone")
                continue
            clash = next((o for o in dst_bars.get(rel.parts[3], []) if iv[0] <= o[1] and o[0] <= iv[1]), None)
            if clash:
                out.conflicts.append(f"{rel}: overlaps {clash[0]}_{clash[1]} already in dst; left alone")
                continue
        out.link.append(rel)
    return out


def apply(src: Path, dst: Path, rels: list[Path]) -> int:
    done = 0
    for rel in rels:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src / rel, target)  # fails if the target exists: never overwrites
        except FileExistsError:
            continue
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            tmp = target.with_name(f".merge-tmp.{target.name}")
            shutil.copy2(src / rel, tmp)
            os.link(tmp, target)
            tmp.unlink()
        done += 1
    return done


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--src", required=True, help="the old data folder (repo/data)")
    p.add_argument("--dst", required=True, help="the live data folder (deploy-main/data)")
    p.add_argument("--apply", action="store_true", help="link the files; without it nothing changes")
    args = p.parse_args(argv)
    src, dst = Path(args.src).resolve(), Path(args.dst).resolve()
    if src == dst or not src.is_dir() or not dst.is_dir():
        print(f"--src and --dst must be two existing, different folders ({src}, {dst})", file=sys.stderr)
        return 2
    result = plan(src, dst)
    by_area = Counter(_area(rel) for rel in result.link)
    for area, n in sorted(by_area.items()):
        print(f"{'link' if args.apply else 'would link'} {n:>7} file(s) under {area}")
    print(f"{result.present} file(s) already in dst; {len(result.conflicts)} conflict(s)")
    for c in result.conflicts[:50]:
        print(f"  conflict: {c}")
    if not args.apply:
        print("dry run: nothing changed (pass --apply)")
        return 1 if result.conflicts else 0
    with lake_maintenance_lock_or_exit(dst / "analytics_lake"):
        done = apply(src, dst, result.link)
    print(f"linked {done} file(s) into {dst}")
    return 1 if result.conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
