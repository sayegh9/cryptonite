"""One maintenance job at a time on the Analytics Lake.

``scripts/compact_lake.py`` moves a partition's files to quarantine once their compacted copy is in place,
``scripts/repair_lake_open_stamped_rows.py --apply`` moves and rewrites files, and the backfill scripts write
files into any past partition. They take this lock so none of them sees another one's half-finished work.
Live ingest does not take it: compaction leaves alone every file it did not list, and only touches partitions
that ingest has finished with.

The lock is an ``flock`` on ``<lake>/.lake-maintenance.lock``. It is released when the process exits, however
it exits, and works across containers that bind-mount the same lake.
"""

from __future__ import annotations

import fcntl
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = ".lake-maintenance.lock"
EXIT_LOCKED = 75  # EX_TEMPFAIL: try again later


class LakeLockBusy(RuntimeError):
    """Another lake maintenance job (compaction, backfill or repair) holds the lock."""


@contextmanager
def lake_maintenance_lock(lake: str | Path) -> Iterator[Path]:
    """Hold the lake maintenance lock for the ``with`` block. Raises ``LakeLockBusy`` at once if it is taken."""
    lake = Path(lake)
    lake.mkdir(parents=True, exist_ok=True)
    path = lake / LOCK_NAME
    # read-only is enough for flock, and lets a job running under another uid open a lock file it did not create
    fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o666)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LakeLockBusy(
                f"another lake maintenance job (compaction, backfill or repair) holds {path}; retry when it is done"
            ) from None
        yield path
    finally:
        os.close(fd)


@contextmanager
def lake_maintenance_lock_or_exit(lake: str | Path) -> Iterator[Path]:
    """For scripts: hold the lock for the ``with`` block, or say who holds it and exit with ``EXIT_LOCKED``."""
    try:
        with lake_maintenance_lock(lake) as path:
            yield path
    except LakeLockBusy as e:
        print(e, file=sys.stderr)
        raise SystemExit(EXIT_LOCKED) from None
