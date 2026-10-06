"""Shadow mode report (remediation Phase 8, F2): how a route's candidate model compares on live traffic.

Rows come from ``ai_shadow_results`` (written by the gateway). Each row is linked to its labelled
outcome, when one exists, through the setup's ``decision_trace_id`` (the trace parent the debate
worker passes): ``debate_runs.decision_trace_id`` → ``portfolio_decisions`` → ``ai_decision_outcomes``
at 24h. Reporting only: nothing here feeds a decision.

- Agreement: share of OK rows where the shadow's key field equals the primary's.
- Verdict roles (``recommendation`` / ``conviction_level``): primary and shadow are each scored with
  ``score_decision`` against the same 24h return.
- Triage (``worth_debate``): only accepted candidates are debated and labelled, so the report shows
  agreement on labelled rows and, where the shadow would have skipped the debate, how the debated
  decision turned out (a shadow that skips losers is better; one that skips winners is worse).
"""

import datetime
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from cryptonite_agents.gateway.pricing import is_free
from cryptonite_agents.gateway.routes import DEFAULT_ROUTES, route_from_row
from cryptonite_execution.calibration import HORIZON, score_decision
from cryptonite_schemas.debate import verdict_direction

VERDICT_KEYS = frozenset({"recommendation", "conviction_level"})
# Exit verdicts (``ExitDecision``) are not entry verdicts: REDUCE and CLOSE score as an exit (SELL)
_EXIT_DIRECTION = {"REDUCE": "SELL", "CLOSE": "SELL"}


def direction(value: Any) -> str | None:
    """A verdict (PM, lite, CRO or exit wording) as BUY / HOLD / SELL for ``score_decision``: entry
    verdicts through ``normalize_verdict``, exit verdicts through ``_EXIT_DIRECTION``."""
    if value is None:
        return None
    return verdict_direction(str(value)) or _EXIT_DIRECTION.get(str(value).strip().upper())


