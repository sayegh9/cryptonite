"""B4 (F-H6): one-off cleanup of the debate queue left behind before recovery existed.

Finds, on ``stream:debate_queue`` and ``stream:exit_debate_queue`` (group ``debate_workers``):
- pending (PEL) entries idle longer than the reclaim window (10 min): copied to ``stream:debate_dlq``
  with reason ``MANUAL_RECOVERY`` and acked, in one MULTI each. They are not re-run: a setup that old
  is stale, and the DLQ keeps the full payload;
- consumers idle > 1 h with no pending entries left once those are dead-lettered: ``XGROUP DELCONSUMER``
  (a consumer holds no state once it has nothing pending, and is recreated by its next read);
and in Postgres:
- ``debate_runs`` RUNNING for more than 30 min: FAILED, ``failure_reason = 'REAPED_STALE'``
  (status only if migration 039 is not applied yet).

Dry run by default (prints what it would do). ``--apply`` writes, and prints the rollback for each
change: an ``XADD`` that re-enqueues each dead-lettered payload as a new entry, and the ``UPDATE``
that puts the reaped runs back to RUNNING. Re-running finds nothing left to do.

    docker compose -f infra/docker-compose.yml run --rm --no-deps test \\
        uv run python scripts/recover_debate_queue.py            # dry run
    ... uv run python scripts/recover_debate_queue.py --apply    # write
Pass the debate worker's own ``DATABASE_URL`` and ``REDIS_URL`` (``-e``, from its compose environment):
the queue lives on Redis DB 0 and the runs in the dev DB ``cryptonite``.
"""

import argparse
import asyncio
import os
import shlex
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import redis.asyncio as redis

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "debate_worker"))
from debate_queue import (  # noqa: E402
    CONSUMER_IDLE_DELETE_MS,
    DEL_IDLE_CONSUMER_LUA,
    GROUP,
    RECLAIM_IDLE_MS,
    STALE_RUN_S,
    STREAMS,
    dead_letter,
)

TOOL = "recover_debate_queue"


@dataclass
class Plan:
    pending: list[dict] = field(default_factory=list)  # stream, message_id, consumer, idle_ms, deliveries, fields
    runs: list[dict] = field(default_factory=list)  # id, instrument_id, trigger_source, created_at
    consumers: list[dict] = field(default_factory=list)  # stream, name, idle_ms
    has_reason_column: bool = True


async def build_plan(r, pool, *, pel_idle_ms: int = RECLAIM_IDLE_MS, run_age_s: int = STALE_RUN_S,
                     consumer_idle_ms: int = CONSUMER_IDLE_DELETE_MS) -> Plan:
    plan = Plan()
    for stream in STREAMS:
        if not await r.exists(stream):
            continue
        try:
            rows = await r.xpending_range(stream, GROUP, min="-", max="+", count=10_000)
        except redis.ResponseError:  # no such group
            continue
        for row in rows:
            if int(row["time_since_delivered"]) < pel_idle_ms:
                continue
            entries = await r.xrange(stream, row["message_id"], row["message_id"])
            plan.pending.append({"stream": stream, "message_id": row["message_id"], "consumer": row["consumer"],
                                 "idle_ms": int(row["time_since_delivered"]), "deliveries": int(row["times_delivered"]),
                                 "fields": entries[0][1] if entries else None})
        cleared = Counter(p["consumer"] for p in plan.pending if p["stream"] == stream)
        for c in await r.xinfo_consumers(stream, GROUP):  # deletable once the entries above are dead-lettered
            if int(c["pending"]) == cleared[c["name"]] and int(c["idle"]) > consumer_idle_ms:
                plan.consumers.append({"stream": stream, "name": c["name"], "idle_ms": int(c["idle"])})
    async with pool.acquire() as conn:
        plan.has_reason_column = bool(await conn.fetchval(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'debate_runs' AND column_name = 'failure_reason'"))
        plan.runs = [dict(r) for r in await conn.fetch(
            """SELECT id, instrument_id, trigger_source, created_at FROM debate_runs
               WHERE status = 'RUNNING' AND created_at < now() - make_interval(secs => $1) ORDER BY created_at""",
            run_age_s)]
    return plan


def print_plan(plan: Plan) -> None:
    print(f"Stuck PEL entries (idle > {RECLAIM_IDLE_MS // 60000} min): {len(plan.pending)}")
    for p in plan.pending:
        print(f"  {p['stream']} {p['message_id']} consumer={p['consumer']} idle={p['idle_ms'] // 1000}s "
              f"deliveries={p['deliveries']} payload={p['fields']}")
    print(f"Stale RUNNING debate_runs (> {STALE_RUN_S // 60} min): {len(plan.runs)}")
    for run in plan.runs:
        print(f"  {run['id']} {run['instrument_id']} {run['trigger_source']} created_at={run['created_at']}")
    print(f"Idle consumers (> {CONSUMER_IDLE_DELETE_MS // 3600000} h, 0 pending after the above): {len(plan.consumers)}")
    for c in plan.consumers:
        print(f"  {c['stream']} {c['name']} idle={c['idle_ms'] // 1000}s")
    if not plan.has_reason_column:
        print("Note: migration 039 is not applied; runs would be set FAILED without failure_reason.")


async def apply_plan(r, pool, plan: Plan) -> list[str]:
    """Apply the plan; return the rollback commands for what was changed."""
    rollback: list[str] = []
    for p in plan.pending:
        if p["fields"] is None:  # trimmed from the stream: nothing to keep, only the PEL slot to free
            await r.xack(p["stream"], GROUP, p["message_id"])
            continue
        await dead_letter(r, p["stream"], p["message_id"], p["fields"], "MANUAL_RECOVERY", p["deliveries"], TOOL,
                          expected_owner=p["consumer"])
        args = " ".join(f"{shlex.quote(str(k))} {shlex.quote(str(v))}" for k, v in p["fields"].items())
        rollback.append(f"XADD {p['stream']} * {args}")
    if plan.runs:
        ids = [run["id"] for run in plan.runs]
        reason = ", failure_reason = 'REAPED_STALE'" if plan.has_reason_column else ""
        async with pool.acquire() as conn:
            done = await conn.fetch(
                f"""UPDATE debate_runs SET status = 'FAILED', completed_at = now(){reason}
                    WHERE id = ANY($1::uuid[]) AND status = 'RUNNING' RETURNING id""", ids)
        if done:
            undo = "failure_reason = NULL, " if plan.has_reason_column else ""
            id_list = ", ".join(f"'{row['id']}'" for row in done)
            rollback.append(f"UPDATE debate_runs SET status = 'RUNNING', {undo}completed_at = NULL WHERE id IN ({id_list});")
    for c in plan.consumers:
        idle_ms = c.get("idle_ms", 0)
        await r.eval(DEL_IDLE_CONSUMER_LUA, 1, c["stream"], GROUP, c["name"], idle_ms)
    return rollback


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = parser.parse_args()
    r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    try:
        plan = await build_plan(r, pool)
        print_plan(plan)
        if not args.apply:
            print("\nDry run: nothing changed. Re-run with --apply to write.")
            return 0
        rollback = await apply_plan(r, pool, plan)
        print(f"\nApplied. Dead-lettered {len(plan.pending)} entries, reaped {len(plan.runs)} runs, "
              f"deleted {len(plan.consumers)} consumers (consumers need no rollback).")
        if rollback:
            print("Rollback:")
            for line in rollback:
                print(f"  {line}")
        return 0
    finally:
        await pool.close()
        await r.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
