"""Resolve coin overlaps among mandates before migration 050 (auto-trade profiles P0.8, ADR 0011 D6).

Migration 050 locks each base asset (BTC, ETH, WLD ...) to one mandate whose status is ACTIVE, PAUSED or HALTED, and
refuses to run while a coin has two such owners. BTCUSDT and BTCUSDT-PERP are both BTC, so a spot and a perp mandate
on BTC overlap. Before 050 there is no STOPPED status and the API cannot archive, so this script makes the change by
hand, with the same base-asset rule (cryptonite_schemas.profile.base_asset).

Report (the default; exit 0 when no coin has two owners, 1 when one does):
    DATABASE_URL="postgresql://..." uv run python scripts/profile_overlap_resolve.py
Archive a mandate (refused while its instance holds an open position, a working order or a queued command):
    ... scripts/profile_overlap_resolve.py --archive "3 assets" --confirm "3 assets"
Replace a mandate's instruments (refused when it drops an instrument that is held, gives an empty list, or gives a
coin that another live mandate owns):
    ... scripts/profile_overlap_resolve.py --set-instruments "BTC-ETH spot L1" ETHUSDT --confirm "BTC-ETH spot L1"

Without a --confirm that matches the mandate name exactly, the script prints what it would do and changes nothing.
With a match it applies the change and writes one audit_events row in the same transaction. Exit 0 applied (or a
dry run that would succeed), 2 refused or invalid.

Works on schema 049. From 050 on (ai_mandates.markets exists) it only reports: the migration has already enforced
one owner per coin, and the profile API owns the lifecycle from then on.

Take a backup first (scripts/backup.sh db). Run it in a quiet window: the orchestrator may place an order between
the check and the commit (the mandate row is locked, which the lifecycle also takes, but a fill already in flight
is not visible here).
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass

import asyncpg
from cryptonite_schemas.mandate import canonical_instrument, mandate_instance_id
from cryptonite_schemas.profile import base_asset

OWNING_STATUSES = ("ACTIVE", "PAUSED", "HALTED")  # migration 050 LOCKING_STATUSES
ARCHIVED = "ARCHIVED"
# paper_orders statuses still able to fill (profile_lifecycle.WORKING_ORDER_STATUSES)
WORKING_ORDER_STATUSES = ("PENDING", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED")
# execution_commands statuses not yet finished (reconcile_phantom_manual_sells.ACTIVE_COMMANDS)
COMMANDS_IN_FLIGHT = ("PENDING", "CLAIMED", "RUNNING", "SENDING")
SOURCE = "scripts/profile_overlap_resolve.py"
REF = "auto-trade profiles P0.8 (scripts/profile_overlap_resolve.py)"

MANDATES_SQL = "SELECT id, name, status, instruments FROM ai_mandates ORDER BY name"
MANDATE_FOR_UPDATE_SQL = (
    "SELECT id, name, status, instruments FROM ai_mandates WHERE name = $1 FOR UPDATE"
)
HELD_SQL = """
    SELECT instrument_id, 'open position' AS why FROM paper_positions
    WHERE instance_id = $1 AND status = 'OPEN' AND quantity <> 0
    UNION ALL
    SELECT instrument_id, 'working order' FROM paper_orders
    WHERE instance_id = $1 AND status = ANY($2::varchar[])
"""
IN_FLIGHT_SQL = """SELECT count(*) FROM execution_commands
                   WHERE target_instance_id = $1 AND status = ANY($2::varchar[])"""
HAS_MARKETS_SQL = """SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
                     AND table_name = 'ai_mandates' AND column_name = 'markets')"""


@dataclass
class Mandate:
    id: object
    name: str
    status: str
    instruments: list[str]

    @property
    def instance(self) -> str:
        return mandate_instance_id(self.id)


@dataclass
class Change:
    kind: str  # "archive" or "instruments"
    name: str
    instruments: list[str] | None = None  # canonical, for "instruments"


def bases(instruments) -> set[str]:
    return {base_asset(i) for i in instruments}


def find_overlaps(mandates: list[Mandate]) -> dict[str, list[Mandate]]:
    """Base asset -> its owners, for every base with more than one ACTIVE, PAUSED or HALTED owner."""
    owners: dict[str, list[Mandate]] = {}
    for m in mandates:
        if m.status in OWNING_STATUSES:
            for base in sorted(bases(m.instruments)):
                owners.setdefault(base, []).append(m)
    return {base: ms for base, ms in sorted(owners.items()) if len(ms) > 1}


def parse_instruments(raw: str) -> list[str]:
    """'btcusdt, ETHUSDT-PERP' -> ['BTCUSDT.BINANCE', 'ETHUSDT-PERP.BINANCE']; duplicates dropped, order kept."""
    out: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            canonical = canonical_instrument(part)
            if canonical not in out:
                out.append(canonical)
    return out


async def load_mandates(conn) -> list[Mandate]:
    return [
        Mandate(r["id"], r["name"], r["status"], list(r["instruments"]))
        for r in await conn.fetch(MANDATES_SQL)
    ]


async def held(conn, m: Mandate) -> dict[str, set[str]]:
    """Instrument -> why this mandate's instance holds it ('open position', 'working order')."""
    out: dict[str, set[str]] = {}
    for r in await conn.fetch(HELD_SQL, m.instance, list(WORKING_ORDER_STATUSES)):
        out.setdefault(r["instrument_id"], set()).add(r["why"])
    return out


