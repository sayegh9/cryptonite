"""Gate metrics for the autonomy ladder, measured from the database (AI-first blueprint §4.5, A5.5).

Everything is measured since the mandate reached its current level (``level_since``):

- days at level; closed trades (spot sells and closed perp positions of the mandate's instance);
- max drawdown from ``ai_mandate_equity``; mandate breaches (``BREACH`` events);
- Sortino of daily mandate equity vs Sortino of BTC buy-and-hold over the same days (the tracker
  stores the BTC price with every snapshot); fees are already inside mandate equity;
- veto rate: vetoed / decided ADD_RISK approvals created at L2;
- calibration: not measured before A7.2 (``None``), so the L3 gate fails closed.

``propose_if_ready`` records a system promotion proposal when the gate passes (never a promotion).
"""

import datetime
import json
import math
from decimal import Decimal
from typing import Any

from cryptonite_schemas.mandate import AutonomyLevel, Mandate, MandateStatus

from cryptonite_execution.autonomy import (
    GateMetrics,
    GateReport,
    evaluate_gate,
    period_returns,
    sortino,
)
from cryptonite_execution.mandate_store import record_event


async def gate_metrics(conn, mandate: Mandate, now: datetime.datetime | None = None) -> GateMetrics:
    now = now or datetime.datetime.now(datetime.UTC)
    since = await conn.fetchval("SELECT level_since FROM ai_mandates WHERE id = $1", mandate.id)
    days = max(0.0, (now - since).total_seconds() / 86400)
    closed = await conn.fetchval(
        """SELECT (SELECT count(*) FROM paper_trades WHERE instance_id = $1 AND side = 'SELL' AND executed_at >= $2
                   AND instrument_id NOT LIKE '%-PERP.%')
                + (SELECT count(*) FROM paper_positions WHERE instance_id = $1 AND status = 'CLOSED' AND closed_at >= $2
                   AND instrument_id LIKE '%-PERP.%')""",
        mandate.instance_id, since)
    max_dd = await conn.fetchval(
        "SELECT max(drawdown_pct) FROM ai_mandate_equity WHERE mandate_id = $1 AND ts >= $2", mandate.id, since)
    breaches = await conn.fetchval(
        "SELECT count(*) FROM ai_autonomy_events WHERE mandate_id = $1 AND kind = 'BREACH' AND created_at >= $2",
        mandate.id, since)
    daily = await conn.fetch(
        """SELECT DISTINCT ON (ts::date) ts::date AS day, equity, benchmark_price FROM ai_mandate_equity
           WHERE mandate_id = $1 AND ts >= $2 ORDER BY ts::date, ts DESC""", mandate.id, since)
    eq = [float(r["equity"]) for r in daily]
    bench = [float(r["benchmark_price"]) for r in daily if r["benchmark_price"] is not None]
    decided = await conn.fetchrow(
        """SELECT count(*) FILTER (WHERE status = 'VETOED') AS vetoed,
                  count(*) FILTER (WHERE status IN ('APPROVED', 'VETOED', 'EXPIRED_PROCEEDED')) AS decided
           FROM ai_approvals WHERE mandate_id = $1 AND action_class = 'ADD_RISK' AND level_at_creation = 2 AND created_at >= $2""",
        mandate.id, since)
    veto_rate = (Decimal(decided["vetoed"]) / Decimal(decided["decided"]) * 100) if decided["decided"] else None
    return GateMetrics(
        days_at_level=days, closed_trades=int(closed or 0),
        max_drawdown_pct=Decimal(str(max_dd)) if max_dd is not None else None, breaches=int(breaches or 0),
        sortino=sortino(period_returns(eq)),
        benchmark_sortino=sortino(period_returns(bench)) if len(bench) == len(eq) else None,
        honest_paper=True, calibration_error_pp=None, veto_rate_pct=veto_rate,
    )


async def gate_report(conn, mandate: Mandate, now: datetime.datetime | None = None) -> tuple[GateReport, GateMetrics]:
    metrics = await gate_metrics(conn, mandate, now)
    return evaluate_gate(mandate.autonomy_level, metrics), metrics


async def propose_if_ready(conn, mandate: Mandate, now: datetime.datetime | None = None) -> int | None:
    """The system proposes a promotion when the gate passes; the operator confirms it with TOTP.
    Returns the proposal event id (an already open proposal is returned, not duplicated)."""
    if mandate.status is not MandateStatus.ACTIVE or mandate.autonomy_level >= AutonomyLevel.L3:
        return None
    open_id = await conn.fetchval(
        "SELECT id FROM ai_autonomy_events WHERE mandate_id = $1 AND kind = 'PROPOSED' AND proposal_status = 'PENDING'",
        mandate.id)
    if open_id:
        return open_id
    report, _ = await gate_report(conn, mandate, now)
    if not report.passed:
        return None
    event_id = await record_event(
        conn, mandate.id, "PROPOSED", from_level=int(mandate.autonomy_level), to_level=int(report.target_level),
        reason=f"gate for L{int(report.target_level)} passed", detail={"gate": report.as_dict()}, actor="system",
        proposal_status="PENDING")
    await conn.execute(
        """INSERT INTO notifications_outbox (tier, kind, title, body, payload, dedupe_key)
           VALUES ('INFO', 'PROMOTION_PROPOSED', $1, $2, $3::jsonb, $4) ON CONFLICT (dedupe_key) DO NOTHING""",
        f"{mandate.name}: promotion to L{int(report.target_level)} proposed",
        "The gate passed. Confirm it in Mission Control → Autonomy & Evals with your TOTP; nothing changes until you do.",
        json.dumps({"mandate_id": str(mandate.id), "proposal_id": event_id}), f"proposal:{event_id}")
    return event_id


def _num(x: float | None) -> float | str | None:
    if x is None:
        return None
    return ("inf" if x > 0 else "-inf") if math.isinf(x) else round(x, 3)


def report_view(report: GateReport, metrics: GateMetrics) -> dict[str, Any]:
    return {**report.as_dict(), "metrics": {
        "days_at_level": round(metrics.days_at_level, 2), "closed_trades": metrics.closed_trades,
        "max_drawdown_pct": str(metrics.max_drawdown_pct) if metrics.max_drawdown_pct is not None else None,
        "breaches": metrics.breaches, "sortino": _num(metrics.sortino), "benchmark_sortino": _num(metrics.benchmark_sortino),
        "calibration_error_pp": str(metrics.calibration_error_pp) if metrics.calibration_error_pp is not None else None,
        "veto_rate_pct": str(round(metrics.veto_rate_pct, 2)) if metrics.veto_rate_pct is not None else None,
    }}

