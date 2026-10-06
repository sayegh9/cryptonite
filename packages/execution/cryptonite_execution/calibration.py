"""Confidence calibration from outcome labels (AI-first blueprint A7.2).

A role is calibrated when its stated confidence matches how often it is right. For the portfolio
verdict (``debate.verdict``): ``conviction_score`` against whether the decision was right after
24h (``score_decision``: a BUY wins on a rise beyond a small band, a SELL on a fall beyond it, a HOLD
when the asset did not rise beyond it; moves inside the band are NEUTRAL and left out). Decisions
are binned by confidence; the expected calibration error (ECE) is the sample-weighted mean gap
between stated confidence and hit rate, in percentage points. Too few labelled decisions give no
number, so the L3 gate keeps failing closed.

Reported alongside, for information only (R4.3, D6): a 90% bootstrap interval of the ECE and the
reliability table on equal-count bins (fixed-width bins can leave one bin with most of the points).
The 20-decision minimum is policy and unchanged.

Also for information only (F5): an isotonic (pool-adjacent-violators) map from stated conviction to
hit rate, fitted once there are ``MIN_ISOTONIC_POINTS`` labelled decisions. It is shown next to the
raw conviction and never used for sizing, routing or a gate.
"""

import random
from decimal import Decimal
from typing import Any, Literal

from cryptonite_schemas.debate import normalize_verdict

MIN_SAMPLES = 20
BINS = 5
HORIZON = "24h"
NEUTRAL_BAND = 0.002  # |24h return| <= 0.2%: too small to call right or wrong
ECE_BOOT = 1000
ECE_SEED = 20261005
MIN_ISOTONIC_POINTS = 200  # F5: the isotonic map is fitted from 200 labelled (WIN/LOSS) decisions on

Score = Literal["WIN", "LOSS", "NEUTRAL"]


def score_decision(recommendation: str, return_pct: float, band: float = NEUTRAL_BAND) -> Score:
    """Discrete right/wrong of a verdict from the asset's return after the horizon (a fraction).

    BUY wins above +band and loses below -band; SELL (spot: exit) is the mirror; HOLD wins when the
    asset did not rise beyond +band and loses above it (a missed rise). Everything else is NEUTRAL.
    Any spelling ``normalize_verdict`` knows is graded by its direction (B2: Overweight is a BUY);
    an unknown verdict raises ``ValueError`` rather than being graded as a short.
    """
    rec = normalize_verdict(recommendation)
    if rec == "HOLD":
        return "WIN" if return_pct <= band else "LOSS"
    signed = return_pct if rec == "BUY" else -return_pct
    if signed > band:
        return "WIN"
    if signed < -band:
        return "LOSS"
    return "NEUTRAL"


def _ece(points: list[tuple[float, bool]], bins: int) -> float:
    gap = 0.0
    for b in _fixed_buckets(points, bins):
        if b:
            gap += len(b) * abs(sum(c for c, _ in b) / len(b) - sum(1 for _, r in b if r) / len(b))
    return gap / len(points) * 100


def _fixed_buckets(points: list[tuple[float, bool]], bins: int) -> list[list[tuple[float, bool]]]:
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for conf, right in points:
        c = min(max(float(conf), 0.0), 1.0)
        buckets[min(int(c * bins), bins - 1)].append((c, right))
    return buckets


def ece_interval_pp(points: list[tuple[float, bool]], bins: int = BINS, confidence: float = 0.90,
                    n_boot: int = ECE_BOOT, seed: int = ECE_SEED) -> tuple[float, float] | None:
    """Percentile bootstrap interval of the ECE (pp), seeded; None below ``MIN_SAMPLES``. Decisions are
    resampled independently (one label per decision, no serial structure assumed)."""
    if len(points) < MIN_SAMPLES:
        return None
    rng = random.Random(seed)
    n = len(points)
    vals = sorted(_ece([points[rng.randrange(n)] for _ in range(n)], bins) for _ in range(n_boot))
    alpha = (1 - confidence) / 2
    return round(vals[int(alpha * (n_boot - 1))], 2), round(vals[int((1 - alpha) * (n_boot - 1))], 2)


