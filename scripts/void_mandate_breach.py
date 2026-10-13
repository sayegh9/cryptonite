"""Set aside a mandate BREACH that the system caused, not the AI (migration 045).

The BREACH row stays in the audit log; a BREACH_VOIDED event names it with your reason, and the autonomy gate stops
counting it. Use it only when the breach was a system fault (for example the 2026-10-13 sizing conflict), never to
excuse the AI overstepping its mandate.

Dry run (lists the breaches, changes nothing):
    uv run python scripts/void_mandate_breach.py --database-url "$DEV_DB_URL"
Void one (the event id from the dry run, repeated as --confirm):
    uv run python scripts/void_mandate_breach.py --database-url "$DEV_DB_URL" \\
        --apply --event-id 123 --confirm 123 --reason "orchestrator sizing conflict, fixed in PR #86"

Take a backup first (scripts/backup.sh db).
"""

import argparse
import asyncio
import sys

import asyncpg
from cryptonite_execution.mandate_store import BreachVoidError, void_breach

LIST_SQL = """
    SELECT b.id, m.name, b.mandate_id, b.reason, b.created_at, b.detail->>'command_id' AS command_id,
           c.payload->>'quantity' IS NULL AS system_sized,
           EXISTS (SELECT 1 FROM ai_autonomy_events v WHERE v.kind = 'BREACH_VOIDED' AND v.detail->>'voids' = b.id::text)
               AS voided
    FROM ai_autonomy_events b
    JOIN ai_mandates m ON m.id = b.mandate_id
    LEFT JOIN execution_commands c ON c.command_id::text = b.detail->>'command_id'
    WHERE b.kind = 'BREACH'
    ORDER BY b.id
"""


async def main(args: argparse.Namespace) -> int:
    conn = await asyncpg.connect(args.database_url)
    try:
        rows = await conn.fetch(LIST_SQL)
        print(f"{len(rows)} breach event(s):")
        for r in rows:
            sized = "system-sized" if r["system_sized"] else "quantity named by the order"
            state = "VOIDED" if r["voided"] else "counts"
            print(f"  #{r['id']}  {r['created_at']:%Y-%m-%d %H:%M}  {r['name']}  {r['reason']}  "
                  f"command {r['command_id']} ({sized})  [{state}]")
        if not args.apply:
            print("\nDry run: nothing changed. To void one: --apply --event-id N --confirm N --reason '...'")
            return 0
        if args.event_id is None or args.confirm != args.event_id:
            print("--apply needs --event-id N and the same N as --confirm", file=sys.stderr)
            return 2
        target = next((r for r in rows if r["id"] == args.event_id), None)
        if target is None:
            print(f"event {args.event_id} is not a breach", file=sys.stderr)
            return 2
        async with conn.transaction():
            vid = await void_breach(conn, target["mandate_id"], args.event_id, reason=args.reason,
                                    actor=f"operator:{args.actor}")
        print(f"Voided breach #{args.event_id} (event #{vid}). The breach row is kept; the gate no longer counts it.")
        return 0
    except BreachVoidError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    finally:
        await conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--database-url", required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--event-id", type=int)
    p.add_argument("--confirm", type=int)
    p.add_argument("--reason", default="")
    p.add_argument("--actor", default="admin")
    sys.exit(asyncio.run(main(p.parse_args())))
