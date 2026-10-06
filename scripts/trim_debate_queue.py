"""Trim the debate queue once to the cap the scanner now applies on every XADD (D1 / F-H2).

``stream:debate_queue`` grew without a cap (15,780 entries on 2026-10-06, all delivered: lag 0). The scanner,
the exit monitor and the API now pass ``MAXLEN ~ 10000``, which trims only as new entries arrive; this
script brings an existing stream down to the cap in one step and reports what it drops: how many entries,
their id and time range, trigger sources, and, per consumer group, how many of them were never delivered or
are still pending (unacknowledged). It refuses to drop undelivered or pending entries unless ``--force``.

Dry run by default: prints the plan and changes nothing. ``--apply`` runs ``XTRIM <stream> MINID <first kept
id>``, so exactly the reported entries go, even if new ones arrive in between. Take a Redis backup first
(``docs/runbooks``); a trimmed entry cannot be restored from Redis itself.

    REDIS_URL=redis://:<password>@127.0.0.1:6379/0 uv run python scripts/trim_debate_queue.py            # dry run
    REDIS_URL=redis://:<password>@127.0.0.1:6379/0 uv run python scripts/trim_debate_queue.py --apply
"""

from __future__ import annotations

import argparse
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

import redis

DEFAULT_STREAM = "stream:debate_queue"
DEFAULT_MAXLEN = 10_000


def _id_tuple(entry_id: str) -> tuple[int, int]:
    ms, _, seq = entry_id.partition("-")
    return int(ms), int(seq or 0)


def _id_time(entry_id: str) -> str:
    return datetime.fromtimestamp(_id_tuple(entry_id)[0] / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class TrimPlan:
    stream: str
    length: int
    maxlen: int
    drop: int = 0
    first_dropped: str | None = None
    last_dropped: str | None = None
    first_kept: str | None = None
    sources: Counter = field(default_factory=Counter)
    undelivered: dict[str, int] = field(default_factory=dict)  # group -> dropped entries it never read
    pending: dict[str, int] = field(default_factory=dict)  # group -> dropped entries still in its PEL

    @property
    def unsafe(self) -> bool:
        return any(self.undelivered.values()) or any(self.pending.values())


def plan_trim(r: redis.Redis, stream: str, maxlen: int) -> TrimPlan:
    length = r.xlen(stream)
    p = TrimPlan(stream=stream, length=length, maxlen=maxlen)
    if length <= maxlen:
        return p
    p.drop = length - maxlen
    entries = r.xrange(stream, count=p.drop + 1)  # the dropped entries plus the first one kept
    dropped, p.first_kept = entries[:p.drop], entries[p.drop][0]
    p.first_dropped, p.last_dropped = dropped[0][0], dropped[-1][0]
    p.sources = Counter(f.get("trigger_source", "?") for _, f in dropped)
    last = _id_tuple(p.last_dropped)
    for g in r.xinfo_groups(stream):
        name, delivered = g["name"], g["last-delivered-id"]
        p.undelivered[name] = sum(1 for i, _ in dropped if _id_tuple(i) > _id_tuple(delivered))
        pel = r.xpending_range(stream, name, min="-", max=p.last_dropped, count=p.drop)
        p.pending[name] = sum(1 for e in pel if _id_tuple(e["message_id"]) <= last)
    return p


def apply_trim(r: redis.Redis, p: TrimPlan) -> int:
    """Drop exactly the planned entries (everything before the first kept id). Returns how many went."""
    if not p.drop:
        return 0
    return r.xtrim(p.stream, minid=p.first_kept, approximate=False)


def report(p: TrimPlan) -> str:
    lines = [f"{p.stream}: {p.length} entries, cap {p.maxlen}"]
    if not p.drop:
        return lines[0] + ": nothing to trim"
    lines.append(f"  would drop the oldest {p.drop}: {p.first_dropped} ({_id_time(p.first_dropped)}) .. "
                 f"{p.last_dropped} ({_id_time(p.last_dropped)}); first kept {p.first_kept} ({_id_time(p.first_kept)})")
    lines.append("  trigger sources: " + ", ".join(f"{k}={v}" for k, v in p.sources.most_common()))
    for g in p.undelivered:
        lines.append(f"  group {g}: {p.undelivered[g]} never delivered, {p.pending[g]} pending (unacked)")
    if p.unsafe:
        lines.append("  UNSAFE: some dropped entries were never delivered or are unacknowledged (--force to drop anyway)")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stream", action="append", help=f"stream to trim (repeatable; default {DEFAULT_STREAM})")
    ap.add_argument("--maxlen", type=int, default=DEFAULT_MAXLEN)
    ap.add_argument("--apply", action="store_true", help="trim; without it, only report")
    ap.add_argument("--force", action="store_true", help="also drop undelivered or pending entries")
    args = ap.parse_args()
    r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
    for stream in args.stream or [DEFAULT_STREAM]:
        p = plan_trim(r, stream, args.maxlen)
        print(report(p))
        if not args.apply or not p.drop:
            continue
        if p.unsafe and not args.force:
            print("  skipped: unsafe without --force")
            continue
        print(f"  trimmed {apply_trim(r, p)} entries; length now {r.xlen(stream)}")
    if not args.apply:
        print("dry run: nothing changed (--apply to trim)")


if __name__ == "__main__":
    main()
