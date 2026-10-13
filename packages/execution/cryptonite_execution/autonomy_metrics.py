"""Gate metrics for the autonomy ladder, measured from the database (AI-first blueprint §4.5, A5.5).

Everything is measured since the mandate reached its current level (``level_since``):

- days at level; closed trades (spot sells and closed perp positions of the mandate's instance);
- max drawdown from ``ai_mandate_equity``; mandate breaches (``BREACH`` events);
- Sortino of daily mandate equity vs Sortino of BTC buy-and-hold over the same days (the tracker
  stores the BTC price with every snapshot); fees are already inside mandate equity;
- veto rate: vetoed / decided ADD_RISK approvals created at L2;
- calibration (A7.2): expected calibration error of the mandate's verdict conviction against 24h
  outcomes (``calibration.py``); ``None`` below the minimum sample, so the L3 gate fails closed.
- informational (R4.2/R4.3): per-trade returns after fees (spot round trips at average cost from
  ``paper_trades``, closed perp positions from ``paper_positions``), the paired daily returns behind
  the Sortino check, and the ECE interval; they feed the interval checks, never ``passed``.
- honest paper (A0.4, R4.6): derived from what the database records (every fill of the mandate's
  instance in the window charged a commission), otherwise "not measured". It is never derived as a
  failing value before D6.

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
from cryptonite_execution.calibration import calibration_error_pp, calibration_interval_pp
from cryptonite_execution.mandate_store import record_event
from cryptonite_execution.stats import wilson_interval


def spot_trade_returns(fills: list[dict[str, Any]]) -> list[float]:
    """Round-trip returns after fees from time-ordered spot fills (instrument_id, side, price, quantity,
    commission in quote). Average cost per instrument; a SELL closes at most what was bought in the
    window (holdings from before it are not counted)."""
    held: dict[str, tuple[float, float]] = {}  # instrument -> (qty, cost incl. buy fees)
    out: list[float] = []
    for f in fills:
        inst, qty, px, fee = f["instrument_id"], float(f["quantity"]), float(f["price"]), float(f["commission"] or 0)
        q0, c0 = held.get(inst, (0.0, 0.0))
        if f["side"] == "BUY":
            held[inst] = (q0 + qty, c0 + qty * px + fee)
        elif q0 > 0 and qty > 0:
            matched = min(qty, q0)
            cost = c0 * matched / q0
            proceeds = matched * px - fee * matched / qty
            out.append(proceeds / cost - 1 if cost > 0 else 0.0)
            held[inst] = (q0 - matched, c0 - cost)
    return out


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
    # A breach set aside by a BREACH_VOIDED event (migration 045) stays in the audit log but does not count
    breaches = await conn.fetchval(
        """SELECT count(*) FROM ai_autonomy_events b
           WHERE b.mandate_id = $1 AND b.kind = 'BREACH' AND b.created_at >= $2
             AND NOT EXISTS (SELECT 1 FROM ai_autonomy_events v
                             WHERE v.mandate_id = b.mandate_id AND v.kind = 'BREACH_VOIDED'
                               AND v.detail->>'voids' = b.id::text)""",
        mandate.id, since)
    daily = await conn.fetch(
        """SELECT DISTINCT ON (ts::date) ts::date AS day, equity, benchmark_price FROM ai_mandate_equity
           WHERE mandate_id = $1 AND ts >= $2 ORDER BY ts::date, ts DESC""", mandate.id, since)
    eq = [float(r["equity"]) for r in daily]
    bench = [float(r["benchmark_price"]) for r in daily if r["benchmark_price"] is not None]
    fills = await conn.fetch(
        """SELECT instrument_id, side, price, quantity, commission FROM paper_trades
           WHERE instance_id = $1 AND executed_at >= $2 AND instrument_id NOT LIKE '%-PERP.%'
           ORDER BY executed_at, trade_id""", mandate.instance_id, since)
    perp = await conn.fetch(
        """SELECT realized_pnl, entry_price, quantity FROM paper_positions
           WHERE instance_id = $1 AND status = 'CLOSED' AND closed_at >= $2 AND instrument_id LIKE '%-PERP.%'""",
        mandate.instance_id, since)
    trade_returns = spot_trade_returns([dict(r) for r in fills]) + [
        float(r["realized_pnl"]) / (float(r["entry_price"]) * float(r["quantity"]))
        for r in perp if r["realized_pnl"] is not None and r["entry_price"] and r["quantity"]]
    fee_check = await conn.fetchrow(
        """SELECT count(*) AS n, count(*) FILTER (WHERE commission IS NULL OR commission <= 0) AS free
           FROM paper_trades WHERE instance_id = $1 AND executed_at >= $2""", mandate.instance_id, since)
    if fee_check["n"] and not fee_check["free"]:
        honest, honest_detail = True, f"{fee_check['n']} fills, all with fees"
    elif fee_check["n"]:  # reported, not failed, until D6 (R4.6)
        honest, honest_detail = None, f"{fee_check['free']} of {fee_check['n']} fills without fees"
    else:
        honest, honest_detail = None, "no fills yet"
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
        honest_paper=honest, honest_paper_detail=honest_detail,
        calibration_error_pp=await calibration_error_pp(conn, mandate.id, since),
        veto_rate_pct=veto_rate,
        trade_returns=tuple(trade_returns), daily_returns=tuple(period_returns(eq)),
        benchmark_returns=tuple(period_returns(bench)) if len(bench) == len(eq) else (),
        calibration_interval_pp=await calibration_interval_pp(conn, mandate.id, since),
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
    n = len(metrics.trade_returns)
    wins = sum(1 for r in metrics.trade_returns if r > 0)
    win_ci = wilson_interval(wins, n)
    return {**report.as_dict(), "metrics": {
        "win_rate": round(wins / n, 4) if n else None,
        "win_rate_interval": [f"{win_ci[0]:.4f}", f"{win_ci[1]:.4f}"] if win_ci else None,
        "honest_paper": metrics.honest_paper, "honest_paper_detail": metrics.honest_paper_detail,
        "days_at_level": round(metrics.days_at_level, 2), "closed_trades": metrics.closed_trades,
        "max_drawdown_pct": str(metrics.max_drawdown_pct) if metrics.max_drawdown_pct is not None else None,
        "breaches": metrics.breaches, "sortino": _num(metrics.sortino), "benchmark_sortino": _num(metrics.benchmark_sortino),
        "calibration_error_pp": str(metrics.calibration_error_pp) if metrics.calibration_error_pp is not None else None,
        "veto_rate_pct": str(round(metrics.veto_rate_pct, 2)) if metrics.veto_rate_pct is not None else None,
    }}

