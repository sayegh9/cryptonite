"""Nightly Analytics Lake maintenance: the ``lake_maintenance`` compose service (docs/runbooks/host-stability.md §6).

Every night at ``LAKE_MAINTENANCE_AT`` (default 01:30 UTC), holding the lake maintenance lock:

1. compact every closed date partition (``lake_compaction.run``: each one checked before it is swapped in);
2. prune quarantined originals older than ``LAKE_QUARANTINE_DAYS`` (default 14) whose rows are provably still in
   the lake (``lake_compaction.prune_quarantine``); anything unproven is kept.

A backfill or the open-stamped repair holding the lock is waited out: the run is retried every
``LAKE_LOCK_RETRY_S`` (default 1 h), up to ``LAKE_LOCK_RETRIES`` (default 6) times. Only problems are reported: a
partition that failed its check, a quarantine entry that could not be proven, an error, or a lock held all night
publish a CRITICAL message on ``channel:system_alerts``, which the notifier forwards to Telegram and ntfy.

State (last run, last success, what happened) is kept in ``<quarantine>/.lake-maintenance-state.json``; the
healthcheck (``--check``) fails when the last run failed or nothing has succeeded within 36 hours of the later
of the last success and the service start.

    python -m cryptonite_storage.lake_maintenance            # the service loop
    python -m cryptonite_storage.lake_maintenance --once     # one run now, then exit
    python -m cryptonite_storage.lake_maintenance --check    # healthcheck
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import duckdb

from cryptonite_storage import lake_compaction
from cryptonite_storage.lake_lock import LakeLockBusy, lake_maintenance_lock

logger = logging.getLogger("cryptonite.lake_maintenance")

ALERT_CHANNEL = "channel:system_alerts"  # the notifier forwards CRITICAL messages here to Telegram and ntfy
ALERT_KIND = "LAKE_MAINTENANCE"
STATE_NAME = ".lake-maintenance-state.json"
HEALTHY_WITHIN = timedelta(hours=36)
MAX_PROBLEM_LINES = 15

Publish = Callable[[str, str], None]  # (title, message)


@dataclass
class Config:
    lake: Path
    quarantine: Path
    at: dtime = dtime(1, 30)
    keep_days: int = 14
    lock_retry_s: float = 3600.0
    lock_retries: int = 6

    @classmethod
    def from_env(cls) -> Config:
        lake = Path(os.getenv("ANALYTICS_LAKE_PATH", "/app/data/analytics_lake"))
        quarantine = Path(os.getenv("LAKE_QUARANTINE_PATH", str(lake.parent / "analytics_lake_quarantine")))
        hh, mm = os.getenv("LAKE_MAINTENANCE_AT", "01:30").split(":")
        return cls(lake=lake, quarantine=quarantine, at=dtime(int(hh), int(mm)),
                   keep_days=int(os.getenv("LAKE_QUARANTINE_DAYS", "14")),
                   lock_retry_s=float(os.getenv("LAKE_LOCK_RETRY_S", "3600")),
                   lock_retries=int(os.getenv("LAKE_LOCK_RETRIES", "6")))

    @property
    def state_path(self) -> Path:
        return self.quarantine / STATE_NAME


def next_run(now: datetime, at: dtime) -> datetime:
    """The next ``at`` (UTC) strictly after ``now``."""
    now = now.astimezone(UTC)
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


# --- state ------------------------------------------------------------------------------------------------------


def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def write_state(path: Path, **updates) -> dict:
    state = {**read_state(path), **updates}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, path)
    return state


def healthy(state: dict, now: datetime) -> tuple[bool, str]:
    """(ok, why): not ok when the last run failed or nothing succeeded within 36 h of the later of the last success
    and the service start (a fresh service gets 36 h before its first night must have succeeded)."""
    if state.get("last_status") == "failed":
        return False, f"last run failed: {state.get('last_problem', '?')}"
    marks = [datetime.fromisoformat(state[k]) for k in ("last_success_at", "started_at") if state.get(k)]
    if not marks:
        return False, "no state yet"
    if now - max(marks) > HEALTHY_WITHIN:
        return False, f"no successful run since {max(marks).isoformat()}"
    return True, "ok"


# --- one night --------------------------------------------------------------------------------------------------


def run_once(cfg: Config, now: datetime) -> tuple[dict, list[str]]:
    """Compact, then prune, under the lake maintenance lock (raises ``LakeLockBusy`` when it is taken).
    Returns a summary and the problems worth an alert."""
    with lake_maintenance_lock(cfg.lake):
        compacted = lake_compaction.run(cfg.lake, cfg.quarantine, now=now)
        con = duckdb.connect(":memory:", config=lake_compaction.DUCKDB_CONFIG)
        try:
            pruned = lake_compaction.prune_quarantine(cfg.lake, cfg.quarantine, now=now, keep_days=cfg.keep_days,
                                                      con=con)
        finally:
            con.close()
    counts: dict[str, int] = {}
    for r in compacted:
        counts[r.status] = counts.get(r.status, 0) + 1
    summary = {
        "partitions": counts,
        "files_before": sum(len(r.partition.files) for r in compacted if r.status == "compacted"),
        "quarantine_pruned": sum(1 for p in pruned if p.status == "pruned"),
        "quarantine_files_deleted": sum(p.files for p in pruned),
        "quarantine_unverified": sum(1 for p in pruned if p.status == "unverified"),
    }
    problems = [f"compaction {r.status}: {r.partition.symbol} {r.partition.timeframe} {r.partition.day}: {r.detail}"
                for r in compacted if r.status == "failed"]
    problems += [f"quarantine kept (unproven): {p.manifest.relative_to(cfg.quarantine)}: {p.detail}"
                 for p in pruned if p.status == "unverified"]
    return summary, problems


def nightly(cfg: Config, *, now: Callable[[], datetime], publish: Publish,
            sleep: Callable[[float], bool]) -> str:
    """One scheduled night: ``run_once``, waiting out a held lock. Records the outcome and alerts on problems.
    ``sleep`` returns False when the service is stopping. Returns the status: ok | problems | failed | locked |
    stopped."""
    for attempt in range(cfg.lock_retries + 1):
        started = now()
        write_state(cfg.state_path, last_run_at=started.isoformat())
        try:
            summary, problems = run_once(cfg, started)
        except LakeLockBusy:
            logger.info("lake maintenance lock is held (a backfill or the repair is running); attempt %d of %d",
                        attempt + 1, cfg.lock_retries + 1)
            if attempt < cfg.lock_retries and not sleep(cfg.lock_retry_s):
                return "stopped"
            continue
        except Exception as e:  # anything else: record it, alert, and try again tomorrow
            logger.exception("lake maintenance run failed")
            msg = f"{type(e).__name__}: {e}"
            write_state(cfg.state_path, last_status="failed", last_problem=msg)
            publish("Lake maintenance failed", f"The nightly lake run raised {msg}. Nothing was half-done: "
                    "each partition is swapped only after its check. Logs: docker logs cryptonite_lake_maintenance")
            return "failed"
        # A partition that failed its check makes the service unhealthy until a run succeeds; quarantine kept
        # because it could not be proven is safe (nothing is deleted), so it only alerts.
        compaction_failed = summary["partitions"].get("failed", 0) > 0
        status = "failed" if compaction_failed else "problems" if problems else "ok"
        logger.info("lake maintenance %s: %s", status, json.dumps(summary))
        updates = {"last_status": status, "last_summary": summary, "last_problem": problems[0] if problems else ""}
        if not compaction_failed:
            updates["last_success_at"] = now().isoformat()
        write_state(cfg.state_path, **updates)
        if problems:
            shown = problems[:MAX_PROBLEM_LINES]
            more = len(problems) - len(shown)
            publish("Lake maintenance needs a look",
                    "\n".join(shown + ([f"... and {more} more"] if more else []))
                    + "\nThe lake was not changed where a check failed. Logs: docker logs cryptonite_lake_maintenance")
        return status
    msg = f"the lake maintenance lock stayed held for {cfg.lock_retries} retries ({cfg.lock_retry_s / 3600:g} h apart)"
    write_state(cfg.state_path, last_status="locked", last_problem=msg)
    publish("Lake maintenance skipped tonight", msg + "; is a backfill stuck? It runs again tomorrow night.")
    return "locked"


# --- service ----------------------------------------------------------------------------------------------------


def redis_publisher(url: str | None) -> Publish:
    """Publish CRITICAL alerts on ``channel:system_alerts``; never raises (an alert must not stop maintenance)."""
    def publish(title: str, message: str) -> None:
        logger.error("ALERT %s: %s", title, message)
        if not url:
            return
        try:
            import redis  # the service image has it; the package itself does not need it

            client = redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
            try:
                client.publish(ALERT_CHANNEL, json.dumps({"severity": "CRITICAL", "kind": ALERT_KIND,
                                                          "title": title, "message": f"{title}\n{message}"}))
            finally:
                client.close()
        except Exception as e:
            logger.error("could not publish the alert to Redis: %s", e)
    return publish


def serve(cfg: Config, publish: Publish, stop: threading.Event) -> None:
    write_state(cfg.state_path, started_at=datetime.now(UTC).isoformat())

    def sleep(seconds: float) -> bool:
        return not stop.wait(seconds)

    while not stop.is_set():
        due = next_run(datetime.now(UTC), cfg.at)
        logger.info("next lake maintenance at %s", due.isoformat())
        while not stop.is_set() and (left := (due - datetime.now(UTC)).total_seconds()) > 0:
            stop.wait(min(left, 300))
        if stop.is_set():
            break
        nightly(cfg, now=lambda: datetime.now(UTC), publish=publish, sleep=sleep)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = Config.from_env()
    if "--check" in argv:
        ok, why = healthy(read_state(cfg.state_path), datetime.now(UTC))
        print(why)
        return 0 if ok else 1
    publish = redis_publisher(os.getenv("REDIS_URL"))
    if "--once" in argv:
        status = nightly(cfg, now=lambda: datetime.now(UTC), publish=publish, sleep=lambda s: time.sleep(s) or True)
        return 0 if status == "ok" else 1
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    serve(cfg, publish, stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
