"""Strategy registry endpoints."""

import hashlib
import json
from typing import Any
from uuid import uuid4

from cryptonite_api.routes.auth import AUTHED_READ, OPERATOR_WRITE, require_csrf, require_operator
from cryptonite_strategies.registry import STRATEGY_REGISTRY, list_strategies
from cryptonite_strategies.validation import resolve_imports, validate_strategy_source
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

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



class CreateStrategyRequest(BaseModel):
    name: str
    description: str
    python_code: str

@router.post("", dependencies=OPERATOR_WRITE)
async def create_strategy(req: CreateStrategyRequest, request: Request):
    pool = request.app.state.pool
    # Model-written code is untrusted: reject it here, before it is stored or ever backtested
    validation = validate_strategy_source(req.python_code)
    violations = validation.violations if not validation.ok else resolve_imports(req.python_code)
    if violations:
        raise HTTPException(status_code=422, detail={"error": "STRATEGY_CODE_REJECTED", "violations": violations})
    
    # Generate slug and hashes
    slug = f"ai-gen-{uuid4().hex[:8]}"
    strat_id = uuid4()
    version_id = uuid4()
    code_hash = hashlib.sha256(req.python_code.encode()).hexdigest()
    
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow("SELECT id, slug FROM strategies WHERE name = $1", req.name)
            
            if existing:
                if existing["slug"] in STRATEGY_REGISTRY:
                    raise HTTPException(status_code=409, detail="Core strategies cannot receive generated code; choose another name.")
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


# Tables whose rows tie a paper instance to its history: an instance with any of these cannot be removed
INSTANCE_HISTORY_TABLES = ("paper_orders", "paper_positions", "paper_trades", "paper_risk_events")


def instance_delete_blockers(instances: list[dict[str, Any]], history: dict[str, int]) -> list[str]:
    """Why these paper instances cannot be removed with their strategy. ``instances`` are rows with
    ``instance_id`` and ``status``; ``history`` maps an instance id to its count of trading-history rows.
    An instance can go only when it is STOPPED and has no history."""
    blockers = []
    for inst in instances:
        iid = inst["instance_id"]
        if inst["status"] != "STOP":
            blockers.append(f"instance {iid} is {inst['status']}: stop it first")
        elif history.get(iid, 0):
            blockers.append(f"instance {iid} has trading history: it cannot be removed")
    return blockers


@router.delete("/{slug}")
async def delete_strategy(slug: str, request: Request, _op=Depends(require_operator), _csrf=Depends(require_csrf)):
    """Delete a custom/AI-generated strategy and its versions. A stopped paper instance with no trading history
    that runs this strategy is removed with it (the instance row must go before the version it points to);
    anything else that still uses the strategy is refused with the reason, nothing is deleted."""
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
            version_ids = [r["id"] for r in await conn.fetch(
                "SELECT id FROM strategy_versions WHERE strategy_id = $1", strat_id)]
            runs = await conn.fetchval(
                "SELECT count(*) FROM backtest_runs WHERE strategy_version_id = ANY($1::uuid[])", version_ids)
            if runs:
                raise HTTPException(status_code=409, detail=(
                    f"Strategy '{slug}' has {runs} backtest run(s); they are its history, so it is kept."))

            instances = [dict(r) for r in await conn.fetch(
                "SELECT instance_id, status FROM paper_strategy_instances WHERE strategy_version_id = ANY($1::uuid[])",
                version_ids)]
            history: dict[str, int] = {}
            for inst in instances:
                for table in INSTANCE_HISTORY_TABLES:
                    history[inst["instance_id"]] = history.get(inst["instance_id"], 0) + await conn.fetchval(
                        f"SELECT count(*) FROM {table} WHERE instance_id = $1", inst["instance_id"])
            blockers = instance_delete_blockers(instances, history)
            if blockers:
                raise HTTPException(status_code=409, detail="; ".join(blockers))

            removed = [inst["instance_id"] for inst in instances]
            if removed:
                await conn.execute("DELETE FROM paper_strategy_instances WHERE instance_id = ANY($1::text[])", removed)
            await conn.execute("DELETE FROM strategy_versions WHERE strategy_id = $1", strat_id)
            await conn.execute("DELETE FROM strategies WHERE id = $1", strat_id)

    return {"status": "success", "message": f"Strategy '{slug}' deleted successfully.",
            "removed_instances": removed}