async def report(conn, mandates: list[Mandate]) -> int:
    overlaps = find_overlaps(mandates)
    if not overlaps:
        print(
            "No coin has more than one ACTIVE, PAUSED or HALTED owner. Migration 050's coin check passes."
        )
        return 0
    print(
        f"{len(overlaps)} coin(s) with more than one owner (migration 050 refuses to run while they exist):"
    )
    for base, owners in overlaps.items():
        print(f"\n{base}:")
        for m in owners:
            on_base = sorted(i for i in await held(conn, m) if base_asset(i) == base)
            state = f"HOLDS on {base}: {', '.join(on_base)}" if on_base else f"flat on {base}"
            print(f"  {m.name}  [{m.status}]  id {m.id}")
            print(f"      instruments: {', '.join(m.instruments)}")
            print(f"      instance {m.instance}: {state}")
    print("\nKeep one owner per coin. Runbook: docs/runbooks/o1-coin-overlaps.md")
    return 1


def refuse(problems: list[str]) -> int:
    print("refused:")
    for p in problems:
        print(f"  - {p}")
    return 2


async def change(conn, ch: Change, confirm: str | None, actor: str, reason: str) -> int:
    async with conn.transaction():
        row = await conn.fetchrow(MANDATE_FOR_UPDATE_SQL, ch.name)
        if row is None:
            print(f"no mandate named {ch.name!r}", file=sys.stderr)
            return 2
        target = Mandate(row["id"], row["name"], row["status"], list(row["instruments"]))
        others = [m for m in await load_mandates(conn) if m.id != target.id]
        problems: list[str] = []
        if target.status == ARCHIVED:
            problems.append(f"{target.name} is already ARCHIVED")
        if await conn.fetchval(IN_FLIGHT_SQL, target.instance, list(COMMANDS_IN_FLIGHT)):
            problems.append(f"{target.name} has commands in flight; wait for the queue to drain")
        holding = await held(conn, target)
        if ch.kind == "archive":
            for inst, why in sorted(holding.items()):
                problems.append(f"{inst} has {' and '.join(sorted(why))}")
            new = None
        else:
            new = ch.instruments or []
            if not new:
                problems.append(
                    "the instrument list is empty; a mandate needs at least one instrument"
                )
            for inst in target.instruments:
                if inst not in new and inst in holding:
                    problems.append(
                        f"{inst} is dropped but held ({' and '.join(sorted(holding[inst]))})"
                    )
            taken = {
                b: m.name
                for m in others
                if m.status in OWNING_STATUSES
                for b in bases(m.instruments)
            }
            for base in sorted(bases(new)):
                if base in taken:
                    problems.append(f"{base} is already owned by {taken[base]}")

        if problems:
            return refuse(problems)
        if ch.kind == "archive":
            print(f"Would archive {target.name} (status {target.status}); flat on every coin.")
        else:
            print(
                f"Would set {target.name} instruments:\n  {', '.join(target.instruments)}\n  -> {', '.join(new)}"
            )
        if confirm is None:
            print(f"\nDry run: nothing changed. To apply: --confirm {target.name!r}")
            return 0
        if confirm != target.name:
            print(f"--confirm {confirm!r} does not match {target.name!r}; nothing changed")
            return 2

        if ch.kind == "archive":
            await conn.execute(
                "UPDATE ai_mandates SET status = 'ARCHIVED', updated_at = now() WHERE id = $1",
                target.id,
            )
            event = "PROFILE_OVERLAP_ARCHIVED"
            moved = {"status_before": target.status, "status_after": ARCHIVED}
        else:
            await conn.execute(
                "UPDATE ai_mandates SET instruments = $2, updated_at = now() WHERE id = $1",
                target.id,
                new,
            )
            event = "PROFILE_OVERLAP_INSTRUMENTS_SET"
            moved = {"instruments_before": target.instruments, "instruments_after": new}
        details = {
            "mandate_id": str(target.id),
            "name": target.name,
            "instance": target.instance,
            "actor": f"operator:{actor}",
            "reason": reason,
            "ref": REF,
            **moved,
        }
        eid = await conn.fetchval(
            "INSERT INTO audit_events (event_type, source, details) VALUES ($1, $2, $3::jsonb) RETURNING id",
            event,
            SOURCE,
            json.dumps(details),
        )
        print(f"Applied {event} to {target.name}; audit_events #{eid}.")
        return 0


async def main(args: argparse.Namespace) -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        if args.archive is None and args.set_instruments is None:
            return await report(conn, await load_mandates(conn))
        if await conn.fetchval(HAS_MARKETS_SQL):
            print(
                "migration 050 or later is applied (ai_mandates.markets exists): owners cannot overlap, so this "
                "script only reports. Change profiles through the API.",
                file=sys.stderr,
            )
            return 2
        if args.archive is not None:
            ch = Change("archive", args.archive)
        else:
            name, raw = args.set_instruments
            ch = Change("instruments", name, parse_instruments(raw))
        return await change(conn, ch, args.confirm, args.actor, args.reason)
    except ValueError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    finally:
        await conn.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--archive", metavar="NAME", help="archive this mandate")
    group.add_argument(
        "--set-instruments",
        nargs=2,
        metavar=("NAME", "SYMBOLS"),
        help="replace the instruments, comma-separated (BTCUSDT,ETHUSDT)",
    )
    p.add_argument("--confirm", metavar="NAME", help="the mandate name again, to apply")
    p.add_argument("--reason", default="", help="why, stored in the audit row")
    p.add_argument("--actor", default="admin", help="recorded as operator:ACTOR")
    args = p.parse_args(argv)
    if args.confirm is not None and args.archive is None and args.set_instruments is None:
        p.error("--confirm applies only with --archive or --set-instruments")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
