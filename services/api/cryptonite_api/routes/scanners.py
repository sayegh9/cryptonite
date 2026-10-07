import json
from typing import Any
from uuid import UUID, uuid4

from cryptonite_api.routes.auth import AUTHED_READ, OPERATOR_WRITE
from cryptonite_scanner.models import (
    MAX_ACTIVE_SPECS,
    ScanJobRead,
    ScanSpecCreate,
    ScanSpecRead,
    ScanSpecVersionRead,
    compute_spec_hash,
)
from cryptonite_scanner.presets import BUILTIN_PRESETS
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/scanners", tags=["scanners"])


class PresetUpgradeItem(BaseModel):
    slug: str
    name: str
    current_version_num: int | None = None
    current_hash: str | None = None
    target_hash: str
    action: str
    detail: str


class PresetUpgradeResponse(BaseModel):
    dry_run: bool
    active_count: int
    max_active_specs: int = MAX_ACTIVE_SPECS
    items: list[PresetUpgradeItem]


# Known prior hashes for unedited built-in presets (before event crossover rewrite)
PRESET_PREVIOUS_HASHES: dict[str, set[str]] = {
    "rsi-oversold-reversal": {"6c06d8c94c121430b9dd4e09470bf1e4f6a2789369a90140d240f8d7d9fdd07a"},
    "trend-pullback": {"800d5d0c9370712e789981f21a86e8312601e8631aa71d5a25eb935b27824586"},
    "bollinger-breakout": {"4db974a03f6671097f2a56103cdeac0c31de1644d57a88746c9f6a47c6db32da"},
    "macd-momentum-shift": {"ce67b07159b95938ef6d93f1add67ef32654d572d5953d8ad528f1183120ab9a"},
}


async def ensure_presets_seeded(pool: Any) -> None:
    """Bootstrap built-in scanner presets on a fresh, unseeded database.

    - Returns immediately if any scan_specs exist (only fresh database bootstrapped).
    - Honors MAX_ACTIVE_SPECS limit if presets ever exceed cap.
    - Upgrades to new preset versions must be executed via POST /presets/upgrade.
    """
    if not pool:
        return

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('scan_specs:active_limit'));")
            count = await conn.fetchval("SELECT count(*) FROM scan_specs;")
            if count and count > 0:
                return

            active_count = 0
            for preset in BUILTIN_PRESETS:
                univ_dict = preset.universe.model_dump()
                rules_dict = [rg.model_dump() for rg in preset.rules]
                target_hash = compute_spec_hash(univ_dict, {"rules": rules_dict})

                spec_id = uuid4()
                version_id = uuid4()
                should_activate = active_count < MAX_ACTIVE_SPECS
                await conn.execute(
                    """
                    INSERT INTO scan_specs (id, slug, name, description, active_version_id, is_archived)
                    VALUES ($1, $2, $3, $4, NULL, $5);
                    """,
                    spec_id,
                    preset.slug,
                    preset.name,
                    preset.description,
                    not should_activate,
                )
                await conn.execute(
                    """
                    INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
                    VALUES ($1, $2, 1, $3, $4::jsonb, $5::jsonb);
                    """,
                    version_id,
                    spec_id,
                    target_hash,
                    json.dumps(univ_dict),
                    json.dumps(rules_dict),
                )
                if should_activate:
                    await conn.execute(
                        "UPDATE scan_specs SET active_version_id = $1 WHERE id = $2;",
                        version_id,
                        spec_id,
                    )
                    active_count += 1