def _truthy(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().strip('"').lower() in {"true", "1", "yes", "on"}


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-int(q * 100) * len(ordered) // 100))  # ceil(q * n), at least 1
    return float(ordered[min(rank, len(ordered)) - 1])


def _scores(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    c = Counter(row[key] for row in rows if row.get(key))
    return {"wins": c["WIN"], "losses": c["LOSS"], "neutral": c["NEUTRAL"]}


def _outcomes(rows: list[dict[str, Any]], key_field: str | None) -> dict[str, Any] | None:
    labelled = [r for r in rows if r["status"] == "OK" and r.get("return_pct") is not None]
    if key_field in VERDICT_KEYS:
        scored = []
        for r in labelled:
            p, s = direction(r.get("primary_key")), direction(r.get("shadow_key"))
            if p and s:
                ret = float(r["return_pct"])
                scored.append({"p": score_decision(p, ret), "s": score_decision(s, ret)})
        return {"horizon": HORIZON, "labelled": len(scored), "primary": _scores(scored, "p"), "shadow": _scores(scored, "s")}
    if key_field == "worth_debate":
        scored = []
        for r in labelled:
            final = direction(r.get("final_recommendation"))
            if final:
                scored.append({"o": score_decision(final, float(r["return_pct"])), "agrees": r.get("agrees"),
                               "skip": _truthy(r.get("shadow_key")) is False})
        agreed = [x for x in scored if x["agrees"] is not None]
        return {
            "horizon": HORIZON, "labelled": len(scored),
            "agreement_rate": (sum(1 for x in agreed if x["agrees"]) / len(agreed)) if agreed else None,
            "debated": _scores(scored, "o"),
            "shadow_would_skip": {"n": sum(1 for x in scored if x["skip"]), **_scores([x for x in scored if x["skip"]], "o")},
        }
    return None


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts, agreement, latency, cost and (if labelled) outcomes for one group of shadow rows."""
    ok = [r for r in rows if r["status"] == "OK"]
    agreed = [r for r in ok if r.get("agrees") is not None]
    key_fields = {r["key_field"] for r in rows if r.get("key_field")}
    key_field = next(iter(key_fields)) if len(key_fields) == 1 else None
    lat = lambda col: [float(r[col]) for r in ok if r.get(col) is not None]  # noqa: E731
    shadow_cost = sum((Decimal(str(r["shadow_cost_usd"] or 0)) for r in rows), Decimal("0"))
    return {
        "n": len(rows), "ok": len(ok), "errors": sum(1 for r in rows if r["status"] == "ERROR"),
        "skipped": sum(1 for r in rows if r["status"] == "SKIPPED"),
        "skip_reasons": dict(Counter(r.get("error") or "?" for r in rows if r["status"] == "SKIPPED")),
        "key_field": key_field,
        "agreement": {"n": len(agreed), "agree": sum(1 for r in agreed if r["agrees"]),
                      "rate": (sum(1 for r in agreed if r["agrees"]) / len(agreed)) if agreed else None},
        "latency_ms": {"primary_p50": percentile(lat("primary_latency_ms"), 0.5),
                       "primary_p95": percentile(lat("primary_latency_ms"), 0.95),
                       "shadow_p50": percentile(lat("shadow_latency_ms"), 0.5),
                       "shadow_p95": percentile(lat("shadow_latency_ms"), 0.95)},
        "cost_usd": {"shadow": str(shadow_cost.quantize(Decimal("0.000001")))},
        "outcomes": _outcomes(rows, key_field),
    }


def report(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Per (role, shadow model) and per role."""
    by_model: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_model[(r["role"], r["shadow_model"])].append(r)
        by_role[r["role"]].append(r)
    return {
        "models": [{"role": role, "shadow_model": model,
                    "primary_models": sorted({r["primary_model"] for r in group}), **summarize(group)}
                   for (role, model), group in sorted(by_model.items())],
        "roles": [{"role": role, **summarize(group)} for role, group in sorted(by_role.items())],
    }


async def shadow_rows(conn, days: int, role: str | None) -> list[dict[str, Any]]:
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
    rows = await conn.fetch(
        """SELECT s.role, s.primary_model, s.shadow_model, s.status, s.key_field, s.agrees, s.error,
                  s.primary_latency_ms, s.shadow_latency_ms, s.primary_cost_usd, s.shadow_cost_usd,
                  s.primary_output ->> s.key_field AS primary_key, s.shadow_output ->> s.key_field AS shadow_key,
                  d.recommendation AS final_recommendation, o.return_pct
           FROM ai_shadow_results s
           LEFT JOIN LATERAL (
               SELECT pd.id, pd.recommendation FROM debate_runs dr JOIN portfolio_decisions pd ON pd.run_id = dr.id
               WHERE s.parent_trace_id IS NOT NULL AND dr.decision_trace_id = s.parent_trace_id
               ORDER BY pd.created_at DESC LIMIT 1) d ON true
           LEFT JOIN ai_decision_outcomes o ON o.decision_id = d.id AND o.horizon = $3
           WHERE s.created_at >= $1 AND ($2::text IS NULL OR s.role = $2)""",
        since, role, HORIZON)
    return [dict(r) for r in rows]


async def shadow_routes(conn) -> tuple[list[dict[str, Any]], bool]:
    """Routes with a shadow model (as the gateway resolves them) and whether paid shadows are allowed."""
    rows = await conn.fetch("SELECT * FROM ai_model_routes WHERE shadow_model IS NOT NULL ORDER BY role")
    try:
        allow_paid = bool(_truthy(await conn.fetchval(
            "SELECT value FROM system_settings WHERE key = 'AI_SHADOW_ALLOW_PAID'")))
    except Exception:
        allow_paid = False
    routes = []
    for row in rows:
        r = route_from_row(dict(row), DEFAULT_ROUTES.get(row["role"]))
        paid = not is_free(r.shadow_model or "")
        routes.append({
            "role": r.role, "primary_model": r.primary_model, "fallback_model": r.fallback_model,
            "fallback_policy": r.fallback_policy.value, "priority": r.priority.value,
            "shadow_model": r.shadow_model, "shadow_sample_rate": r.shadow_sample_rate,
            "shadow_paid": paid, "active": r.shadow_sample_rate > 0 and (allow_paid or not paid),
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        })
    return routes, allow_paid
