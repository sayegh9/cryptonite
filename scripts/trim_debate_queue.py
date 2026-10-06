"""Trim the debate queue once to the cap the scanner now applies on every XADD (D1 / F-H2).

``stream:debate_queue`` grew without a cap (15,780 entries on 2026-10-06, all delivered: lag 0). The scanner,
the exit monitor and the API now pass ``MAXLEN ~ 10000``, which trims only as new entries arrive; this
script brings an existing stream down to the cap in one step and reports what it drops: how many entries,
their id and time range, trigger sources, and, per consumer group, how many of them were never delivered or
are still pending (unacknowledged). It refuses to drop undelivered or pending entries unless ``--force``.

Dry run by default: prints the plan and changes nothing. ``--apply`` runs one Lua script that revalidates
the CURRENT consumer groups and PEL and XTRIMs, atomically: if a group was created or rewound, or new
entries became pending, after the plan was taken, the script refuses (exit without ``--force``) instead of
dropping work the earlier snapshot thought was delivered. ``--force`` makes that decision explicit in the
``apply_trim`` API too. The trim boundary stays exactly ``first_kept`` (``XTRIM ... MINID <first kept id>``),
so entries arriving in between are kept. Take a Redis backup first (``docs/runbooks``); a trimmed entry
cannot be restored from Redis itself.

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


class UnsafeTrim(RuntimeError):
    """The stream's groups/PEL changed after planning: the trim would drop undelivered or pending work."""


# One atomic operation: revalidate the CURRENT state and trim, or refuse. Between the old plan and this
# script a group could be created or rewound (undelivered entries appear) and entries could be re-read
# into a PEL (pending entries appear); the earlier snapshot cannot see either, so it must not be trusted.
# KEYS[1]=stream  ARGV[1]=planned first_kept id  ARGV[2]="1" only when the caller explicitly forces.
_TRIM_LUA = """
local stream, first_kept, force = KEYS[1], ARGV[1], ARGV[2]
if redis.call('XLEN', stream) == 0 then
  return {0, 0}
end
local dropped = redis.call('XRANGE', stream, '-', '(' .. first_kept)
if #dropped == 0 then
  return {0, 0}
end

local function id_tuple(id)
  local ms, seq = id:match('^(%d+)%-(%d+)$')
  return tonumber(ms) or 0, tonumber(seq) or 0
end
local function id_gt(a, b)
  local ams, aseq = id_tuple(a)
  local bms, bseq = id_tuple(b)
  if ams ~= bms then return ams > bms end
  return aseq > bseq
end

local unsafe = 0
for _, g in ipairs(redis.call('XINFO', 'GROUPS', stream)) do
  local gname, last_delivered, pending_total = nil, '0-0', 0
  for j = 1, #g, 2 do
    if g[j] == 'name' then gname = g[j + 1]
    elseif g[j] == 'last-delivered-id' then last_delivered = g[j + 1]
    elseif g[j] == 'pending' then pending_total = tonumber(g[j + 1]) or 0 end
  end
  for j = 1, #dropped do
    if id_gt(dropped[j][1], last_delivered) then unsafe = 1 break end
  end
  if unsafe == 0 and pending_total > 0 then
    local pel = redis.call('XPENDING', stream, gname, '-', '(' .. first_kept, pending_total)
    if #pel > 0 then unsafe = 1 end
  end
end
if unsafe == 1 and force ~= '1' then
  return {0, 1}
end
return {redis.call('XTRIM', stream, 'MINID', first_kept), 0}
"""


def apply_trim(r: redis.Redis, p: TrimPlan, *, force: bool = False) -> int:
    """Drop exactly the planned entries (everything before ``first_kept``), atomically revalidating the
    current groups and PEL first. Raises :class:`UnsafeTrim` — dropping nothing — when that revalidation
    finds undelivered or pending entries in the drop range and ``force`` was not given explicitly.
    Returns how many went."""
    if not p.drop:
        return 0
    trimmed, refused = r.eval(_TRIM_LUA, 1, p.stream, p.first_kept, "1" if force else "0")
    if refused:
        raise UnsafeTrim(
            f"{p.stream}: groups/PEL changed since planning — undelivered or pending entries would be "
            "dropped (rerun the plan, or force=True if that is intended)")
    return trimmed


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
    ap.add_argument("--force", action="store_true",
                    help="explicitly drop undelivered or pending entries (plan-time or found by the "
                         "atomic revalidation at apply time)")
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
        try:
            trimmed = apply_trim(r, p, force=args.force)
        except UnsafeTrim as e:
            print(f"  skipped: {e}")
            continue
        print(f"  trimmed {trimmed} entries; length now {r.xlen(stream)}")
    if not args.apply:
        print("dry run: nothing changed (--apply to trim)")


if __name__ == "__main__":
    main()
