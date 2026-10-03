"""Research reports (LE-6): the research loop's results as queryable history.

POST is called by the research worker (operator-scoped machine token); GET serves the digest, the
Brain and the UI. Re-posting the same report id is a no-op, so a retried worker never duplicates it.
"""

import datetime
import json
from typing import Any, Literal
from uuid import UUID

from cryptonite_api.routes.auth import AUTHED_READ, OPERATOR_WRITE
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1/research", tags=["research"])


class ReportIn(BaseModel):
    id: UUID
    idea: str = Field(min_length=1, max_length=4000)
    status: Literal["PROMOTE", "REVISE", "REJECT", "FAILED"]
    reason: str = Field(default="", max_length=4000)
    strategy_slug: str | None = Field(default=None, max_length=128)
    strategy_name: str | None = Field(default=None, max_length=256)
    in_sample: dict[str, Any] = Field(default_factory=dict)
    out_of_sample: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0
    created_at: datetime.datetime
    report: dict[str, Any]


@router.post("/reports", status_code=201, dependencies=OPERATOR_WRITE)
async def save_report(body: ReportIn, request: Request) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        inserted = await conn.fetchval(
            """INSERT INTO research_reports (id, idea, status, reason, strategy_slug, strategy_name, in_sample,
                                             out_of_sample, cost_usd, report, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10::jsonb, $11)
               ON CONFLICT (id) DO NOTHING RETURNING id""",
            body.id, body.idea, body.status, body.reason, body.strategy_slug, body.strategy_name,
            json.dumps(body.in_sample, default=str), json.dumps(body.out_of_sample, default=str), body.cost_usd,
            json.dumps(body.report, default=str), body.created_at)
    return {"id": str(body.id), "created": inserted is not None}


@router.get("/reports", dependencies=AUTHED_READ)
async def list_reports(request: Request, hours: int = Query(default=24, ge=1, le=24 * 365),
                       limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, idea, status, reason, strategy_slug, strategy_name, in_sample, out_of_sample, cost_usd, created_at
               FROM research_reports WHERE created_at >= now() - make_interval(hours => $1)
               ORDER BY created_at DESC LIMIT $2""", hours, limit)
    return {"reports": [{
        "id": str(r["id"]), "idea": r["idea"], "status": r["status"], "reason": r["reason"],
        "strategy_slug": r["strategy_slug"], "strategy_name": r["strategy_name"],
        "in_sample": json.loads(r["in_sample"]), "out_of_sample": json.loads(r["out_of_sample"]),
        "cost_usd": float(r["cost_usd"]), "created_at": r["created_at"].isoformat()} for r in rows]}
