"""Past decisions shown next to an approval (remediation R6.4).

A plain query on labelled decisions: the same instrument and recommendation, newest first, with the
24h outcome. No model or embedding call, so it cannot slow or fail an approval; callers drop the
field when it raises.
"""

import logging
import uuid
from typing import Any

from cryptonite_execution.calibration import score_decision

logger = logging.getLogger("cryptonite.api.similar")

HORIZON = "24h"
LIMIT = 3


async def similar_decisions(conn, instrument_id: str | None, recommendation: str | None,
                            exclude_run_id: uuid.UUID | None = None, limit: int = LIMIT) -> list[dict[str, Any]]:
    if not instrument_id:
        return []
    rows = await conn.fetch(
        """SELECT pd.run_id, pd.created_at, pd.recommendation, o.return_pct
           FROM portfolio_decisions pd
           JOIN ai_decision_outcomes o ON o.decision_id = pd.id AND o.horizon = $1
           WHERE pd.instrument_id = $2
             AND ($3::text IS NULL OR pd.recommendation = $3)
             AND ($4::uuid IS NULL OR pd.run_id IS DISTINCT FROM $4)
           ORDER BY pd.created_at DESC
           LIMIT $5""",
        HORIZON, instrument_id, recommendation.upper() if recommendation else None, exclude_run_id, limit)
    return [{"run_id": str(r["run_id"]) if r["run_id"] else None, "created_at": r["created_at"].isoformat(),
             "recommendation": r["recommendation"],
             # signed like the learner's outcome: BUY gains when the asset rose, SELL/HOLD when it fell
             "return_pct": signed(r["recommendation"], float(r["return_pct"])),
             "asset_return_pct": float(r["return_pct"]),
             "outcome": score_decision(r["recommendation"], float(r["return_pct"]))} for r in rows]


def signed(recommendation: str, return_pct: float) -> float:
    """Same convention as ``services/debate_worker/outcomes.signed_outcome``."""
    return return_pct if recommendation.upper() == "BUY" else -return_pct


async def similar_or_none(pool, instrument_id: str | None, recommendation: str | None,
                          exclude_run_id: uuid.UUID | None = None) -> list[dict[str, Any]] | None:
    """``similar_decisions`` with its own connection; None (field omitted) on any failure."""
    try:
        async with pool.acquire() as conn:
            return await similar_decisions(conn, instrument_id, recommendation, exclude_run_id)
    except Exception as e:  # informational only: never fail the endpoint over it
        logger.warning("similar_decisions unavailable for %s: %s", instrument_id, e)
        return None
