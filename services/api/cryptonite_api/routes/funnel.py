"""H13c: what happened to each scanner setup. Reads only: nothing here writes, queues or computes an outcome."""

import datetime

from cryptonite_api.routes.auth import require_operator
from cryptonite_schemas.funnel import ROWS_SQL, funnel_counts, funnel_row
from fastapi import APIRouter, Depends, Query, Request

router = APIRouter(prefix="/api/v1/funnel", tags=["funnel"])

ROWS_MAX = 500
EVAL_OUTCOMES = ("NO_TRIGGER", "REJECTED_RR", "STALE", "MATCHED_FRESH")
CAVEATS = [
    "What-if figures are model figures, not trades.",
    "They assume the profile's exits as they are today, not as they were when the setup appeared.",
    "Provisional figures are before later costs and can still change; final figures are net of the cost model.",
    "Setups overlap, so these are not independent wins: several rows can be the same move.",
]


@router.get("")
async def get_funnel(request: Request, auth: dict = Depends(require_operator),
                     hours: int = Query(default=24, ge=1, le=168)):
    async with request.app.state.pool.acquire() as conn:
        now = await conn.fetchval("SELECT now()")
        since = now - datetime.timedelta(hours=hours)
        evaluations = await conn.fetch(
            "SELECT outcome, sum(n)::bigint AS n FROM scan_eval_counts WHERE bucket_start >= $1 GROUP BY outcome", since)
        raw = await conn.fetch(ROWS_SQL, since)
    rows = [funnel_row(dict(r), now) for r in raw]
    found = {r["outcome"]: r["n"] for r in evaluations}
    return {"now": now.isoformat(), "hours": hours,
            "counts": {"evaluations": {o: found.get(o, 0) for o in EVAL_OUTCOMES}, **funnel_counts(rows)},
            "rows": rows[:ROWS_MAX], "rows_truncated": len(rows) > ROWS_MAX, "caveats": CAVEATS}
