"""Mandate persistence shared by the orchestrator and the API (asyncpg connections).

Autonomy transitions are computed by ``cryptonite_execution.autonomy`` and persisted here: one
``ai_mandates`` update and one ``ai_autonomy_events`` row, in the caller's transaction.
"""

import json
import uuid
from decimal import Decimal
from typing import Any

from cryptonite_schemas.mandate import Mandate, MandateStatus, canonical_instrument
from cryptonite_schemas.marks import LiveMark
from cryptonite_schemas.marks import entry_room_pct as room_at_marks

from cryptonite_execution.autonomy import Transition

MANDATE_COLUMNS = ("id, name, capital_budget_usd, instruments, max_position_pct, max_drawdown_pct, allow_perps, "
                   "max_leverage, strategies, autonomy_level, status, active_hours_utc, veto_window_s")


async def load_mandate(conn, mandate_id: str | uuid.UUID, *, for_update: bool = False) -> Mandate | None:
    try:
        mid = uuid.UUID(str(mandate_id))
    except ValueError:
        return None
    lock = " FOR UPDATE" if for_update else ""
    row = await conn.fetchrow(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE id = $1{lock}", mid)
    return Mandate.from_row(row) if row else None


async def record_event(conn, mandate_id: uuid.UUID, kind: str, *, from_level: int | None = None,
                       to_level: int | None = None, reason: str = "", detail: dict[str, Any] | None = None,
                       actor: str = "system", proposal_status: str | None = None) -> int:
    return await conn.fetchval(
        """INSERT INTO ai_autonomy_events (mandate_id, kind, from_level, to_level, reason, detail, actor, proposal_status)
           VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8) RETURNING id""",
        mandate_id, kind, from_level, to_level, reason, json.dumps(detail or {}, default=str), actor, proposal_status,
    )


class BreachVoidError(ValueError):
    """The event cannot be voided: not a BREACH of this mandate, or voided already."""


async def void_breach(conn, mandate_id: uuid.UUID, breach_event_id: int, *, reason: str, actor: str) -> int:
    """Set a breach aside (migration 045): a BREACH_VOIDED event that names it. The BREACH row is never edited or
    deleted. For breaches the system caused (a sizing fault), not for the AI overstepping. Returns the event id."""
    if not reason.strip():
        raise BreachVoidError("a reason is required")
    row = await conn.fetchrow("SELECT mandate_id, kind, reason, detail FROM ai_autonomy_events WHERE id = $1",
                              breach_event_id)
    if row is None or row["mandate_id"] != mandate_id or row["kind"] != "BREACH":
        raise BreachVoidError(f"event {breach_event_id} is not a BREACH of mandate {mandate_id}")
    if await conn.fetchval("SELECT 1 FROM ai_autonomy_events WHERE kind = 'BREACH_VOIDED' AND detail->>'voids' = $1",
                           str(breach_event_id)):
        raise BreachVoidError(f"breach {breach_event_id} is already voided")
    detail = row["detail"] if isinstance(row["detail"], dict) else json.loads(row["detail"] or "{}")
    return await record_event(conn, mandate_id, "BREACH_VOIDED", reason=reason.strip(), actor=actor,
                              detail={"voids": str(breach_event_id), "breach_reason": row["reason"],
                                      "command_id": detail.get("command_id")})


async def apply_transition(conn, mandate_id: uuid.UUID, t: Transition, *, actor: str = "system",
                           detail: dict[str, Any] | None = None) -> int | None:
    """Persist a transition. A NOOP writes nothing. Returns the event id."""
    if t.kind == "NOOP":
        return None
    halted = t.after.status is MandateStatus.HALTED and t.before.status is not MandateStatus.HALTED
    resumed = t.kind == "RESUMED"
    await conn.execute(
        """UPDATE ai_mandates SET
               autonomy_level = $2, status = $3, allow_perps = $4,
               level_since = CASE WHEN autonomy_level <> $2 THEN now() ELSE level_since END,
               halted_reason = CASE WHEN $5 THEN $6 WHEN $7 THEN NULL ELSE halted_reason END,
               halted_at = CASE WHEN $5 THEN now() WHEN $7 THEN NULL ELSE halted_at END,
               hwm_rebased_at = CASE WHEN $7 THEN now() ELSE hwm_rebased_at END,
               last_dd_demotion_hwm = CASE WHEN $7 THEN NULL ELSE last_dd_demotion_hwm END,
               updated_at = now()
           WHERE id = $1""",
        mandate_id, int(t.after.level), t.after.status.value, t.after.allow_perps, halted, t.reason, resumed,
    )
    if t.level_changed or t.after.status is not t.before.status:
        # a pending promotion proposal is stale once the level or the status moved
        await conn.execute(
            """UPDATE ai_autonomy_events SET proposal_status = 'CANCELLED', resolved_at = now()
               WHERE mandate_id = $1 AND kind = 'PROPOSED' AND proposal_status = 'PENDING'""",
            mandate_id,
        )
    return await record_event(conn, mandate_id, t.kind, from_level=int(t.before.level), to_level=int(t.after.level),
                              reason=t.reason, detail=detail, actor=actor)


def level_name(level: int) -> str:
    return {1: "L1 Copilot", 2: "L2 Supervised", 3: "L3 Autonomous"}.get(int(level), f"L{level}")



async def resolve_mandate_for(conn, instrument_id: str, mandate_id: str | uuid.UUID | None = None) -> tuple[Mandate | None, str]:
    """The mandate an AI order on ``instrument_id`` belongs to: the one named, else the single ACTIVE
    mandate that allows the instrument. Returns (mandate, "") or (None, reason)."""
    if mandate_id:
        m = await load_mandate(conn, mandate_id)
        if m is None:
            return None, "MANDATE_NOT_FOUND"
        if m.status is not MandateStatus.ACTIVE:
            return None, "MANDATE_HALTED"
        if not m.allows_instrument(instrument_id):
            return None, "MANDATE_INSTRUMENT_NOT_ALLOWED"
        return m, ""
    rows = await conn.fetch(
        f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE status = 'ACTIVE' AND $1 = ANY(instruments) ORDER BY created_at",
        canonical_instrument(instrument_id),
    )
    if not rows:
        return None, "NO_MANDATE"
    if len(rows) > 1:
        return None, "AMBIGUOUS_MANDATE"
    return Mandate.from_row(rows[0]), ""


async def entry_room_pct(conn, mandate: Mandate, instrument_id: str, marks: dict[str, LiveMark] | None = None) -> Decimal:
    """Room for a new entry, as % of the mandate budget: under the instrument's position limit and under
    the budget, with the mandate's open positions at their live mark (``marks``, B3), else at their last
    stored mark. (The orchestrator sizes the order to the exact room at execution; this is for deciding
    whether to ask at all.)"""
    rows = await conn.fetch(
        """SELECT instrument_id, quantity, current_price FROM paper_positions
           WHERE instance_id = $1 AND status = 'OPEN' AND quantity > 0""",
        mandate.instance_id)
    return room_at_marks(mandate.capital_budget_usd, mandate.max_position_pct, rows, instrument_id, marks)


async def instance_spot_holdings(conn, instance_id: str) -> dict[str, tuple[Decimal, Decimal]]:
    """Net filled spot quantity (and average fill price) per instrument for one instance, from
    ``paper_orders``. The orchestrator writes those rows inside the claim transaction of every fill and
    close, so unlike ``paper_positions`` (written later by the event bridge) they are never stale."""
    rows = await conn.fetch(
        """SELECT instrument_id,
                  SUM(CASE WHEN side = 'BUY' THEN filled_qty ELSE -filled_qty END) AS net,
                  SUM(CASE WHEN side = 'BUY' THEN filled_qty * COALESCE(avg_fill_price, price, 0) ELSE 0 END)
                      / NULLIF(SUM(CASE WHEN side = 'BUY' THEN filled_qty ELSE 0 END), 0) AS avg_px
           FROM paper_orders
           WHERE instance_id = $1 AND filled_qty > 0 AND instrument_id NOT LIKE '%-PERP.%'
           GROUP BY instrument_id""",
        instance_id,
    )
    return {r["instrument_id"]: (max(Decimal(str(r["net"] or 0)), Decimal("0")), Decimal(str(r["avg_px"] or 0))) for r in rows}
