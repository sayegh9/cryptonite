"""Scanner specifications, immutable versions, and execution jobs route."""

import json
from typing import Any
from uuid import UUID, uuid4

from cryptonite_scanner.models import (
    ScanJobRead,
    ScanSpecCreate,
    ScanSpecRead,
    ScanSpecVersionRead,
    compute_spec_hash,
)
from cryptonite_scanner.presets import BUILTIN_PRESETS
from fastapi import APIRouter, HTTPException, Request, Response, status

router = APIRouter(prefix="/api/v1/scanners", tags=["scanners"])


async def ensure_presets_seeded(pool: Any) -> None:
    """Seed built-in scanner presets if no specs currently exist in database."""
    count = await pool.fetchval("SELECT count(*) FROM scan_specs;")
    if count > 0:
        return

    for preset in BUILTIN_PRESETS:
        spec_id = uuid4()
        version_id = uuid4()
        univ_dict = preset.universe.model_dump()
        rules_dict = [rg.model_dump() for rg in preset.rules]
        spec_hash = compute_spec_hash(univ_dict, {"rules": rules_dict})

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO scan_specs (id, slug, name, description, active_version_id, is_archived)
                    VALUES ($1, $2, $3, $4, NULL, false);
                    """,
                    spec_id,
                    preset.slug,
                    preset.name,
                    preset.description,
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
                    "UPDATE scan_specs SET active_version_id = $1 WHERE id = $2;",
                    version_id,
                    spec_id,
                )


@router.get("")
async def list_scanners(request: Request) -> list[ScanSpecRead]:
    """List active scanner specs with their current active version."""
    pool = request.app.state.pool
    await ensure_presets_seeded(pool)

    rows = await pool.fetch(
        """
        SELECT s.id, s.slug, s.name, s.description, s.active_version_id, s.is_archived,
               s.created_at, s.updated_at,
               v.version_num, v.spec_hash, v.universe, v.rules, v.created_at as version_created_at
        FROM scan_specs s
        LEFT JOIN scan_spec_versions v ON s.active_version_id = v.id
        WHERE s.is_archived = false
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


@router.post("", status_code=status.HTTP_201_CREATED)
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
            # Check unique slug
            existing = await conn.fetchval("SELECT id FROM scan_specs WHERE slug = $1;", spec_in.slug)
            if existing:
                raise HTTPException(status_code=400, detail=f"Slug '{spec_in.slug}' already exists")

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


@router.put("/{spec_id}")
async def update_scanner(spec_id: UUID, spec_in: ScanSpecCreate, request: Request) -> ScanSpecRead:
    """Append a new immutable version to the spec and point active_version_id to it."""
    pool = request.app.state.pool
    new_version_id = uuid4()

    univ_dict = spec_in.universe.model_dump()
    rules_dict = [rg.model_dump() for rg in spec_in.rules]
    spec_hash = compute_spec_hash(univ_dict, {"rules": rules_dict})

    async with pool.acquire() as conn:
        async with conn.transaction():
            spec = await conn.fetchrow("SELECT id FROM scan_specs WHERE id = $1 AND is_archived = false;", spec_id)
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


@router.delete("/{spec_id}")
async def archive_scanner(spec_id: UUID, request: Request) -> dict[str, str]:
    """Archive a scanner specification without deleting research history."""
    pool = request.app.state.pool
    res = await pool.execute(
        "UPDATE scan_specs SET is_archived = true, updated_at = now() WHERE id = $1;",
        spec_id,
    )
    if "0" in res:
        raise HTTPException(status_code=404, detail="Scan spec not found")
    return {"status": "archived", "id": str(spec_id)}


@router.post("/{spec_id}/run", status_code=status.HTTP_202_ACCEPTED)
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
        "request_id": req_id,
        "spec_version_id": str(row["active_version_id"]),
    }


@router.get("/jobs/{job_id}")
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
