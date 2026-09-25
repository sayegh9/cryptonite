"""Strategy registry endpoints."""

import json
from typing import Any
from uuid import UUID, uuid4

from cryptonite_strategies.registry import STRATEGY_REGISTRY, list_strategies
from cryptonite_api.routes.auth import AUTHED_READ, OPERATOR_WRITE, require_operator, require_csrf
from fastapi import APIRouter, Depends, HTTPException, Request

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



from pydantic import BaseModel
import hashlib

class CreateStrategyRequest(BaseModel):
    name: str
    description: str
    python_code: str

@router.post("", dependencies=OPERATOR_WRITE)
async def create_strategy(req: CreateStrategyRequest, request: Request):
    pool = request.app.state.pool
    
    # Generate slug and hashes
    slug = f"ai-gen-{uuid4().hex[:8]}"
    strat_id = uuid4()
    version_id = uuid4()
    code_hash = hashlib.sha256(req.python_code.encode()).hexdigest()
    
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow("SELECT id, slug FROM strategies WHERE name = $1", req.name)
            
            if existing:
                strat_id = existing["id"]
                slug = existing["slug"]
                version_row = await conn.fetchrow("SELECT COALESCE(MAX(version_num), 0) + 1 as next_v FROM strategy_versions WHERE strategy_id = $1", strat_id)
                next_v = version_row["next_v"]
            else:
                next_v = 1
                await conn.execute(
                    '''
                    INSERT INTO strategies (id, name, slug, description, author)
                    VALUES ($1, $2, $3, $4, $5)
                    ''',
                    strat_id, req.name, slug, req.description, "AI Agent"
                )

            await conn.execute(
                '''
                INSERT INTO strategy_versions (
                    id, strategy_id, version_num, code_hash, git_commit,
                    default_parameters, parameter_schema, python_code
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8)
                ''',
                version_id, strat_id, next_v, code_hash, "ai-generated",
                "{}", "{}", req.python_code
            )
            
    return {"status": "success", "slug": slug, "id": str(strat_id), "version_id": str(version_id)}


@router.get("", dependencies=AUTHED_READ)
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
    
    valid_slugs = set(STRATEGY_REGISTRY.keys())
    results = []
    for r in rows:
        # Include if registered OR if it's an AI generated strategy
        if r["slug"] not in valid_slugs and not r["slug"].startswith("ai-gen"):
            continue
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


@router.get("/{slug}", dependencies=AUTHED_READ)
async def get_strategy_by_slug(slug: str, request: Request) -> dict[str, Any]:
    """Get single strategy details by slug."""
    pool = request.app.state.pool
    await ensure_strategies_seeded(pool)

    query = """
    SELECT s.id, s.name, s.slug, s.description, s.author,
           sv.id AS active_version_id, sv.version_num, sv.code_hash,
           sv.default_parameters, sv.parameter_schema, sv.python_code
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
        "python_code": r["python_code"] if "python_code" in r and r["python_code"] else None,
        "default_parameters": json.loads(r["default_parameters"]) if isinstance(r["default_parameters"], str) else r["default_parameters"],
        "parameter_schema": json.loads(r["parameter_schema"]) if isinstance(r["parameter_schema"], str) else r["parameter_schema"],
    }


@router.delete("/{slug}")
async def delete_strategy(slug: str, request: Request, _op=Depends(require_operator), _csrf=Depends(require_csrf)):
    """Delete a custom/AI-generated strategy and its versions."""
    pool = request.app.state.pool
    
    # Protect built-in core strategies
    if slug in ("rsi-reversal", "ema-cross", "supertrend"):
        raise HTTPException(status_code=400, detail="Cannot delete core system strategies.")
        
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT id FROM strategies WHERE slug = $1", slug)
            if not row:
                raise HTTPException(status_code=404, detail=f"Strategy '{slug}' not found.")
                
            strat_id = row["id"]
            await conn.execute("DELETE FROM strategy_versions WHERE strategy_id = $1", strat_id)
            await conn.execute("DELETE FROM strategies WHERE id = $1", strat_id)
            
    return {"status": "success", "message": f"Strategy '{slug}' deleted successfully."}
