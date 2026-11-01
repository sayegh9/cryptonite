"""Setups inbox (AI Phase 2, Track C1): the fresh scanner candidates with their owner, cost and funnel trail.

Read-only and operator-only. Nothing here writes or proposes an order: the inbox only joins what other parts
of the system already recorded (``signal_candidates``, ``ai_profile_decisions.candidate_id`` and
``policy_decisions``). A candidate with no decision rows yet is normal.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

from cryptonite_api.routes.auth import require_operator
from cryptonite_scanner.costs import (
    COST_MODEL_VERSION,
    cost_in_r,
    cost_model_name,
    default_market,
    round_trip_cost_fraction,
)
from cryptonite_schemas.profile import (
    PRESET_CATALOG,
    base_asset,
    effective_presets,
    effective_timeframes,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request

router = APIRouter(prefix="/api/v1/setups", tags=["setups"])
UTC = dt.UTC

DEFAULT_WINDOW_HOURS = 6
# Carry for the cost estimate when the owning profile sets no max hold (only perps pay carry).
DEFAULT_HOLD_HOURS = 24.0
# Funnel order; LIFECYCLE, BREAKER and SESSION rows are profile-level events, not steps of a candidate.
FUNNEL_ORDER = ("SCAN", "GATE", "DEBATE", "ROUTE", "APPROVAL", "ORDER", "FILL", "EXIT")


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(Decimal(value), "f")


def _iso(value: dt.datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _owner_base(instrument_id: str) -> str | None:
    try:
        return base_asset(instrument_id)
    except ValueError:
        return None


def _direction(slug: str | None, entry: Decimal | None, stop: Decimal | None) -> str | None:
    if slug in PRESET_CATALOG:
        return PRESET_CATALOG[slug].side.value
    if entry is None or stop is None or entry == stop:
        return None
    return "LONG" if stop < entry else "SHORT"


def _eligibility(owner: dict[str, Any] | None, slug: str | None, timeframe: str) -> dict[str, Any]:
    """Whether the owning profile would run this candidate, with a stable refusal code when it would not."""
    if owner is None:
        return {"eligible": False, "reason": "NO_OWNER"}
    if owner["status"] != "ACTIVE":
        return {"eligible": False, "reason": f"PROFILE_{owner['status']}"}
    strategies = list(owner["strategies"] or [])
    if not slug or slug not in effective_presets(owner["style"], owner["direction"], strategies):
        return {"eligible": False, "reason": "PRESET_NOT_RUN"}
    if timeframe not in effective_timeframes(owner["style"], owner["direction"], strategies,
                                             list(owner["timeframes"] or [])):
        return {"eligible": False, "reason": "TIMEFRAME_NOT_RUN"}
    return {"eligible": True, "reason": None}


def _cost(direction: str | None, entry: Decimal | None, stop: Decimal | None,
          hold_hours: float) -> dict[str, Any]:
    market = default_market(direction)
    cost_r = None
    if entry is not None and stop is not None:
        cost_r = cost_in_r(float(entry), float(stop), round_trip_cost_fraction(market, hold_hours))
    return {
        "cost_r": None if cost_r is None else round(cost_r, 4),
        "cost_model": cost_model_name(market),
        "market": market,
        "hold_hours": hold_hours,
    }


def _funnel(rows: list[Any]) -> dict[str, Any]:
    trail = [
        {"id": r["id"], "stage": r["stage"], "outcome": r["outcome"], "reason": r["reason"],
         "mandate_id": str(r["mandate_id"]), "ts": _iso(r["ts"])}
        for r in rows
    ]
    steps = [t for t in trail if t["stage"] in FUNNEL_ORDER]
    furthest = max(steps, key=lambda t: (FUNNEL_ORDER.index(t["stage"]), t["ts"] or "", t["id"]), default=None)
    return {
        "furthest_stage": furthest["stage"] if furthest else None,
        "outcome": furthest["outcome"] if furthest else None,
        "reason": furthest["reason"] if furthest else None,
        "trail": trail,
    }


def _policy(row: Any) -> dict[str, Any]:
    return {
        "policy": row["policy"], "decision": row["decision"], "side": row["side"],
        "size_factor": _text(row["size_factor"]), "reason": row["reason"], "model": row["model"],
        "cost_usd": _text(row["cost_usd"]), "decided_at": _iso(row["decided_at"]),
    }


@router.get("")
async def list_setups(
    request: Request,
    since: dt.datetime | None = Query(default=None, description="ISO 8601; default the last 6 hours"),
    profile: uuid.UUID | None = Query(default=None, description="only the setups this profile owns"),
    limit: int = Query(default=200, ge=1, le=500),
    auth: dict[str, Any] = Depends(require_operator),
) -> dict[str, Any]:
    """Fresh candidates, newest first, each with provenance, the cost in R, its owner and its funnel trail."""
    now = dt.datetime.now(UTC)
    if since is None:
        since = now - dt.timedelta(hours=DEFAULT_WINDOW_HOURS)
    elif since.tzinfo is None:
        since = since.replace(tzinfo=UTC)

    async with request.app.state.pool.acquire() as conn:
        instrument_filter: list[str] | None = None
        if profile is not None:
            if await conn.fetchval("SELECT 1 FROM ai_mandates WHERE id = $1", profile) is None:
                raise HTTPException(status_code=404, detail="PROFILE_NOT_FOUND")
            owned = {r["base_asset"] for r in await conn.fetch(
                "SELECT base_asset FROM ai_profile_coin_locks WHERE mandate_id = $1", profile)}
            seen = [r["instrument_id"] for r in await conn.fetch(
                "SELECT DISTINCT instrument_id FROM signal_candidates WHERE created_at >= $1", since)]
            instrument_filter = [i for i in seen if _owner_base(i) in owned]

        if instrument_filter is not None and not instrument_filter:
            candidates: list[Any] = []
        else:
            candidates = await conn.fetch(
                """SELECT c.id, c.scan_run_id, c.spec_version_id, c.instrument_id, c.timeframe, c.matched_rules,
                          c.metrics, c.suggested_entry, c.suggested_stop, c.suggested_target, c.created_at,
                          s.slug AS spec_slug
                   FROM signal_candidates c
                   LEFT JOIN scan_spec_versions v ON v.id = c.spec_version_id
                   LEFT JOIN scan_specs s ON s.id = v.spec_id
                   WHERE c.created_at >= $1 AND ($2::text[] IS NULL OR c.instrument_id = ANY($2::text[]))
                   ORDER BY c.created_at DESC, c.id
                   LIMIT $3""",
                since, instrument_filter, limit)

        ids = [c["id"] for c in candidates]
        bases = sorted({b for c in candidates if (b := _owner_base(c["instrument_id"]))})
        owners = {r["base_asset"]: dict(r) for r in await conn.fetch(
            """SELECT l.base_asset, m.id, m.name, m.status, m.style, m.direction, m.strategies, m.timeframes,
                      m.exit_max_hold_hours
               FROM ai_profile_coin_locks l JOIN ai_mandates m ON m.id = l.mandate_id
               WHERE l.base_asset = ANY($1::text[])""", bases)}
        decisions: dict[uuid.UUID, list[Any]] = {}
        for r in await conn.fetch(
            """SELECT id, candidate_id, mandate_id, stage, outcome, reason, ts FROM ai_profile_decisions
               WHERE candidate_id = ANY($1::uuid[]) ORDER BY ts, id""", ids):
            decisions.setdefault(r["candidate_id"], []).append(r)
        policies: dict[uuid.UUID, list[Any]] = {}
        for r in await conn.fetch(
            """SELECT candidate_id, policy, decision, side, size_factor, reason, model, cost_usd, decided_at
               FROM policy_decisions WHERE candidate_id = ANY($1::uuid[]) ORDER BY policy""", ids):
            policies.setdefault(r["candidate_id"], []).append(r)

    setups: list[dict[str, Any]] = []
    for c in candidates:
        metrics = _json(c["metrics"]) or {}
        slug = c["spec_slug"] or metrics.get("preset_slug") or metrics.get("spec_slug")
        entry, stop, target = c["suggested_entry"], c["suggested_stop"], c["suggested_target"]
        direction = _direction(slug, entry, stop)
        owner = owners.get(_owner_base(c["instrument_id"]) or "")
        hold = float(owner["exit_max_hold_hours"]) if owner and owner["exit_max_hold_hours"] else DEFAULT_HOLD_HOURS
        setups.append({
            "id": str(c["id"]),
            "instrument_id": c["instrument_id"],
            "provenance": {
                "preset": slug, "timeframe": c["timeframe"],
                "scan_run_id": str(c["scan_run_id"]), "spec_version_id": str(c["spec_version_id"]),
            },
            "direction": direction,
            "entry": _text(entry), "stop": _text(stop), "target": _text(target),
            "created_at": _iso(c["created_at"]),
            "age_seconds": max(0, int((now - c["created_at"]).total_seconds())),
            "cost": _cost(direction, entry, stop, hold),
            "profile": None if owner is None else {
                "id": str(owner["id"]), "name": owner["name"], "status": owner["status"],
                **_eligibility(owner, slug, c["timeframe"]),
            },
            "eligibility": _eligibility(owner, slug, c["timeframe"]),
            "funnel": _funnel(decisions.get(c["id"], [])),
            "policies": [_policy(p) for p in policies.get(c["id"], [])],
            "brief": {"matched_rules": _json(c["matched_rules"]) or [], "metrics": metrics},
        })
    return {
        "since": _iso(since), "count": len(setups), "cost_model_version": COST_MODEL_VERSION, "setups": setups,
    }
