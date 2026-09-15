"""Strategy registry endpoints."""

import json
from typing import Any
from uuid import UUID, uuid4

from cryptonite_strategies.registry import STRATEGY_REGISTRY, list_strategies
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])


async def ensure_strategies_seeded(pool: Any) -> None:
    """Ensure all registered strategies are seeded into strategies & strategy_versions tables."""
    if not pool:
        return

    for meta in list_strategies():
        slug = meta["slug"]
        row = await pool.fetchrow("SELECT id FROM strategies WHERE slug = $1", slug)
        if not row:
            strat_id = uuid4()
            version_id = uuid4()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO strategies (id, name, slug, description, author)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        strat_id,
                        meta["name"],
                        meta["slug"],
                        meta["description"],
                        meta["author"],
                    )
                    await conn.execute(
                        """
                        INSERT INTO strategy_versions (
                            id, strategy_id, version_num, code_hash, git_commit,
                            default_parameters, parameter_schema
                        ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                        """,
                        version_id,
                        strat_id,
                        meta["version_num"],
                        meta["code_hash"],
                        "rc4",
                        json.dumps(meta["default_parameters"]),
                        json.dumps(meta["parameter_schema"]),
                    )


@router.get("")
async def get_strategies(request: Request) -> list[dict[str, Any]]:
    """List all available strategies with active versions and parameter schemas."""
    pool = request.app.state.pool
    await ensure_strategies_seeded(pool)

    query = """
    SELECT DISTINCT ON (s.id) s.id, s.name, s.slug, s.description, s.author,
           sv.id AS active_version_id, sv.version_num, sv.code_hash,
           sv.default_parameters, sv.parameter_schema
    FROM strategies s
    JOIN strategy_versions sv ON sv.strategy_id = s.id
    ORDER BY s.id, sv.created_at DESC;
    """
    rows = await pool.fetch(query)
    results = []
    for r in rows:
        results.append({
            "id": str(r["id"]),
            "name": r["name"],
            "slug": r["slug"],
            "description": r["description"],
            "author": r["author"],
            "active_version_id": str(r["active_version_id"]),
            "version_num": r["version_num"],
            "code_hash": r["code_hash"],
            "default_parameters": json.loads(r["default_parameters"]) if isinstance(r["default_parameters"], str) else r["default_parameters"],
            "parameter_schema": json.loads(r["parameter_schema"]) if isinstance(r["parameter_schema"], str) else r["parameter_schema"],
        })
    return results


@router.get("/{slug}")
async def get_strategy_by_slug(slug: str, request: Request) -> dict[str, Any]:
    """Get single strategy details by slug."""
    pool = request.app.state.pool
    await ensure_strategies_seeded(pool)

    query = """
    SELECT s.id, s.name, s.slug, s.description, s.author,
           sv.id AS active_version_id, sv.version_num, sv.code_hash,
           sv.default_parameters, sv.parameter_schema
    FROM strategies s
    JOIN strategy_versions sv ON sv.strategy_id = s.id
    WHERE s.slug = $1
    ORDER BY sv.created_at DESC
    LIMIT 1;
    """
    r = await pool.fetchrow(query, slug)
    if not r:
        raise HTTPException(status_code=404, detail=f"Strategy '{slug}' not found.")

    return {
        "id": str(r["id"]),
        "name": r["name"],
        "slug": r["slug"],
        "description": r["description"],
        "author": r["author"],
        "active_version_id": str(r["active_version_id"]),
        "version_num": r["version_num"],
        "code_hash": r["code_hash"],
        "default_parameters": json.loads(r["default_parameters"]) if isinstance(r["default_parameters"], str) else r["default_parameters"],
        "parameter_schema": json.loads(r["parameter_schema"]) if isinstance(r["parameter_schema"], str) else r["parameter_schema"],
    }
