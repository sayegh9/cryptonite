"""Research reports (LE-6): the research loop's results as queryable history.

POST is called by the research worker (operator-scoped machine token); GET serves the digest, the
Brain and the UI. Re-posting the same report id is a no-op, so a retried worker never duplicates it.
Remediation Phase 5 adds the validation results (``folds``, ``dsr``, ``holdout``, ``robustness``,
migration 032) and ``GET /trials``, the number of parameter trials already tested on an
instrument/timeframe, which the loop feeds into the Deflated Sharpe Ratio.
Phase 8 (F3) adds ``GET /live-vs-backtest``: each running paper strategy instance against the latest
completed backtest of its strategy version (reporting only, ``cryptonite_api.live_vs_backtest``).
"""

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from cryptonite_api.live_vs_backtest import live_vs_backtest_report
from cryptonite_api.routes.auth import AUTHED_READ, OPERATOR_WRITE
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1/research", tags=["research"])
logger = logging.getLogger("cryptonite.api.research")


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
    # Phase 5 validation; absent from reports written before it
    folds: list[dict[str, Any]] | None = None
    dsr: float | None = Field(default=None, ge=0, le=1)
    holdout: dict[str, Any] | None = None
    robustness: dict[str, Any] | None = None


def _jsonb(value: Any) -> str | None:
    return json.dumps(value, default=str) if value else None


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


@router.post("/reports", status_code=201, dependencies=OPERATOR_WRITE)
async def save_report(body: ReportIn, request: Request) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        inserted = await conn.fetchval(
            """INSERT INTO research_reports (id, idea, status, reason, strategy_slug, strategy_name, in_sample,
                                             out_of_sample, cost_usd, report, created_at, folds, dsr, holdout, robustness)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10::jsonb, $11, $12::jsonb, $13, $14::jsonb, $15::jsonb)
               ON CONFLICT (id) DO NOTHING RETURNING id""",
            body.id, body.idea, body.status, body.reason, body.strategy_slug, body.strategy_name,
            json.dumps(body.in_sample, default=str), json.dumps(body.out_of_sample, default=str), body.cost_usd,
            json.dumps(body.report, default=str), body.created_at, _jsonb(body.folds), body.dsr,
            _jsonb(body.holdout), _jsonb(body.robustness))
    return {"id": str(body.id), "created": inserted is not None}


@router.get("/reports", dependencies=AUTHED_READ)
async def list_reports(request: Request, hours: int = Query(default=24, ge=1, le=24 * 365),
                       limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, idea, status, reason, strategy_slug, strategy_name, in_sample, out_of_sample, cost_usd, created_at,
                      folds, dsr, holdout, robustness
               FROM research_reports WHERE created_at >= now() - make_interval(hours => $1)
               ORDER BY created_at DESC LIMIT $2""", hours, limit)
    return {"reports": [{
        "id": str(r["id"]), "idea": r["idea"], "status": r["status"], "reason": r["reason"],
        "strategy_slug": r["strategy_slug"], "strategy_name": r["strategy_name"],
        "in_sample": json.loads(r["in_sample"]), "out_of_sample": json.loads(r["out_of_sample"]),
        "cost_usd": float(r["cost_usd"]), "created_at": r["created_at"].isoformat(),
        "folds": _load(r["folds"]), "dsr": float(r["dsr"]) if r["dsr"] is not None else None,
        "holdout": _load(r["holdout"]), "robustness": _load(r["robustness"])} for r in rows]}


@router.get("/trials", dependencies=AUTHED_READ)
async def trials_tested(request: Request, instrument_id: str = Query(min_length=1, max_length=64),
                        timeframe: str = Query(min_length=1, max_length=8),
                        days: int = Query(default=90, ge=1, le=3650)) -> dict[str, Any]:
    """Parameter trials of earlier research on this instrument/timeframe (R5.4: the DSR's trial count)."""
    async with request.app.state.pool.acquire() as conn:
        trials = await conn.fetchval(
            """SELECT coalesce(sum(jsonb_array_length(coalesce(report->'trials', '[]'::jsonb))), 0)
               FROM research_reports
               WHERE report->>'instrument_id' = $1 AND report->>'timeframe' = $2
                 AND created_at >= now() - make_interval(days => $3)""", instrument_id, timeframe, days)
    return {"instrument_id": instrument_id, "timeframe": timeframe, "days": days, "trials": int(trials)}


@router.get("/live-vs-backtest", dependencies=AUTHED_READ)
async def live_vs_backtest(request: Request) -> dict[str, Any]:
    """F3, reporting only. ``available: false`` with empty lists when the report cannot be built."""
    from cryptonite_api.routes.backtests import DEFAULT_ARTIFACTS_PATH

    base = Path(os.getenv("BACKTEST_ARTIFACTS_PATH", DEFAULT_ARTIFACTS_PATH))
    try:
        report = await live_vs_backtest_report(request.app.state.pool, base)
    except Exception as e:
        logger.warning("Live-vs-backtest report unavailable: %s", e)
        return {"as_of": datetime.datetime.now(datetime.UTC).isoformat(), "available": False,
                "instances": [], "without_backtest": []}
    return {"as_of": datetime.datetime.now(datetime.UTC).isoformat(), "available": True, **report}
