"""C3 (F-M9, F-H2) and A1a M1/M2: remove test pollution from a Cryptonite database.

Dry-run by default: everything is read inside a READ ONLY transaction and the plan is printed with a
plan id. ``--apply --confirm <plan id>`` runs the same plan in ONE transaction, prints before/after
counts, re-checks its own invariants (exact counts, real accounts byte-for-byte untouched) and rolls
back if any fails. It refuses to run if the plan changed since the dry-run (different plan id).

Each change runs under the least-privileged role that can make it (``SET LOCAL ROLE`` inside the
owner's session, so the grants are enforced and it is still one transaction):

1. cryptonite_api: archive the active scan specs whose slug starts with ``test-``/``test_`` (A1a M1).
   ``is_archived = true`` as ``DELETE /api/v1/scanners/{id}`` does; nothing is deleted.
2. cryptonite_api: ``scalp-multi-tf`` gets version N+1 without ``1d`` in its timeframes (Q8, A1a M2),
   built exactly as ``PUT /api/v1/scanners/{id}`` builds one (immutable versions, recomputed hash).
   cryptonite_scanner: the new version inherits the old version's scanner checkpoints, so the scanner
   does not re-prime it over the whole lookback at its next start.
3. cryptonite_api: archive the XRP-only mandate (Q5). The autonomy state machine has no ARCHIVE
   trigger, so: PAUSE through ``transition()``/``apply_transition()`` exactly like
   ``POST /api/v1/ai/mandates/{id}/pause`` (PAUSED event, pending ADD_RISK approvals cancelled), then
   ``status = 'ARCHIVED'`` with an EDITED event naming the change. Refused if it holds open positions.
4. cryptonite_api: stop the SOL 1m / BTC 15m strategy instances (Q5) by enqueuing STOP commands, as
   ``POST /api/v1/paper/strategies/{id}/stop`` does; the orchestrator applies them (cancels their
   orders and sets the status). Instances have no ARCHIVED state; STOP is the terminal one. Refused
   if an instance holds open positions.
5. owner (cryptonite): delete the ``execution_kill_switch`` rows of test accounts. No other role may
   delete there (cryptonite_api: SELECT only; cryptonite_executor: no DELETE). Deleting a latched test
   row un-latches that test account only; real accounts are listed and asserted untouched.

Run from the host (the compose ``test`` service also defaults to the dev DB):

    uv run python scripts/cleanup_test_pollution.py --database-url "$DEV_DB_URL"   # the owner role on the cryptonite DB
    uv run python scripts/cleanup_test_pollution.py --database-url ... --apply --confirm <plan id>

Take a backup first (scripts/backup.sh). Redis is not touched; skipping the scanner backlog is
scripts/skip_scanner_backlog.py.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from cryptonite_execution.autonomy import AutonomyState, AutonomyTrigger, transition
from cryptonite_execution.mandate_store import apply_transition, load_mandate, record_event
from cryptonite_scanner.models import RuleGroup, UniverseConfig, compute_spec_hash
from cryptonite_scanner.presets import PRESET_SCALP_MULTI_TF
from cryptonite_schemas.mandate import MandateStatus, mandate_instance_id

ACTOR = "operator:c3-cleanup"
API_ROLE = "cryptonite_api"
SCANNER_ROLE = "cryptonite_scanner"

# Same rule as the 038 trigger: an active spec with this slug is refused in a protected database
TEST_SPEC_SLUG = re.compile(r"^test[-_]", re.IGNORECASE)

SCALP_SLUG = "scalp-multi-tf"
SCALP_DROP_TIMEFRAMES = ("1d",)  # Q8; D3 decides the rest of the automatic-trigger timeframes
SCALP_OLD_DESCRIPTION = "Captures 5m, 15m, 1H, 4H, and 1D bar close states for the Execution Strategist Engine."

XRP_MANDATE_INSTRUMENTS = ["XRPUSDT.BINANCE"]  # Q5: the mandate whose only instrument is XRP

# Q5: instance_id -> (instrument, timeframe) it must still have, or the script refuses
INSTANCES_TO_STOP = {
    "INST-513B9013": ("SOLUSDT.BINANCE", "1m"),
    "INST-7DF91538": ("BTCUSDT.BINANCE", "15m"),
}

# Accounts the services actually use: orchestrator ACCOUNT_ID, executor ACCOUNT_ID, the control-route and
# reconciler default. Every paper_accounts row is added at run time. These rows are never changed.
REAL_ACCOUNTS = frozenset({"PAPER-ACCOUNT-001", "DEMO-ACCOUNT-001", "BINANCE-SPOT-DEMO"})

# Kill-switch account ids the test suite generates (file that creates them -> pattern)
TEST_ACCOUNT_PATTERNS: dict[str, str] = {
    "ACC-RACE-<hex8> (test_m5a_hardening_edge_cases.py)": r"ACC-RACE-[0-9a-f]{8}",
    "ACC-CRASH-<hex8> (test_m5a_hardening_edge_cases.py)": r"ACC-CRASH-[0-9a-f]{8}",
    "ACC-MISMATCH|OTHER|ABORT|LOCK-PRE|LOCK-POST-<hex8> (test_m5a_hardening_edge_cases.py)":
        r"ACC-(MISMATCH|OTHER|ABORT|LOCK-PRE|LOCK-POST)-[0-9a-f]{8}",
    "ACC-<hex8> (test_authoritative_reconciliation.py, test_durable_reservations.py)": r"ACC-[0-9a-f]{8}",
    "ACC-DEMO|RESET|ADV-<HEX8> (test_demo_execution_service_pipeline.py)": r"ACC-(DEMO|RESET|ADV)-[0-9A-F]{8}",
    "ACC-E2E-<HEX8> (test_full_stack_paper_trading_e2e.py)": r"ACC-E2E-[0-9A-F]{8}",
    "TEST-ACC, TEST-ACC-PROTECTED (test_migration_006..., test_alembic_migrations_roundtrip.py)":
        r"TEST-ACC(-PROTECTED)?",
}
_TEST_ACCOUNT_RE = [(label, re.compile(rf"^{p}$")) for label, p in TEST_ACCOUNT_PATTERNS.items()]


def test_account_label(account_id: str) -> str | None:
    for label, rx in _TEST_ACCOUNT_RE:
        if rx.match(account_id):
            return label
    return None


@dataclass
class Plan:
    test_specs: list[dict[str, Any]] = field(default_factory=list)
    scalp: dict[str, Any] | None = None  # {spec_id, from_version, from_version_id, timeframes, new_timeframes, ...}
    mandates: list[dict[str, Any]] = field(default_factory=list)
    instances: list[dict[str, Any]] = field(default_factory=list)
    kill_switch_test_rows: list[dict[str, Any]] = field(default_factory=list)
    real_accounts: list[str] = field(default_factory=list)
    real_kill_switch_rows: list[dict[str, Any]] = field(default_factory=list)
    unclassified_kill_switch_rows: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def plan_id(self) -> str:
        """Fingerprint of what --apply would change (not of the read-only context)."""
        body = {
            "specs": sorted(str(s["id"]) for s in self.test_specs),
            "scalp": self.scalp and [str(self.scalp["from_version_id"]), self.scalp["new_timeframes"]],
            "mandates": sorted(str(m["id"]) for m in self.mandates),
            "instances": sorted(i["instance_id"] for i in self.instances if i["action"] == "STOP"),
            "kill_switch": sorted(
                [r["environment"], r["account_id"], r["latch_generation"], r["is_latched"]]
                for r in self.kill_switch_test_rows
            ),
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


async def collect(conn: asyncpg.Connection, *, lock: bool = False) -> Plan:
    """What --apply would change. ``lock`` (apply only) row-locks the specs and kill-switch rows it reads."""
    plan = Plan()
    for_update = " FOR UPDATE" if lock else ""

    # 1. active test specs
    for r in await conn.fetch(
        f"SELECT id, slug, created_at FROM scan_specs WHERE NOT is_archived ORDER BY created_at, slug{for_update}"
    ):
        if TEST_SPEC_SLUG.match(r["slug"]):
            plan.test_specs.append(dict(r))

    # 2. scalp-multi-tf without 1d
    row = await conn.fetchrow(
        """SELECT s.id, s.name, s.description, s.is_archived, v.id AS version_id, v.version_num, v.spec_hash,
                  v.universe, v.rules
           FROM scan_specs s JOIN scan_spec_versions v ON v.id = s.active_version_id
           WHERE s.slug = $1""",
        SCALP_SLUG,
    )
    if row is None:
        plan.notes.append(f"{SCALP_SLUG}: not found or no active version; nothing to change")
    elif row["is_archived"]:
        plan.notes.append(f"{SCALP_SLUG}: archived; nothing to change")
    else:
        universe = UniverseConfig(**_json(row["universe"])).model_dump()
        rules = [RuleGroup(**g).model_dump() for g in _json(row["rules"])]
        if compute_spec_hash(universe, {"rules": rules}) != row["spec_hash"]:
            plan.notes.append(f"{SCALP_SLUG} v{row['version_num']}: stored hash differs from the API's recomputation")
        dropped = [tf for tf in universe["timeframes"] if tf in SCALP_DROP_TIMEFRAMES]
        if not dropped:
            plan.notes.append(f"{SCALP_SLUG} v{row['version_num']}: timeframes {universe['timeframes']} have no 1d already")
        else:
            new_universe = {**universe, "timeframes": [tf for tf in universe["timeframes"] if tf not in dropped]}
            checkpoints = await conn.fetch(
                "SELECT instrument_id, source_timeframe, last_processed_watermark_ns, last_run_id "
                "FROM scanner_checkpoints WHERE spec_version_id = $1 ORDER BY instrument_id, source_timeframe",
                row["version_id"],
            )
            plan.scalp = {
                "spec_id": row["id"], "name": row["name"], "description": row["description"],
                "from_version": row["version_num"], "from_version_id": row["version_id"],
                "timeframes": universe["timeframes"], "new_timeframes": new_universe["timeframes"],
                "universe": new_universe, "rules": rules,
                "new_hash": compute_spec_hash(new_universe, {"rules": rules}),
                "checkpoints": [dict(c) for c in checkpoints],
            }

    # 3. the XRP-only mandate
    for r in await conn.fetch(
        "SELECT id, name, status, autonomy_level, instruments FROM ai_mandates "
        "WHERE status <> 'ARCHIVED' AND instruments = $1::text[] ORDER BY created_at",
        XRP_MANDATE_INSTRUMENTS,
    ):
        m = dict(r)
        m["instance_id"] = mandate_instance_id(r["id"])
        m["open_positions"] = await conn.fetchval(
            "SELECT count(*) FROM paper_positions WHERE instance_id = $1 AND status = 'OPEN' AND quantity <> 0",
            m["instance_id"],
        )
        m["pending_approvals"] = await conn.fetchval(
            "SELECT count(*) FROM ai_approvals WHERE mandate_id = $1 AND status = 'PENDING'", r["id"]
        )
        if m["open_positions"]:
            plan.refusals.append(f"mandate {r['name']} ({r['id']}) holds {m['open_positions']} open position(s)")
        plan.mandates.append(m)

    # 4. strategy instances
    for iid, (inst, tf) in INSTANCES_TO_STOP.items():
        r = await conn.fetchrow(
            "SELECT instance_id, account_id, instrument_id, timeframe, status FROM paper_strategy_instances "
            "WHERE instance_id = $1",
            iid,
        )
        if r is None:
            plan.notes.append(f"instance {iid}: not found; nothing to stop")
            continue
        i = dict(r)
        i["open_positions"] = await conn.fetchval(
            "SELECT count(*) FROM paper_positions WHERE instance_id = $1 AND status = 'OPEN' AND quantity <> 0", iid
        )
        i["stop_pending"] = await conn.fetchval(
            "SELECT count(*) FROM execution_commands WHERE target_instance_id = $1 AND command_type = 'STOP' "
            "AND status IN ('PENDING', 'CLAIMED', 'RUNNING', 'SENDING')",
            iid,
        )
        if (r["instrument_id"], r["timeframe"]) != (inst, tf):
            plan.refusals.append(f"instance {iid} is {r['instrument_id']} {r['timeframe']}, expected {inst} {tf}")
        if i["open_positions"]:
            plan.refusals.append(f"instance {iid} holds {i['open_positions']} open position(s)")
        if r["status"] not in ("RUNNING", "PAUSED"):
            i["action"] = f"none (status {r['status']})"
        elif i["stop_pending"]:
            i["action"] = "none (STOP already pending)"
        else:
            i["action"] = "STOP"
        plan.instances.append(i)

    # 5. kill-switch rows
    plan.real_accounts = sorted(
        REAL_ACCOUNTS | {r["account_id"] for r in await conn.fetch("SELECT account_id FROM paper_accounts")}
    )
    for r in await conn.fetch(
        "SELECT id, environment, account_id, is_latched, latch_generation, latched_at, latched_by, reason, "
        f"reset_at, reset_by, updated_at FROM execution_kill_switch ORDER BY account_id, environment{for_update}"
    ):
        row = dict(r)
        label = test_account_label(r["account_id"])
        if r["account_id"] in plan.real_accounts:
            if label:
                plan.refusals.append(f"real account {r['account_id']} matches test pattern {label}")
            plan.real_kill_switch_rows.append(row)
        elif label:
            row["pattern"] = label
            plan.kill_switch_test_rows.append(row)
        else:
            plan.unclassified_kill_switch_rows.append(row)
    return plan


async def counts(conn: asyncpg.Connection) -> dict[str, Any]:
    specs = await conn.fetch("SELECT slug FROM scan_specs WHERE NOT is_archived")
    ks = await conn.fetch("SELECT account_id, is_latched FROM execution_kill_switch")
    scalp_tf = await conn.fetchval(
        "SELECT v.universe -> 'timeframes' FROM scan_specs s JOIN scan_spec_versions v ON v.id = s.active_version_id "
        "WHERE s.slug = $1",
        SCALP_SLUG,
    )
    test_ks = [r for r in ks if test_account_label(r["account_id"])]
    return {
        "active_specs": len(specs),
        "active_test_specs": sum(1 for r in specs if TEST_SPEC_SLUG.match(r["slug"])),
        "scalp_multi_tf_timeframes": _json(scalp_tf),
        "xrp_mandates_not_archived": await conn.fetchval(
            "SELECT count(*) FROM ai_mandates WHERE status <> 'ARCHIVED' AND instruments = $1::text[]",
            XRP_MANDATE_INSTRUMENTS,
        ),
        "instances": {
            r["instance_id"]: r["status"]
            for r in await conn.fetch(
                "SELECT instance_id, status FROM paper_strategy_instances WHERE instance_id = ANY($1::text[])",
                list(INSTANCES_TO_STOP),
            )
        },
        "pending_stop_commands": await conn.fetchval(
            "SELECT count(*) FROM execution_commands WHERE command_type = 'STOP' AND status = 'PENDING' "
            "AND target_instance_id = ANY($1::text[])",
            list(INSTANCES_TO_STOP),
        ),
        "kill_switch_rows": len(ks),
        "kill_switch_test_rows": len(test_ks),
        "kill_switch_test_rows_latched": sum(1 for r in test_ks if r["is_latched"]),
    }


async def real_rows_snapshot(conn: asyncpg.Connection, accounts: list[str]) -> list[tuple]:
    rows = await conn.fetch(
        "SELECT row_to_json(k)::text FROM execution_kill_switch k WHERE account_id = ANY($1::text[]) ORDER BY id",
        accounts,
    )
    return [tuple(r) for r in rows]


def print_plan(plan: Plan) -> None:
    print("== Plan ==")
    print(f"1. archive {len(plan.test_specs)} active test spec(s) [role {API_ROLE}]")
    for s in plan.test_specs:
        print(f"     {s['slug']}  (created {s['created_at']:%Y-%m-%d})")
    if plan.scalp:
        s = plan.scalp
        print(f"2. {SCALP_SLUG}: v{s['from_version']} {s['timeframes']} -> v{s['from_version'] + 1} {s['new_timeframes']} "
              f"[role {API_ROLE}]; carry {len(s['checkpoints'])} scanner checkpoint(s) to v{s['from_version'] + 1} "
              f"[role {SCANNER_ROLE}]")
    else:
        print(f"2. {SCALP_SLUG}: no change")
    print(f"3. archive {len(plan.mandates)} XRP-only mandate(s) (PAUSE via the state machine, then ARCHIVED) [role {API_ROLE}]")
    for m in plan.mandates:
        print(f"     {m['name']} {m['id']} status={m['status']} L{m['autonomy_level']} "
              f"open_positions={m['open_positions']} pending_approvals={m['pending_approvals']}")
    print(f"4. stop strategy instances via STOP commands [role {API_ROLE}]")
    for i in plan.instances:
        print(f"     {i['instance_id']} {i['instrument_id']} {i['timeframe']} status={i['status']} "
              f"open_positions={i['open_positions']} -> {i['action']}")
    print(f"5. delete {len(plan.kill_switch_test_rows)} test-account execution_kill_switch row(s) "
          f"({sum(1 for r in plan.kill_switch_test_rows if r['is_latched'])} latched) [role owner: no other role may delete]")
    print("   test-account patterns:")
    for label in TEST_ACCOUNT_PATTERNS:
        n = sum(1 for r in plan.kill_switch_test_rows if r["pattern"] == label)
        print(f"     {label}: {n}")
    for r in plan.kill_switch_test_rows:
        print(f"     {r['environment']:5} {r['account_id']:20} latched={r['is_latched']!s:5} gen={r['latch_generation']}")
    print(f"   real accounts (never changed; asserted byte-identical): {', '.join(plan.real_accounts)}")
    for r in plan.real_kill_switch_rows:
        print(f"     {r['environment']:5} {r['account_id']:20} latched={r['is_latched']!s:5} gen={r['latch_generation']} "
              f"latched_by={r['latched_by']} reason={r['reason']!r}")
    if plan.unclassified_kill_switch_rows:
        print("   unclassified rows (left alone):")
        for r in plan.unclassified_kill_switch_rows:
            print(f"     {r['environment']:5} {r['account_id']:20} latched={r['is_latched']}")
    for n in plan.notes:
        print(f"note: {n}")
    for r in plan.refusals:
        print(f"REFUSED: {r}")
    print(f"plan id: {plan.plan_id()}")


def print_counts(title: str, c: dict[str, Any]) -> None:
    print(f"== {title} ==")
    for k, v in c.items():
        print(f"  {k}: {v}")


class CheckFailed(Exception):
    """An apply-time invariant failed; the transaction is rolled back."""


def _expect(ok: bool, message: str) -> None:
    # explicit, not assert: python -O strips asserts
    if not ok:
        raise CheckFailed(message)


async def apply(conn: asyncpg.Connection, plan: Plan) -> None:
    """Run the plan. The caller owns the transaction."""
    await conn.execute(f"SET LOCAL ROLE {API_ROLE}")

    # 1. archive test specs
    if plan.test_specs:
        res = await conn.execute(
            "UPDATE scan_specs SET is_archived = true, updated_at = now() WHERE id = ANY($1::uuid[]) AND NOT is_archived",
            [s["id"] for s in plan.test_specs],
        )
        _expect(res == f"UPDATE {len(plan.test_specs)}", f"archive test specs: {res}, expected {len(plan.test_specs)}")

    # 2. scalp-multi-tf version N+1 (as PUT /api/v1/scanners/{id})
    if plan.scalp:
        s = plan.scalp
        new_version_id = uuid.uuid4()
        next_ver = await conn.fetchval(
            "SELECT COALESCE(MAX(version_num), 0) + 1 FROM scan_spec_versions WHERE spec_id = $1", s["spec_id"]
        )
        await conn.execute(
            "INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)",
            new_version_id, s["spec_id"], next_ver, s["new_hash"], json.dumps(s["universe"]), json.dumps(s["rules"]),
        )
        # the seeded description names 1D; it follows the preset (PUT sets the description with every version)
        description = PRESET_SCALP_MULTI_TF.description if s["description"] == SCALP_OLD_DESCRIPTION else s["description"]
        res = await conn.execute(
            "UPDATE scan_specs SET active_version_id = $2, description = $4, updated_at = now() "
            "WHERE id = $1 AND active_version_id = $3",
            s["spec_id"], new_version_id, s["from_version_id"], description,
        )
        _expect(res == "UPDATE 1", f"{SCALP_SLUG}: active version moved since the plan ({res})")
        if s["checkpoints"]:
            await conn.execute(f"SET LOCAL ROLE {SCANNER_ROLE}")
            for c in s["checkpoints"]:
                await conn.execute(
                    "INSERT INTO scanner_checkpoints (spec_version_id, instrument_id, source_timeframe, "
                    "last_processed_watermark_ns, last_run_id) VALUES ($1, $2, $3, $4, $5)",
                    new_version_id, c["instrument_id"], c["source_timeframe"], c["last_processed_watermark_ns"],
                    c["last_run_id"],
                )
            await conn.execute(f"SET LOCAL ROLE {API_ROLE}")

    # 3. archive the XRP mandate: PAUSE (as POST .../pause), then ARCHIVED with an EDITED event
    for planned in plan.mandates:
        m = await load_mandate(conn, planned["id"], for_update=True)
        _expect(m is not None and m.status is not MandateStatus.ARCHIVED, f"mandate {planned['id']} gone or already archived")
        if m.status is MandateStatus.ACTIVE:
            t = transition(AutonomyState(m.autonomy_level, m.status, m.allow_perps), AutonomyTrigger.PAUSE,
                           reason="review-plan C3 / Q5: archive (no XRP feed)")
            await apply_transition(conn, m.id, t, actor=ACTOR)
            await conn.execute(
                """UPDATE ai_approvals SET status = 'CANCELLED', decided_via = 'system', decided_at = now()
                   WHERE mandate_id = $1 AND status = 'PENDING' AND action_class = 'ADD_RISK'""", m.id)
        before = await conn.fetchval("SELECT status FROM ai_mandates WHERE id = $1", m.id)
        await conn.execute("UPDATE ai_mandates SET status = 'ARCHIVED', updated_at = now() WHERE id = $1", m.id)
        await record_event(conn, m.id, "EDITED", reason="status", actor=ACTOR,
                           detail={"changes": {"status": [before, "ARCHIVED"]}, "risk_adding": False,
                                   "decision": "review-plan Q5"})

    # 4. STOP commands (as POST /api/v1/paper/strategies/{id}/stop)
    for i in plan.instances:
        if i["action"] != "STOP":
            continue
        await conn.execute(
            """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id,
                   requester_id, payload, status, attempt_count, environment, account_id, created_at)
               VALUES ($1, $2, 'STOP', $3, $4, $5::jsonb, 'PENDING', 0, 'PAPER', $6, now())""",
            uuid.uuid4(), f"C3-CLEANUP:STOP:{i['instance_id']}:{uuid.uuid4().hex[:8]}", i["instance_id"], ACTOR,
            json.dumps({"instance_id": i["instance_id"], "reason": "review-plan C3 / Q5"}), i["account_id"],
        )

    # 5. delete test kill-switch rows (owner only)
    await conn.execute("RESET ROLE")
    if plan.kill_switch_test_rows:
        res = await conn.execute(
            "DELETE FROM execution_kill_switch WHERE id = ANY($1::int[])", [r["id"] for r in plan.kill_switch_test_rows]
        )
        _expect(res == f"DELETE {len(plan.kill_switch_test_rows)}",
                f"kill-switch delete: {res}, expected {len(plan.kill_switch_test_rows)}")


def check_after(plan: Plan, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    problems = []
    if after["active_test_specs"] != 0:
        problems.append(f"{after['active_test_specs']} active test spec(s) remain")
    if after["active_specs"] != before["active_specs"] - len(plan.test_specs):
        problems.append("active spec count is not before - archived")
    if after["kill_switch_test_rows"] != 0:
        problems.append(f"{after['kill_switch_test_rows']} test kill-switch row(s) remain")
    if after["kill_switch_rows"] != before["kill_switch_rows"] - len(plan.kill_switch_test_rows):
        problems.append("kill-switch row count is not before - deleted")
    if plan.mandates and after["xrp_mandates_not_archived"] != 0:
        problems.append("XRP mandate not archived")
    if plan.scalp and any(tf in (after["scalp_multi_tf_timeframes"] or []) for tf in SCALP_DROP_TIMEFRAMES):
        problems.append("scalp-multi-tf still has 1d")
    if after["pending_stop_commands"] < sum(1 for i in plan.instances if i["action"] == "STOP"):
        problems.append("STOP commands missing")
    return problems


async def wait_for_stops(conn: asyncpg.Connection, seconds: float) -> None:
    ids = list(INSTANCES_TO_STOP)
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        pending = await conn.fetchval(
            "SELECT count(*) FROM execution_commands WHERE command_type = 'STOP' "
            "AND status IN ('PENDING', 'CLAIMED', 'RUNNING', 'SENDING') "
            "AND target_instance_id = ANY($1::text[])", ids)
        if not pending or asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(1)
    rows = await conn.fetch(
        "SELECT instance_id, status FROM paper_strategy_instances WHERE instance_id = ANY($1::text[]) ORDER BY 1", ids)
    print(f"== STOP commands after commit (waited up to {seconds:.0f}s): {pending} still pending ==")
    for r in rows:
        print(f"  {r['instance_id']}: {r['status']}")
    if pending:
        print("  the orchestrator applies them when it runs; re-check later")


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    p.add_argument("--apply", action="store_true", help="execute the plan in one transaction")
    p.add_argument("--confirm", help="the plan id printed by the dry-run (required with --apply)")
    p.add_argument("--wait-seconds", type=float, default=30.0, help="after --apply, wait this long for the STOPs")
    args = p.parse_args(argv)
    if not args.database_url:
        p.error("--database-url or DATABASE_URL is required")
    if args.apply and not args.confirm:
        p.error("--apply needs --confirm <plan id> from a dry-run")

    conn = await asyncpg.connect(args.database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    committed = False
    try:
        db, user = await conn.fetchrow("SELECT current_database(), current_user")
        print(f"database: {db}  user: {user}  mode: {'APPLY' if args.apply else 'dry-run (READ ONLY)'}")
        if not args.apply:
            async with conn.transaction(readonly=True):
                plan = await collect(conn)
                print_plan(plan)
                print_counts("Counts now", await counts(conn))
            return 1 if plan.refusals else 0

        for role in (API_ROLE, SCANNER_ROLE):
            if not await conn.fetchval("SELECT pg_has_role(current_user, $1, 'MEMBER')", role):
                print(f"REFUSED: {user} cannot SET ROLE {role}; run as the database owner")
                return 2
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            await conn.execute("SET LOCAL statement_timeout = '60s'")
            plan = await collect(conn, lock=True)
            print_plan(plan)
            if plan.refusals:
                raise SystemExit(2)
            if plan.plan_id() != args.confirm:
                print(f"REFUSED: plan id is {plan.plan_id()}, --confirm was {args.confirm}; re-run the dry-run")
                raise SystemExit(2)
            before = await counts(conn)
            real_before = await real_rows_snapshot(conn, plan.real_accounts)
            await apply(conn, plan)
            after = await counts(conn)
            problems = check_after(plan, before, after)
            if await real_rows_snapshot(conn, plan.real_accounts) != real_before:
                problems.append("a real account's kill-switch row changed")
            print_counts("Before", before)
            print_counts("After (in transaction)", after)
            if problems:
                for pr in problems:
                    print(f"CHECK FAILED: {pr}")
                raise SystemExit(3)  # leaving the block on an exception rolls the transaction back
        committed = True
        print("COMMITTED")
        if any(i["action"] == "STOP" for i in plan.instances):
            await wait_for_stops(conn, args.wait_seconds)
        return 0
    except SystemExit as e:
        print("ROLLED BACK")
        return int(e.code or 1)
    except CheckFailed as e:
        print(f"CHECK FAILED: {e}")
        print("ROLLED BACK")
        return 3
    except Exception as e:
        if not args.apply or committed:
            raise  # a dry-run changes nothing; after COMMITTED only the read-only wait can fail
        print(f"ERROR: {type(e).__name__}: {e}")
        print("ROLLED BACK")
        return 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