def equal_count_bins(points: list[tuple[float, bool]], bins: int = BINS) -> list[dict[str, Any]]:
    """Reliability on ``bins`` bins of (nearly) equal size, sorted by confidence."""
    pts = sorted((min(max(float(c), 0.0), 1.0), r) for c, r in points)
    rows = []
    for i in range(bins):
        b = pts[i * len(pts) // bins:(i + 1) * len(pts) // bins]
        if b:
            rows.append({"lo": b[0][0], "hi": b[-1][0], "n": len(b),
                         "mean_confidence": round(sum(c for c, _ in b) / len(b), 4),
                         "hit_rate": round(sum(1 for _, r in b if r) / len(b), 4)})
    return rows


def pav(ys: list[float], weights: list[float] | None = None) -> list[float]:
    """Pool-adjacent-violators: the non-decreasing sequence closest to ``ys`` in weighted least squares
    (``ys`` already ordered by x). Returns one fitted value per input."""
    ws = weights or [1.0] * len(ys)
    blocks: list[list[float]] = []  # [weighted sum, weight, count]
    for y, w in zip(ys, ws, strict=True):
        blocks.append([y * w, w, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, w2, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += w2
            blocks[-1][2] += c
    out: list[float] = []
    for s, w, c in blocks:
        out.extend([s / w] * int(c))
    return out


def isotonic_fit(points: list[tuple[float, bool]]) -> list[list[float]]:
    """Isotonic map from stated confidence to hit rate (F5): PAV on the points pooled per distinct
    confidence. Returns the step function as [[x, y], ...] (each step's first and last x)."""
    pooled: dict[float, list[float]] = {}
    for conf, right in points:
        c = min(max(float(conf), 0.0), 1.0)
        acc = pooled.setdefault(c, [0.0, 0.0])
        acc[0] += 1.0 if right else 0.0
        acc[1] += 1.0
    xs = sorted(pooled)
    fitted = pav([pooled[x][0] / pooled[x][1] for x in xs], [pooled[x][1] for x in xs])
    steps: list[list[float]] = []
    for i, (x, y) in enumerate(zip(xs, fitted, strict=True)):
        first = i == 0 or fitted[i - 1] != y
        last = i == len(xs) - 1 or fitted[i + 1] != y
        if first or last:
            steps.append([round(x, 4), round(y, 4)])
    return steps


def isotonic_report(points: list[tuple[float, bool]]) -> dict[str, Any]:
    """``{fitted, n, min_points, points}``: the isotonic calibration map, fitted only from
    ``MIN_ISOTONIC_POINTS`` labelled (WIN/LOSS) decisions on. Informational: never used for sizing,
    routing or a gate."""
    n = len(points)
    if n < MIN_ISOTONIC_POINTS:
        return {"fitted": False, "n": n, "min_points": MIN_ISOTONIC_POINTS, "points": []}
    return {"fitted": True, "n": n, "min_points": MIN_ISOTONIC_POINTS, "points": isotonic_fit(points)}


def _isotonic_or_unavailable(points: list[tuple[float, bool]]) -> dict[str, Any]:
    try:
        return isotonic_report(points)
    except Exception:  # reporting only: the reliability report (and the gate reading its ECE) stands without it
        return {"fitted": False, "n": len(points), "min_points": MIN_ISOTONIC_POINTS, "points": []}


def reliability(points: list[tuple[float, bool]], bins: int = BINS) -> dict[str, Any]:
    """``points`` are (stated confidence 0-1, was right). Returns per-bin mean confidence / hit rate and
    the ECE in percentage points (None below ``MIN_SAMPLES``), plus the informational ECE interval,
    equal-count bins and the isotonic calibration map (F5)."""
    rows, gap_sum = [], 0.0
    for i, b in enumerate(_fixed_buckets(points, bins)):
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
    ci = ece_interval_pp(points, bins)
    return {"n": n, "min_samples": MIN_SAMPLES, "ece_pp": ece, "bins": rows,
            "ece_interval_pp": list(ci) if ci else None, "equal_count_bins": equal_count_bins(points, bins),
            "isotonic": _isotonic_or_unavailable(points)}


async def verdict_points(conn, mandate_id: Any = None, since: Any = None) -> list[tuple[float, bool]]:
    """(conviction, right after 24h) for labelled verdicts, optionally of one mandate since a time.
    NEUTRAL outcomes (a move inside the band) are left out of the reliability bins. Rows written before
    migration 037 may carry the CRO's wording; ``score_decision`` grades them by direction."""
    rows = await conn.fetch(
        """SELECT pd.conviction_score, pd.recommendation, o.return_pct
           FROM portfolio_decisions pd
           JOIN ai_decision_outcomes o ON o.decision_id = pd.id AND o.horizon = $1
           LEFT JOIN debate_runs dr ON dr.id = pd.run_id
           WHERE ($2::uuid IS NULL OR dr.mandate_id = $2::uuid)
             AND ($3::timestamptz IS NULL OR pd.created_at >= $3::timestamptz)""",
        HORIZON, mandate_id, since)
    scored = ((float(r["conviction_score"]), score_decision(r["recommendation"], float(r["return_pct"]))) for r in rows)
    return [(conf, s == "WIN") for conf, s in scored if s != "NEUTRAL"]


async def calibration_error_pp(conn, mandate_id: Any, since: Any) -> Decimal | None:
    ece = reliability(await verdict_points(conn, mandate_id, since))["ece_pp"]
    return Decimal(str(ece)) if ece is not None else None


async def calibration_interval_pp(conn, mandate_id: Any, since: Any) -> tuple[Decimal, Decimal] | None:
    """Informational 90% interval of the mandate's ECE (R4.3); None below the minimum sample."""
    ci = ece_interval_pp(await verdict_points(conn, mandate_id, since))
    return (Decimal(str(ci[0])), Decimal(str(ci[1]))) if ci else None


REGIME_ORDER = ("TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOL", "UNKNOWN")


def outcomes_by_regime(rows: list[tuple[str | None, str, float]]) -> list[dict[str, Any]]:
    """F1 (reporting only): ``rows`` are (regime at decision time or None, recommendation, asset return
    after the horizon). Per regime: n, WIN / LOSS / NEUTRAL counts (``score_decision``) and the mean
    signed return (BUY gains on a rise; SELL / HOLD on a fall). Untagged decisions are ``UNKNOWN``.
    Verdicts are graded by direction (``normalize_verdict``), so BUY-like spellings pool as BUY."""
    acc: dict[str, dict[str, Any]] = {}
    for regime, rec, ret in rows:
        key = regime or "UNKNOWN"
        a = acc.setdefault(key, {"regime": key, "n": 0, "wins": 0, "losses": 0, "neutral": 0, "_sum": 0.0})
        score = score_decision(rec, ret)
        a["n"] += 1
        a["wins" if score == "WIN" else "losses" if score == "LOSS" else "neutral"] += 1
        a["_sum"] += ret if normalize_verdict(rec) == "BUY" else -ret
    out = []
    for key in sorted(acc, key=lambda k: REGIME_ORDER.index(k) if k in REGIME_ORDER else len(REGIME_ORDER)):
        a = acc[key]
        a["mean_signed_return"] = round(a.pop("_sum") / a["n"], 8)
        out.append(a)
    return out


async def regime_outcome_rows(conn, horizon: str = HORIZON, mandate_id: Any = None) -> list[tuple[str | None, str, float]]:
    """(regime, recommendation as BUY / SELL / HOLD, return) of every decision labelled at ``horizon``,
    optionally of one mandate."""
    rows = await conn.fetch(
        """SELECT o.regime, pd.recommendation, o.return_pct
           FROM portfolio_decisions pd
           JOIN ai_decision_outcomes o ON o.decision_id = pd.id AND o.horizon = $1
           LEFT JOIN debate_runs dr ON dr.id = pd.run_id
           WHERE ($2::uuid IS NULL OR dr.mandate_id = $2::uuid)""",
        horizon, mandate_id)
    return [(r["regime"], normalize_verdict(r["recommendation"]), float(r["return_pct"])) for r in rows]
