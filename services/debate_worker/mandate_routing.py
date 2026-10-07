"""Where a debate verdict goes (AI-first blueprint §4.5, D8, A5.7). Replaces DEBATE_EXECUTION_MODE.

Autonomous execution exists only inside a mandate, at the level it has earned:

- no ACTIVE mandate allows the instrument -> the verdict waits for the operator (PENDING_APPROVAL);
- L1 Copilot -> an ``ai_approvals`` row: ADD_RISK times out as *no*;
- L2 Supervised -> an ``ai_approvals`` veto window: ADD_RISK proceeds at timeout only inside the
  operator's active hours; outside them it times out as *no*;
- L3 Autonomous -> the order is queued at once (the mandate check and the dispatcher still apply);
- risk reducing (a spot SELL of what the mandate holds, an exit debate's REDUCE/CLOSE) runs at once
  at L2/L3; at L1 it waits a short veto window that *proceeds* at timeout (D8).

The allocation is capped at the room the mandate has left under its own position limit and budget
(what it already holds counts) before anything is queued or sent for approval, so a model never
"breaches" by over-sizing; with less than ``MIN_MANDATE_ENTRY_PCT`` of room there is no trade. The
orchestrator sizes the order to the exact room again at execution. This module writes the same rows ``cryptonite_execution.approvals``
writes; the orchestrator's sweeper and the API decide them. (The debate worker image does not carry
the execution package, only ``cryptonite_schemas``.)
"""

import datetime
import json
import uuid
from decimal import Decimal
from typing import Any

from cryptonite_schemas.debate import verdict_direction
from cryptonite_schemas.mandate import (
    DEFAULT_APPROVAL_TTL_S,
    DEFAULT_REDUCE_VETO_S,
    MIN_MANDATE_ENTRY_PCT,
    ActionClass,
    AutonomyLevel,
    Mandate,
    canonical_instrument,
    in_active_hours,
    mandate_id_from_instance,
    timeout_policy_for,
)
from cryptonite_schemas.marks import LiveMark
from cryptonite_schemas.marks import entry_room_pct as room_at_marks

PAPER_ACCOUNT_ID = "PAPER-ACCOUNT-001"
MANDATE_COLUMNS = ("id, name, capital_budget_usd, instruments, max_position_pct, max_drawdown_pct, allow_perps, "
                   "max_leverage, strategies, autonomy_level, status, active_hours_utc, veto_window_s")


def side_of(recommendation: str) -> str | None:
    """The order side of a verdict (``normalize_verdict``'s mapping; "strong buy" with a space is
    STRONG_BUY); None for HOLD or an unknown verdict."""
    direction = verdict_direction(str(recommendation or "").strip().replace(" ", "_"))
    return direction if direction in ("BUY", "SELL") else None