@router.get("", dependencies=AUTHED_READ)
async def list_scanners(request: Request, include_archived: bool = False) -> list[ScanSpecRead]:
    """List scanner specs with their current active version."""
    pool = request.app.state.pool

    where_clause = "" if include_archived else "WHERE s.is_archived = false"
    rows = await pool.fetch(
        f"""
        SELECT s.id, s.slug, s.name, s.description, s.active_version_id, s.is_archived,
               s.created_at, s.updated_at,
               v.version_num, v.spec_hash, v.universe, v.rules, v.created_at as version_created_at
        FROM scan_specs s
        LEFT JOIN scan_spec_versions v ON s.active_version_id = v.id
        {where_clause}
        ORDER BY s.created_at ASC;
        """
    )

    result: list[ScanSpecRead] = []
    for r in rows:
        active_ver = None
        if r["active_version_id"] is not None and r["version_num"] is not None:
            active_ver = ScanSpecVersionRead(
                id=r["active_version_id"],
                spec_id=r["id"],
                version_num=r["version_num"],
                spec_hash=r["spec_hash"],
                universe=json.loads(r["universe"]) if isinstance(r["universe"], str) else r["universe"],
                rules=json.loads(r["rules"]) if isinstance(r["rules"], str) else r["rules"],
                created_at=r["version_created_at"],
            )

        result.append(
            ScanSpecRead(
                id=r["id"],
                slug=r["slug"],
                name=r["name"],
                description=r["description"],
                active_version_id=r["active_version_id"],
                active_version=active_ver,
                is_archived=r["is_archived"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
        )

    return result


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=OPERATOR_WRITE)
async def create_scanner(spec_in: ScanSpecCreate, request: Request) -> ScanSpecRead:
    """Create a new scanner specification and its immutable version 1."""
    pool = request.app.state.pool
    spec_id = uuid4()
    version_id = uuid4()

    univ_dict = spec_in.universe.model_dump()
    rules_dict = [rg.model_dump() for rg in spec_in.rules]
    spec_hash = compute_spec_hash(univ_dict, {"rules": rules_dict})

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Serialise concurrent spec creations to avoid racing MAX_ACTIVE_SPECS limit
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('scan_specs:active_limit'));")

            # Check unique slug
            existing = await conn.fetchval("SELECT id FROM scan_specs WHERE slug = $1;", spec_in.slug)
            if existing:
                raise HTTPException(status_code=400, detail=f"Slug '{spec_in.slug}' already exists")

            active_count = await conn.fetchval(
                "SELECT count(*) FROM scan_specs WHERE is_archived = false AND active_version_id IS NOT NULL;"
            )
            if active_count >= MAX_ACTIVE_SPECS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot create active spec: maximum active specs limit ({MAX_ACTIVE_SPECS}) reached. Archive an existing spec first.",
                )

            await conn.execute(
                """
                INSERT INTO scan_specs (id, slug, name, description, active_version_id, is_archived)
                VALUES ($1, $2, $3, $4, NULL, false);
                """,
                spec_id,
                spec_in.slug,
                spec_in.name,
                spec_in.description,
            )
            await conn.execute(
                """
                INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
                VALUES ($1, $2, 1, $3, $4::jsonb, $5::jsonb);
                """,
                version_id,
                spec_id,
                spec_hash,
                json.dumps(univ_dict),
                json.dumps(rules_dict),
            )
            await conn.execute(
                "UPDATE scan_specs SET active_version_id = $1, updated_at = now() WHERE id = $2;",
                version_id,
                spec_id,
            )

    row = await pool.fetchrow(
        """
        SELECT s.id, s.slug, s.name, s.description, s.active_version_id, s.is_archived,
               s.created_at, s.updated_at,
               v.version_num, v.spec_hash, v.universe, v.rules, v.created_at as version_created_at
        FROM scan_specs s
        JOIN scan_spec_versions v ON s.active_version_id = v.id
        WHERE s.id = $1;
        """,
        spec_id,
    )

    active_ver = ScanSpecVersionRead(
        id=row["active_version_id"],
        spec_id=row["id"],
        version_num=row["version_num"],
        spec_hash=row["spec_hash"],
        universe=json.loads(row["universe"]) if isinstance(row["universe"], str) else row["universe"],
        rules=json.loads(row["rules"]) if isinstance(row["rules"], str) else row["rules"],
        created_at=row["version_created_at"],
    )

    return ScanSpecRead(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        description=row["description"],
        active_version_id=row["active_version_id"],
        active_version=active_ver,
        is_archived=row["is_archived"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.put("/{spec_id}", dependencies=OPERATOR_WRITE)
async def update_scanner(spec_id: UUID, spec_in: ScanSpecCreate, request: Request) -> ScanSpecRead:
    """Append a new immutable version to the spec and point active_version_id to it."""
    pool = request.app.state.pool
    new_version_id = uuid4()

    univ_dict = spec_in.universe.model_dump()
    rules_dict = [rg.model_dump() for rg in spec_in.rules]
    spec_hash = compute_spec_hash(univ_dict, {"rules": rules_dict})

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Serialise against concurrent preset upgrades/activations/creations
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('scan_specs:active_limit'));")
            spec = await conn.fetchrow(
                "SELECT id, slug, active_version_id FROM scan_specs WHERE id = $1 AND is_archived = false FOR UPDATE;",
                spec_id,
            )
            if not spec:
                raise HTTPException(status_code=404, detail="Scan spec not found")

            max_ver = await conn.fetchval(
                "SELECT COALESCE(MAX(version_num), 0) FROM scan_spec_versions WHERE spec_id = $1;",
                spec_id,
            )
            next_ver_num = max_ver + 1

            await conn.execute(
                """
                INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb);
                """,
                new_version_id,
                spec_id,
                next_ver_num,
                spec_hash,
                json.dumps(univ_dict),
                json.dumps(rules_dict),
            )

            await conn.execute(
                """
                UPDATE scan_specs
                SET name = $2, description = $3, active_version_id = $4, updated_at = now()
                WHERE id = $1;
                """,
                spec_id,
                spec_in.name,
                spec_in.description,
                new_version_id,
            )

    row = await pool.fetchrow(
        """
        SELECT s.id, s.slug, s.name, s.description, s.active_version_id, s.is_archived,
               s.created_at, s.updated_at,
               v.version_num, v.spec_hash, v.universe, v.rules, v.created_at as version_created_at
        FROM scan_specs s
        JOIN scan_spec_versions v ON s.active_version_id = v.id
        WHERE s.id = $1;
        """,
        spec_id,
    )

    active_ver = ScanSpecVersionRead(
        id=row["active_version_id"],
        spec_id=row["id"],
        version_num=row["version_num"],
        spec_hash=row["spec_hash"],
        universe=json.loads(row["universe"]) if isinstance(row["universe"], str) else row["universe"],
        rules=json.loads(row["rules"]) if isinstance(row["rules"], str) else row["rules"],
        created_at=row["version_created_at"],
    )

    return ScanSpecRead(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        description=row["description"],
        active_version_id=row["active_version_id"],
        active_version=active_ver,
        is_archived=row["is_archived"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.delete("/{spec_id}", dependencies=OPERATOR_WRITE)
async def archive_scanner(spec_id: UUID, request: Request) -> dict[str, str]:
    """Archive a scanner specification without deleting research history."""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('scan_specs:active_limit'));")
            res = await conn.execute(
                "UPDATE scan_specs SET is_archived = true, updated_at = now() WHERE id = $1;",
                spec_id,
            )
            if "0" in res:
                raise HTTPException(status_code=404, detail="Scan spec not found")
            return {"status": "archived", "id": str(spec_id)}


@router.post("/{spec_id}/run", status_code=status.HTTP_202_ACCEPTED, dependencies=OPERATOR_WRITE)
async def enqueue_scan_run(
    spec_id: UUID, request: Request, response: Response
) -> dict[str, Any]:
    """Enqueue a manual scan run job asynchronously (HTTP 202 Accepted)."""
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "SELECT active_version_id FROM scan_specs WHERE id = $1 AND is_archived = false;",
        spec_id,
    )
    if not row or not row["active_version_id"]:
        raise HTTPException(status_code=404, detail="Active scan spec version not found")

    job_id = uuid4()
    req_id = f"req-{uuid4().hex[:12]}"
    await pool.execute(
        """
        INSERT INTO scan_jobs (id, request_id, spec_version_id, status)
        VALUES ($1, $2, $3, 'PENDING');
        """,
        job_id,
        req_id,
        row["active_version_id"],
    )

    response.headers["Location"] = f"/api/v1/scanners/jobs/{job_id}"
    return {
        "status": "ACCEPTED",
        "id": str(job_id),
        "job_id": str(job_id),
        "request_id": req_id,
        "spec_version_id": str(row["active_version_id"]),
    }


@router.get("/jobs/{job_id}", dependencies=AUTHED_READ)
async def get_scan_job(job_id: UUID, request: Request) -> ScanJobRead:
    """Check the status of an enqueued scan job."""
    pool = request.app.state.pool
    row = await pool.fetchrow("SELECT * FROM scan_jobs WHERE id = $1;", job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Scan job not found")

    return ScanJobRead(
        id=row["id"],
        request_id=row["request_id"],
        spec_version_id=row["spec_version_id"],
        status=row["status"],
        requested_watermark_ns=row["requested_watermark_ns"],
        requested_range_start_ns=row["requested_range_start_ns"],
        requested_range_end_ns=row["requested_range_end_ns"],
        scan_run_id=row["scan_run_id"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


@router.post("/presets/upgrade", dependencies=OPERATOR_WRITE)
async def upgrade_presets(
    request: Request,
    dry_run: bool = False,
) -> PresetUpgradeResponse:
    """Explicit operator-only idempotent upgrade for built-in scanner presets.

    - Supports dry_run mode (E1 sequencing) to preview changes without mutation.
    - Transitions presets whose active version matches a known prior built-in hash.
    - Preserves user-edited / custom specs untouched.
    - Honors MAX_ACTIVE_SPECS limit and never unarchives specs accidentally.
    """
    pool = request.app.state.pool
    items: list[PresetUpgradeItem] = []

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('scan_specs:active_limit'));")
            active_count = await conn.fetchval(
                "SELECT count(*) FROM scan_specs WHERE is_archived = false AND active_version_id IS NOT NULL;"
            )

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
                    should_activate = active_count < MAX_ACTIVE_SPECS
                    action = "would_seed" if dry_run else "missing_seeded"
                    detail = (
                        f"Preset '{preset.slug}' not present in DB; {'would seed' if dry_run else 'seeded'} "
                        f"as {'active' if should_activate else 'archived (cap reached)'}."
                    )
                    if not dry_run:
                        spec_id = uuid4()
                        version_id = uuid4()
                        await conn.execute(
                            """
                            INSERT INTO scan_specs (id, slug, name, description, active_version_id, is_archived)
                            VALUES ($1, $2, $3, $4, NULL, $5);
                            """,
                            spec_id,
                            preset.slug,
                            preset.name,
                            preset.description,
                            not should_activate,
                        )
                        await conn.execute(
                            """
                            INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
                            VALUES ($1, $2, 1, $3, $4::jsonb, $5::jsonb);
                            """,
                            version_id,
                            spec_id,
                            target_hash,
                            json.dumps(univ_dict),
                            json.dumps(rules_dict),
                        )
                        if should_activate:
                            await conn.execute(
                                "UPDATE scan_specs SET active_version_id = $1 WHERE id = $2;",
                                version_id,
                                spec_id,
                            )
                            active_count += 1
                    items.append(
                        PresetUpgradeItem(
                            slug=preset.slug,
                            name=preset.name,
                            current_version_num=None,
                            current_hash=None,
                            target_hash=target_hash,
                            action=action,
                            detail=detail,
                        )
                    )
                    continue

                curr_hash = spec_row["spec_hash"]
                curr_ver = spec_row["version_num"]
                is_archived = spec_row["is_archived"]

                if curr_hash == target_hash:
                    items.append(
                        PresetUpgradeItem(
                            slug=preset.slug,
                            name=preset.name,
                            current_version_num=curr_ver,
                            current_hash=curr_hash,
                            target_hash=target_hash,
                            action="already_current",
                            detail=f"Preset '{preset.slug}' is already on target hash {target_hash[:12]}...",
                        )
                    )
                    continue

                prior_hashes = PRESET_PREVIOUS_HASHES.get(preset.slug, set())
                if curr_hash in prior_hashes:
                    action = "would_upgrade" if dry_run else "upgraded"
                    detail = (
                        f"Matched prior built-in hash {curr_hash[:12]}...; "
                        f"{'would upgrade' if dry_run else 'upgraded'} to v{(curr_ver or 0) + 1} "
                        f"with event crossover target hash {target_hash[:12]}... "
                        f"(archived state preserved: {is_archived})."
                    )
                    if not dry_run:
                        spec_id = spec_row["id"]
                        existing_ver_id = await conn.fetchval(
                            "SELECT id FROM scan_spec_versions WHERE spec_id = $1 AND spec_hash = $2;",
                            spec_id,
                            target_hash,
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
                                existing_ver_id,
                                spec_id,
                                next_ver,
                                target_hash,
                                json.dumps(univ_dict),
                                json.dumps(rules_dict),
                            )
                        update_res = await conn.execute(
                            """
                            UPDATE scan_specs
                            SET active_version_id = $1, description = $2, updated_at = now()
                            WHERE id = $3 AND active_version_id = $4;
                            """,
                            existing_ver_id,
                            preset.description,
                            spec_id,
                            spec_row["active_version_id"],
                        )
                        if update_res == "UPDATE 0":
                            rechecked = await conn.fetchrow(
                                """
                                SELECT s.id, s.slug, s.name, s.active_version_id, s.is_archived,
                                       v.version_num, v.spec_hash
                                FROM scan_specs s
                                LEFT JOIN scan_spec_versions v ON s.active_version_id = v.id
                                WHERE s.id = $1;
                                """,
                                spec_id,
                            )
                            items.append(
                                PresetUpgradeItem(
                                    slug=preset.slug,
                                    name=preset.name,
                                    current_version_num=rechecked["version_num"] if rechecked else None,
                                    current_hash=rechecked["spec_hash"] if rechecked else None,
                                    target_hash=target_hash,
                                    action="custom_preserved",
                                    detail="Preset active version concurrently changed; user edit preserved.",
                                )
                            )
                            continue
                    items.append(
                        PresetUpgradeItem(
                            slug=preset.slug,
                            name=preset.name,
                            current_version_num=curr_ver,
                            current_hash=curr_hash,
                            target_hash=target_hash,
                            action=action,
                            detail=detail,
                        )
                    )
                else:
                    items.append(
                        PresetUpgradeItem(
                            slug=preset.slug,
                            name=preset.name,
                            current_version_num=curr_ver,
                            current_hash=curr_hash,
                            target_hash=target_hash,
                            action="custom_preserved",
                            detail=f"Preset has custom hash {curr_hash[:12] if curr_hash else 'none'}; user modifications preserved.",
                        )
                    )

    return PresetUpgradeResponse(
        dry_run=dry_run,
        active_count=active_count,
        max_active_specs=MAX_ACTIVE_SPECS,
        items=items,
    )


@router.post("/presets/{slug}/activate", dependencies=OPERATOR_WRITE)
async def activate_preset_version(slug: str, request: Request) -> ScanSpecRead:
    """Explicit guarded operator activation to upgrade/activate the latest built-in preset version."""
    pool = request.app.state.pool
    preset = next((p for p in BUILTIN_PRESETS if p.slug == slug), None)
    if not preset:
        raise HTTPException(status_code=404, detail=f"Built-in preset '{slug}' not found")

    univ_dict = preset.universe.model_dump()
    rules_dict = [rg.model_dump() for rg in preset.rules]
    target_hash = compute_spec_hash(univ_dict, {"rules": rules_dict})

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('scan_specs:active_limit'));")
            spec_row = await conn.fetchrow(
                "SELECT id, slug, active_version_id, is_archived FROM scan_specs WHERE slug = $1;",
                slug,
            )
            if not spec_row:
                raise HTTPException(status_code=404, detail=f"Scan spec '{slug}' not found in database")

            is_currently_active = (not spec_row["is_archived"]) and (spec_row["active_version_id"] is not None)
            if not is_currently_active:
                active_count = await conn.fetchval(
                    "SELECT count(*) FROM scan_specs WHERE is_archived = false AND active_version_id IS NOT NULL;"
                )
                if active_count >= MAX_ACTIVE_SPECS:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot activate preset: maximum active specs limit ({MAX_ACTIVE_SPECS}) reached. Archive an existing spec first.",
                    )

            spec_id = spec_row["id"]
            existing_ver_id = await conn.fetchval(
                "SELECT id FROM scan_spec_versions WHERE spec_id = $1 AND spec_hash = $2;",
                spec_id,
                target_hash,
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
                    existing_ver_id,
                    spec_id,
                    next_ver,
                    target_hash,
                    json.dumps(univ_dict),
                    json.dumps(rules_dict),
                )

            await conn.execute(
                """
                UPDATE scan_specs
                SET active_version_id = $1, description = $2, is_archived = false, updated_at = now()
                WHERE id = $3;
                """,
                existing_ver_id,
                preset.description,
                spec_id,
            )

    row = await pool.fetchrow(
        """
        SELECT s.id, s.slug, s.name, s.description, s.active_version_id, s.is_archived,
               s.created_at, s.updated_at,
               v.version_num, v.spec_hash, v.universe, v.rules, v.created_at as version_created_at
        FROM scan_specs s
        JOIN scan_spec_versions v ON s.active_version_id = v.id
        WHERE s.id = $1;
        """,
        spec_id,
    )

    active_ver = ScanSpecVersionRead(
        id=row["active_version_id"],
        spec_id=row["id"],
        version_num=row["version_num"],
        spec_hash=row["spec_hash"],
        universe=json.loads(row["universe"]) if isinstance(row["universe"], str) else row["universe"],
        rules=json.loads(row["rules"]) if isinstance(row["rules"], str) else row["rules"],
        created_at=row["version_created_at"],
    )

    return ScanSpecRead(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        description=row["description"],
        active_version_id=row["active_version_id"],
        active_version=active_ver,
        is_archived=row["is_archived"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
