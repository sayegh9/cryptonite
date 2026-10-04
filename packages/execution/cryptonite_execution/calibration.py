"""Confidence calibration from outcome labels (AI-first blueprint A7.2).

A role is calibrated when its stated confidence matches how often it is right. For the portfolio
verdict (``debate.verdict``): ``conviction_score`` against whether the decision was right after
24h (the signed outcome is positive: a BUY saw the asset rise, a SELL/HOLD saw it fall). Decisions
are binned by confidence; the expected calibration error (ECE) is the sample-weighted mean gap
between stated confidence and hit rate, in percentage points. Too few labelled decisions give no
number, so the L3 gate keeps failing closed.
"""

from decimal import Decimal
from typing import Any

MIN_SAMPLES = 20
BINS = 5
HORIZON = "24h"


def reliability(points: list[tuple[float, bool]], bins: int = BINS) -> dict[str, Any]:
    """``points`` are (stated confidence 0-1, was right). Returns per-bin mean confidence / hit rate and
    the ECE in percentage points (None below ``MIN_SAMPLES``)."""
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for conf, right in points:
        c = min(max(float(conf), 0.0), 1.0)
        buckets[min(int(c * bins), bins - 1)].append((c, right))
    rows, gap_sum = [], 0.0
    for i, b in enumerate(buckets):
        if not b:
            rows.append({"lo": i / bins, "hi": (i + 1) / bins, "n": 0, "mean_confidence": None, "hit_rate": None})
            continue
        mean_conf = sum(c for c, _ in b) / len(b)
        hit = sum(1 for _, r in b if r) / len(b)
        gap_sum += len(b) * abs(mean_conf - hit)
        rows.append({"lo": i / bins, "hi": (i + 1) / bins, "n": len(b),
                     "mean_confidence": round(mean_conf, 4), "hit_rate": round(hit, 4)})
    n = len(points)
    ece = round(gap_sum / n * 100, 2) if n >= MIN_SAMPLES else None
    return {"n": n, "min_samples": MIN_SAMPLES, "ece_pp": ece, "bins": rows}


def _right(recommendation: str, return_pct: float) -> bool:
    signed = return_pct if recommendation.upper() == "BUY" else -return_pct
    return signed > 0


async def verdict_points(conn, mandate_id: Any = None, since: Any = None) -> list[tuple[float, bool]]:
    """(conviction, right after 24h) for labelled verdicts, optionally of one mandate since a time."""
    rows = await conn.fetch(
        """SELECT pd.conviction_score, pd.recommendation, o.return_pct
           FROM portfolio_decisions pd
           JOIN ai_decision_outcomes o ON o.decision_id = pd.id AND o.horizon = $1
           LEFT JOIN debate_runs dr ON dr.id = pd.run_id
           WHERE ($2::uuid IS NULL OR dr.mandate_id = $2::uuid)
             AND ($3::timestamptz IS NULL OR pd.created_at >= $3::timestamptz)""",
        HORIZON, mandate_id, since)
    return [(float(r["conviction_score"]), _right(r["recommendation"], float(r["return_pct"]))) for r in rows]


async def calibration_error_pp(conn, mandate_id: Any, since: Any) -> Decimal | None:
    ece = reliability(await verdict_points(conn, mandate_id, since))["ece_pp"]
    return Decimal(str(ece)) if ece is not None else None