async def resolve_mandate(conn, instrument_id: str, mandate_id: str | None = None) -> Mandate | None:
    """The named mandate when it is ACTIVE and allows the instrument, else the single ACTIVE one that does."""
    if mandate_id:
        row = await conn.fetchrow(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE id = $1", uuid.UUID(str(mandate_id)))
        m = Mandate.from_row(row) if row else None
        return m if m and m.status.value == "ACTIVE" and m.allows_instrument(instrument_id) else None
    rows = await conn.fetch(
        f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE status = 'ACTIVE' AND $1 = ANY(instruments) ORDER BY created_at",
        canonical_instrument(instrument_id))
    return Mandate.from_row(rows[0]) if len(rows) == 1 else None


async def mandate_holding(conn, mandate: Mandate, instrument_id: str) -> Decimal:
    qty = await conn.fetchval(
        """SELECT COALESCE(SUM(quantity), 0) FROM paper_positions
           WHERE account_id = $1 AND instance_id = $2 AND instrument_id = $3 AND status = 'OPEN'""",
        PAPER_ACCOUNT_ID, mandate.instance_id, canonical_instrument(instrument_id))
    return Decimal(str(qty or 0))


async def entry_room_pct(conn, mandate: Mandate, instrument_id: str, marks: dict[str, LiveMark] | None = None) -> Decimal:
    """Room for a new entry, as % of the mandate budget: under the instrument's position limit and under
    the budget, with the mandate's open positions at their live mark (``marks``, from the orchestrator's
    projector), else at their stored mark (B3)."""
    rows = await conn.fetch(
        """SELECT instrument_id, quantity, current_price FROM paper_positions
           WHERE account_id = $1 AND instance_id = $2 AND status = 'OPEN' AND quantity > 0""",
        PAPER_ACCOUNT_ID, mandate.instance_id)
    return room_at_marks(mandate.capital_budget_usd, mandate.max_position_pct, rows, instrument_id, marks)


async def position_owner(conn, instrument_id: str) -> tuple[Mandate | None, Decimal]:
    """The mandate that alone holds the instrument (exit debates act only on those)."""
    rows = await conn.fetch(
        """SELECT instance_id, SUM(quantity) AS qty FROM paper_positions
           WHERE account_id = $1 AND instrument_id = $2 AND status = 'OPEN' AND quantity > 0 GROUP BY instance_id""",
        PAPER_ACCOUNT_ID, canonical_instrument(instrument_id))
    if len(rows) != 1 or (mid := mandate_id_from_instance(rows[0]["instance_id"])) is None:
        return None, Decimal("0")
    row = await conn.fetchrow(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE id = $1", mid)
    return (Mandate.from_row(row) if row else None), Decimal(str(rows[0]["qty"]))


async def _approval(conn, mandate: Mandate, action: ActionClass, command_type: str, payload: dict[str, Any],
                    summary: str, source: str, run_id: uuid.UUID, now: datetime.datetime) -> uuid.UUID | None:
    policy = timeout_policy_for(action, mandate.autonomy_level, in_active_hours(mandate.active_hours_utc, now))
    if policy is None:
        return None
    ttl = (DEFAULT_REDUCE_VETO_S if action is ActionClass.REDUCE_RISK
           else mandate.veto_window_s if mandate.autonomy_level == AutonomyLevel.L2 else DEFAULT_APPROVAL_TTL_S)
    approval_id = await conn.fetchval(
        """INSERT INTO ai_approvals (mandate_id, action_class, command_type, payload, summary, source, source_ref,
               level_at_creation, timeout_policy, expires_at)
           VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10)
           ON CONFLICT (source, source_ref) DO NOTHING RETURNING id""",
        mandate.id, action.value, command_type, json.dumps({**payload, "mandate_id": str(mandate.id)}, default=str),
        summary, source, str(run_id), int(mandate.autonomy_level), policy.value,
        now + datetime.timedelta(seconds=ttl))
    if approval_id is not None:
        await conn.execute(
            """INSERT INTO notifications_outbox (tier, kind, title, body, payload, approval_id, dedupe_key)
               VALUES ('ACTION', 'APPROVAL', $1, $2, $3::jsonb, $4, $5) ON CONFLICT (dedupe_key) DO NOTHING""",
            f"{mandate.name} (L{int(mandate.autonomy_level)})", summary,
            json.dumps({"mandate": mandate.name, "action_class": action.value, "timeout_policy": policy.value,
                        "run_id": str(run_id)}),
            approval_id, f"approval:{approval_id}")
    return approval_id


def _trace_uuid(payload: dict[str, Any]) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(payload["decision_trace_id"])) if payload.get("decision_trace_id") else None
    except ValueError:
        return None


async def _enqueue(conn, mandate: Mandate, command_type: str, payload: dict[str, Any], key: str) -> None:
    # decision_trace_id is written at insert: the API role cannot UPDATE execution_commands (R7.4)
    await conn.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, requester_id,
               payload, status, environment, account_id, mandate_id, decision_trace_id)
           VALUES ($1, $2, $3, $4, 'ai:debate_worker', $5::jsonb, 'PENDING', 'PAPER', $6, $7, $8)
           ON CONFLICT (idempotency_key) DO NOTHING""",
        uuid.uuid4(), key, command_type, mandate.instance_id,
        json.dumps({**payload, "mandate_id": str(mandate.id), "account_id": PAPER_ACCOUNT_ID}, default=str),
        PAPER_ACCOUNT_ID, mandate.id, _trace_uuid(payload))


async def route_entry(conn, run_id: uuid.UUID, instrument_id: str, recommendation: str, conviction: float,
                      allocation_pct: float, *, mandate_id: str | None = None,
                      now: datetime.datetime | None = None,
                      decision_trace_id: str | None = None,
                      marks: dict[str, LiveMark] | None = None) -> tuple[str, Mandate | None]:
    """Returns (debate_runs status, mandate). ``decision_trace_id`` (R7.4) rides in the command and
    approval payloads so the order and its fill can be traced back to the scanner candidate. ``marks``
    (``cryptonite_schemas.marks.read_marks``) values the mandate's positions for its room."""
    now = now or datetime.datetime.now(datetime.UTC)
    side = side_of(recommendation)
    if side is None:
        return "NO_TRADE", None
    mandate = await resolve_mandate(conn, instrument_id, mandate_id)
    if mandate is None:
        return "PENDING_APPROVAL", None  # outside any mandate: only the operator can act on it
    base = {"instrument_id": canonical_instrument(instrument_id), "side": side, "order_type": "MARKET",
            "recommendation": recommendation, "conviction_score": conviction, "source": "debate", "run_id": str(run_id)}
    if decision_trace_id:
        base["decision_trace_id"] = str(decision_trace_id)
    if side == "BUY":
        room = await entry_room_pct(conn, mandate, instrument_id, marks)
        if room < MIN_MANDATE_ENTRY_PCT:
            return "NO_TRADE", mandate  # already at the position limit: nothing to ask the operator
        alloc = round(min(float(allocation_pct), float(mandate.max_position_pct), float(room)), 2)
        payload = {**base, "allocation_pct": alloc}
        summary = (f"BUY {base['instrument_id']} {alloc:g}% of mandate "
                   f"(${float(mandate.capital_budget_usd) * alloc / 100:,.0f}) · conviction {conviction:.2f}")
        action = ActionClass.ADD_RISK
    else:
        held = await mandate_holding(conn, mandate, instrument_id)
        if held <= 0:
            return "NO_TRADE", mandate  # spot is long-only: nothing of the mandate's to sell
        payload = {**base, "quantity": str(held)}
        summary = f"SELL {held} {base['instrument_id']} (the mandate's position) · conviction {conviction:.2f}"
        action = ActionClass.REDUCE_RISK
    if action is ActionClass.REDUCE_RISK and mandate.autonomy_level >= AutonomyLevel.L2:
        await _enqueue(conn, mandate, "SUBMIT_ORDER", payload, f"debate:{run_id}")
        return "COMPLETED", mandate
    approval = await _approval(conn, mandate, action, "SUBMIT_ORDER", payload, summary, "debate", run_id, now)
    if approval is None:  # ADD_RISK at L3: no approval
        await _enqueue(conn, mandate, "SUBMIT_ORDER", payload, f"debate:{run_id}")
        return "COMPLETED", mandate
    return "PENDING_APPROVAL", mandate


