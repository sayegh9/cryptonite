"""A1a / C3: skip the scanner's stale closed-bar backlog instead of draining it.

Dry-run by default: prints the consumer groups of the closed-bar stream (XINFO GROUPS: lag, pending,
last-delivered-id) and what --apply would do. ``--apply`` runs ``XGROUP SETID <stream> <group> $``,
so the scanner resumes at the next bar that closes. Decision (A1a): the backlog only yields stale
candidates; skipping it is the operator's call and was taken.

Before moving the group, the exact rollback command is printed (``XGROUP SETID <stream> <group>
<old last-delivered-id> ENTRIESREAD <old entries-read>``; it restores the old lag exactly).
Entries already delivered but not acknowledged (the group's PEL, "pending") are not touched: the
scanner re-claims them after 60 s idle (XAUTOCLAIM). ``--ack-pending`` also acknowledges them so
they are dropped too; that cannot be undone, so their IDs are printed first. Nothing is deleted
from the stream itself.

    uv run python scripts/skip_scanner_backlog.py --redis-url "$LIVE_REDIS_URL" [--apply]   # DB 0 of the live Redis
"""

import argparse
import os
import sys
from typing import Any

import redis

STREAM = "cryptonite:stream:closed_bar_committed"
GROUP = "scanner-worker"


def _s(v: Any) -> Any:
    return v.decode() if isinstance(v, bytes) else v


def group_info(r: "redis.Redis", stream: str, group: str) -> dict[str, Any] | None:
    for g in r.xinfo_groups(stream):
        g = {_s(k): _s(v) for k, v in g.items()}
        if g.get("name") == group:
            return g
    return None


def show(r: "redis.Redis", stream: str, group: str, title: str) -> dict[str, Any] | None:
    info = r.xinfo_stream(stream)
    print(f"== {title} ==")
    print(f"  stream {stream}: length={info.get('length')} last-generated-id={_s(info.get('last-generated-id'))}")
    g = group_info(r, stream, group)
    if g is None:
        print(f"  group {group}: not found")
    else:
        print(f"  group {group}: lag={g.get('lag')} pending={g.get('pending')} "
              f"last-delivered-id={g.get('last-delivered-id')} entries-read={g.get('entries-read')} "
              f"consumers={g.get('consumers')}")
    return g


def rollback_command(stream: str, group: str, g: dict[str, Any]) -> str:
    """The command that puts the group back where ``g`` (its XINFO GROUPS entry) found it."""
    cmd = f"XGROUP SETID {stream} {group} {g['last-delivered-id']}"
    if g.get("entries-read") is not None:
        cmd += f" ENTRIESREAD {g['entries-read']}"
    return cmd


def skip(r: "redis.Redis", stream: str, group: str, *, ack_pending: bool) -> list[str]:
    """XGROUP SETID <stream> <group> $; optionally XACK the PEL. Returns the acknowledged IDs."""
    r.xgroup_setid(stream, group, "$")
    acked: list[str] = []
    if ack_pending:
        while True:
            batch = [_s(p["message_id"]) for p in r.xpending_range(stream, group, min="-", max="+", count=1000)]
            if not batch:
                break
            # printed before the XACK: acknowledging cannot be undone
            print(f"  acknowledging (irreversible): {' '.join(batch)}")
            r.xack(stream, group, *batch)
            acked += batch
    return acked


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--redis-url", default=os.getenv("REDIS_URL"))
    p.add_argument("--stream", default=STREAM)
    p.add_argument("--group", default=GROUP)
    p.add_argument("--apply", action="store_true", help="run XGROUP SETID <stream> <group> $")
    p.add_argument("--ack-pending", action="store_true", help="with --apply, also XACK the delivered-but-unacked entries")
    args = p.parse_args(argv)
    if not args.redis_url:
        p.error("--redis-url or REDIS_URL is required")
    r = redis.Redis.from_url(args.redis_url)
    g = show(r, args.stream, args.group, "Before" if args.apply else "Now (dry-run)")
    if g is None:
        print("REFUSED: the consumer group does not exist; nothing to skip")
        return 2
    if not args.apply:
        print(f"--apply would run: XGROUP SETID {args.stream} {args.group} $"
              + (" and XACK the pending entries" if args.ack_pending else ""))
        print(f"rollback would be: {rollback_command(args.stream, args.group, g)}")
        return 0
    rollback = rollback_command(args.stream, args.group, g)
    print(f"ROLLBACK (restores the group exactly): {rollback}")
    if g.get("entries-read") is None:
        print("  note: entries-read was unknown, so the rollback cannot restore lag tracking")
    acked = skip(r, args.stream, args.group, ack_pending=args.ack_pending)
    if args.ack_pending:
        print(f"  acknowledged {len(acked)} pending entr{'y' if len(acked) == 1 else 'ies'}")
    show(r, args.stream, args.group, "After")
    return 0


if __name__ == "__main__":
    sys.exit(main())
