"""Safe operator script to upgrade scanner preset specifications to event-based crossover versions (D2 / E1).

Usage:
    # Dry-run by default (inspects plan, makes zero database modifications):
    DATABASE_URL="postgresql://cryptonite:<pass>@localhost:5432/cryptonite" uv run python scripts/upgrade_scanner_presets.py

    # Apply upgrades to database with committed re-read verification:
    DATABASE_URL="postgresql://cryptonite:<pass>@localhost:5432/cryptonite" uv run python scripts/upgrade_scanner_presets.py --apply

    # Alternatively via HTTP API (requires operator auth token):
    uv run python scripts/upgrade_scanner_presets.py --api-url http://localhost:8000 --token <OPERATOR_BEARER_TOKEN> [--apply]

Guarantees:
    - Dry-run by default.
    - Serialized under pg_advisory_xact_lock(hashtext('scan_specs:active_limit')) and FOR UPDATE row locks.
    - Transitions unedited built-in presets matching PRESET_PREVIOUS_HASHES to new event crossover target hashes.
    - Preserves user-edited custom specs without modification (custom_preserved).
    - Honors MAX_ACTIVE_SPECS limit (10).
    - Re-reads and verifies committed active versions after apply; exits nonzero on any mismatch.
    - Reports commit only after transaction block exits.
    - Prints conditional SQL rollback instructions capturing exact pre-upgrade active_version_ids.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from uuid import uuid4

import asyncpg

# Adjust path to import packages and services
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "packages", "scanner")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "services", "api")))

from cryptonite_api.routes.scanners import (  # noqa: E402
    PRESET_PREVIOUS_HASHES,
)
from cryptonite_scanner.models import (  # noqa: E402
    MAX_ACTIVE_SPECS,
    RuleOperator,
    compute_spec_hash,
)
from cryptonite_scanner.presets import BUILTIN_PRESETS  # noqa: E402


async def run_direct_db(db_url: str, apply: bool) -> int:
    dry_run = not apply
    print(f"[*] Connecting to database: {db_url.split('@')[-1] if '@' in db_url else db_url}")
    print(f"[*] Mode: {'APPLY (live mutations)' if apply else 'DRY RUN (no changes)'}\n")

    pool = await asyncpg.create_pool(db_url)
    try:
        rollback_sqls: list[str] = []
        expected_upgrades: dict[str, str] = {}  # slug -> target_hash

        async with pool.acquire() as conn:
            async with conn.transaction():
                # Acquire advisory lock to serialize against concurrent creations/updates
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext('scan_specs:active_limit'));")

                active_count = await conn.fetchval(
                    "SELECT count(*) FROM scan_specs WHERE is_archived = false AND active_version_id IS NOT NULL;"
                )
                print(f"[*] Current active scan specs count: {active_count} (MAX_ACTIVE_SPECS: {MAX_ACTIVE_SPECS})")

                print("\n{:<25} {:<12} {:<15} {:<18} {:<18}".format("SLUG", "STATUS", "ACTION", "CURRENT_HASH", "TARGET_HASH"))
                print("-" * 90)

                for preset in BUILTIN_PRESETS:
                    univ_dict = preset.universe.model_dump()
                    rules_dict = [rg.model_dump() for rg in preset.rules]
                    target_hash = compute_spec_hash(univ_dict, {"rules": rules_dict})

                    spec_row = await conn.fetchrow(
                        """
                        SELECT s.id, s.slug, s.name, s.active_version_id, s.is_archived,
                               v.version_num, v.spec_hash
                        FROM scan_specs s
                        LEFT JOIN scan_spec_versions v ON s.active_version_id = v.id
                        WHERE s.slug = $1
                        FOR UPDATE OF s;
                        """,
                        preset.slug,
                    )

                    if not spec_row:
                        action = "WOULD_SEED" if dry_run else "SEEDED"
                        print(f"{preset.slug:<25} {'MISSING':<12} {action:<15} {'None':<18} {target_hash[:12]+'...':<18}")
                        if apply:
                            should_activate = active_count < MAX_ACTIVE_SPECS
                            spec_id = uuid4()
                            ver_id = uuid4()
                            await conn.execute(
                                """
                                INSERT INTO scan_specs (id, slug, name, description, active_version_id, is_archived)
                                VALUES ($1, $2, $3, $4, NULL, $5);
                                """,
                                spec_id, preset.slug, preset.name, preset.description, not should_activate,
                            )
                            await conn.execute(
                                """
                                INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
                                VALUES ($1, $2, 1, $3, $4::jsonb, $5::jsonb);
                                """,
                                ver_id, spec_id, target_hash, json.dumps(univ_dict), json.dumps(rules_dict),
                            )
                            if should_activate:
                                await conn.execute(
                                    "UPDATE scan_specs SET active_version_id = $1 WHERE id = $2;",
                                    ver_id, spec_id,
                                )
                                active_count += 1
                                expected_upgrades[preset.slug] = target_hash
                        continue

                    curr_hash = spec_row["spec_hash"]
                    curr_ver = spec_row["version_num"]
                    spec_id = spec_row["id"]
                    prior_hashes = PRESET_PREVIOUS_HASHES.get(preset.slug, set())

                    if curr_hash == target_hash:
                        print(f"{preset.slug:<25} {'CURRENT':<12} {'NOOP':<15} {str(curr_hash)[:12]+'...':<18} {target_hash[:12]+'...':<18}")
                        expected_upgrades[preset.slug] = target_hash
                        continue

                    if curr_hash in prior_hashes:
                        action = "WOULD_UPGRADE" if dry_run else "UPGRADED"
                        print(f"{preset.slug:<25} {'PRIOR_MATCH':<12} {action:<15} {str(curr_hash)[:12]+'...':<18} {target_hash[:12]+'...':<18}")
                        if apply:
                            existing_ver_id = await conn.fetchval(
                                "SELECT id FROM scan_spec_versions WHERE spec_id = $1 AND spec_hash = $2;",
                                spec_id, target_hash,
                            )
                            if not existing_ver_id:
                                existing_ver_id = uuid4()
                                max_ver = await conn.fetchval(
                                    "SELECT COALESCE(MAX(version_num), 0) FROM scan_spec_versions WHERE spec_id = $1;",
                                    spec_id,
                                )
                                next_ver = (max_ver or 0) + 1
                                await conn.execute(
                                    """
                                    INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
                                    VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb);
                                    """,
                                    existing_ver_id, spec_id, next_ver, target_hash,
                                    json.dumps(univ_dict), json.dumps(rules_dict),
                                )
                            update_res = await conn.execute(
                                """
                                UPDATE scan_specs
                                SET active_version_id = $1, description = $2, updated_at = now()
                                WHERE id = $3 AND active_version_id = $4;
                                """,
                                existing_ver_id, preset.description, spec_id, spec_row["active_version_id"],
                            )
                            if update_res == "UPDATE 0":
                                print(f"  [!] Concurrent edit detected on '{preset.slug}'; custom version preserved.")
                            else:
                                expected_upgrades[preset.slug] = target_hash
                                rollback_sqls.append(
                                    f"UPDATE scan_specs SET active_version_id = '{spec_row['active_version_id']}', updated_at = now() "
                                    f"WHERE id = '{spec_id}' AND active_version_id = '{existing_ver_id}'; "
                                    f"-- revert {preset.slug} to pre-upgrade version {curr_ver} ({str(curr_hash)[:12]}...)"
                                )
                    else:
                        print(f"{preset.slug:<25} {'CUSTOM':<12} {'PRESERVED':<15} {str(curr_hash)[:12]+'...':<18} {target_hash[:12]+'...':<18}")

            # Transaction has committed after block exit
            if apply:
                print("\n[*] Database transaction committed; verification pending...")
            else:
                print("\n[*] Dry run complete. No modifications were made to the database.")
                print("[*] To execute the upgrades, re-run with --apply flag.")
                return 0

            # -------------------------------------------------------------
            # Post-Apply Committed Re-Read Verification
            # -------------------------------------------------------------
            print("\n[*] Running committed re-read verification from database...")
            re_read_rows = await conn.fetch(
                """
                SELECT s.slug, s.name, s.is_archived, s.active_version_id,
                       v.version_num, v.spec_hash, v.rules
                FROM scan_specs s
                LEFT JOIN scan_spec_versions v ON s.active_version_id = v.id
                ORDER BY s.slug ASC;
                """
            )
            mismatches: list[str] = []

            for slug, exp_hash in expected_upgrades.items():
                row = next((r for r in re_read_rows if r["slug"] == slug), None)
                if not row or not row["active_version_id"]:
                    mismatches.append(f"{slug}: missing active version in committed database")
                    continue
                if row["spec_hash"] != exp_hash:
                    mismatches.append(
                        f"{slug}: committed hash mismatch (got {row['spec_hash'][:12]}..., expected {exp_hash[:12]}...)"
                    )
                rules_val = json.loads(row["rules"]) if isinstance(row["rules"], str) else row["rules"]
                preset_def = next((p for p in BUILTIN_PRESETS if p.slug == slug), None)
                preset_expects_crossover = (
                    preset_def is not None
                    and any(
                        cond.operator in [RuleOperator.CROSS_ABOVE, RuleOperator.CROSS_BELOW, RuleOperator.CROSSES_ABOVE, RuleOperator.CROSSES_BELOW]
                        for rg in preset_def.rules
                        for cond in rg.conditions
                    )
                )
                if preset_expects_crossover:
                    has_crossover = any(
                        cond.get("operator") in ["cross_above", "crosses_above", "cross_below", "crosses_below"]
                        and cond.get("lookback", 0) >= 1
                        for rg in rules_val
                        for cond in rg.get("conditions", [])
                    )
                    if not has_crossover:
                        mismatches.append(f"{slug}: active rules missing crossover condition with lookback >= 1")

            re_read_active_count = len([r for r in re_read_rows if r["active_version_id"] is not None and not r["is_archived"]])
            if re_read_active_count > MAX_ACTIVE_SPECS:
                mismatches.append(f"Active specs cap exceeded: {re_read_active_count} > {MAX_ACTIVE_SPECS}")

            if mismatches:
                print("\n[!] ERROR: Committed re-read verification FAILED:", file=sys.stderr)
                for m in mismatches:
                    print(f"    - {m}", file=sys.stderr)
                return 1

            print(f"[+] Committed re-read verification PASSED: {len(expected_upgrades)} presets verified with valid event crossover rules.")
            print(f"[+] Active specs count within limit: {re_read_active_count}/{MAX_ACTIVE_SPECS}")
            print("[+] Preset upgrades successfully committed and verified.")

            if rollback_sqls:
                print("\n[*] Rollback Criteria:")
                print("    If post-upgrade anomalies occur, verify backup (A3) and Reality Checker gate, then execute:")
                for sql in rollback_sqls:
                    print(f"    psql: {sql}")

    finally:
        await pool.close()

    return 0


async def run_http_api(api_url: str, token: str | None, apply: bool) -> int:
    import httpx

    dry_run = not apply
    url = f"{api_url.rstrip('/')}/api/v1/scanners/presets/upgrade?dry_run={'true' if dry_run else 'false'}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    print(f"[*] Calling API endpoint: {url}")
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers)
        if resp.status_code != 200:
            print(f"[!] API request failed ({resp.status_code}): {resp.text}", file=sys.stderr)
            return 1
        data = resp.json()
        print(f"[*] Response: dry_run={data.get('dry_run')}, active_count={data.get('active_count')}/{data.get('max_active_specs')}")
        expected_upgrades: dict[str, str] = {}
        for item in data.get("items", []):
            print(f"  - {item['slug']}: {item['action']} ({item['detail']})")
            if item["action"] in ["upgraded", "already_current"]:
                expected_upgrades[item["slug"]] = item["target_hash"]

        if apply:
            print("\n[*] Running committed re-read verification via GET /api/v1/scanners?include_archived=true...")
            list_resp = await client.get(f"{api_url.rstrip('/')}/api/v1/scanners?include_archived=true", headers=headers)
            if list_resp.status_code != 200:
                print(f"[!] Failed to re-read scanners ({list_resp.status_code}): {list_resp.text}", file=sys.stderr)
                return 1
            scanners = list_resp.json()
            mismatches: list[str] = []
            for slug, exp_hash in expected_upgrades.items():
                sc = next((s for s in scanners if s["slug"] == slug), None)
                if not sc or not sc.get("active_version"):
                    mismatches.append(f"{slug}: missing active version in GET /api/v1/scanners")
                    continue
                act_ver = sc["active_version"]
                if act_ver.get("spec_hash") != exp_hash:
                    mismatches.append(f"{slug}: hash mismatch (got {act_ver.get('spec_hash')[:12]}..., expected {exp_hash[:12]}...)")
                rules_val = act_ver.get("rules", [])
                preset_def = next((p for p in BUILTIN_PRESETS if p.slug == slug), None)
                preset_expects_crossover = (
                    preset_def is not None
                    and any(
                        cond.operator in [RuleOperator.CROSS_ABOVE, RuleOperator.CROSS_BELOW, RuleOperator.CROSSES_ABOVE, RuleOperator.CROSSES_BELOW]
                        for rg in preset_def.rules
                        for cond in rg.conditions
                    )
                )
                if preset_expects_crossover:
                    has_crossover = any(
                        cond.get("operator") in ["cross_above", "crosses_above", "cross_below", "crosses_below"]
                        and cond.get("lookback", 0) >= 1
                        for rg in rules_val
                        for cond in rg.get("conditions", [])
                    )
                    if not has_crossover:
                        mismatches.append(f"{slug}: active rules missing crossover condition with lookback >= 1")

            re_read_active_count = len([s for s in scanners if s.get("active_version") is not None and not s.get("is_archived")])
            if re_read_active_count > MAX_ACTIVE_SPECS:
                mismatches.append(f"Active specs cap exceeded: {re_read_active_count} > {MAX_ACTIVE_SPECS}")

            if mismatches:
                print("\n[!] ERROR: Committed re-read verification FAILED:", file=sys.stderr)
                for m in mismatches:
                    print(f"    - {m}", file=sys.stderr)
                return 1
            print("[+] Committed re-read verification PASSED via API.")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Upgrade scanner preset specifications to event crossover triggers.")
    parser.add_argument("--apply", action="store_true", help="Apply modifications to database (default is dry-run)")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="PostgreSQL connection URL")
    parser.add_argument("--api-url", default=os.getenv("API_URL"), help="HTTP API URL (e.g. http://localhost:8000)")
    parser.add_argument("--token", default=os.getenv("OPERATOR_TOKEN"), help="Bearer token for API auth")

    args = parser.parse_args()

    if args.api_url:
        exit_code = asyncio.run(run_http_api(args.api_url, args.token, args.apply))
    elif args.database_url:
        exit_code = asyncio.run(run_direct_db(args.database_url, args.apply))
    else:
        print("[!] Either --database-url (or DATABASE_URL env) or --api-url must be provided.", file=sys.stderr)
        sys.exit(1)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