async def route_exit(conn, run_id: uuid.UUID, instrument_id: str, recommendation: str,
                     now: datetime.datetime | None = None) -> tuple[str, Mandate | None]:
    """An exit debate's REDUCE/CLOSE on a mandate's position: immediate at L2/L3, a veto window that
    proceeds at L1. Anything else waits for the operator."""
    now = now or datetime.datetime.now(datetime.UTC)
    rec = str(recommendation or "").upper()
    if rec not in ("REDUCE", "CLOSE"):
        return "PENDING_APPROVAL", None
    mandate, held = await position_owner(conn, instrument_id)
    if mandate is None or held <= 0:
        return "PENDING_APPROVAL", None
    qty = held if rec == "CLOSE" else (held / 2)
    payload = {"instrument_id": canonical_instrument(instrument_id), "quantity": str(qty), "reason": f"EXIT_DEBATE_{rec}",
               "source": "exit_debate", "run_id": str(run_id)}
    if mandate.autonomy_level >= AutonomyLevel.L2:
        await _enqueue(conn, mandate, "CLOSE_POSITION", payload, f"exit_debate:{run_id}")
        return "COMPLETED", mandate
    await _approval(conn, mandate, ActionClass.REDUCE_RISK, "CLOSE_POSITION", payload,
                    f"{rec} {qty} {payload['instrument_id']} (exit debate)", "exit_debate", run_id, now)
    return "PENDING_APPROVAL", mandate
