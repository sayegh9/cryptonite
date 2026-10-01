"""Mandate persistence shared by the orchestrator and the API (asyncpg connections).

Autonomy transitions are computed by ``cryptonite_execution.autonomy`` and persisted here: one
``ai_mandates`` update and one ``ai_autonomy_events`` row, in the caller's transaction.
"""

import json
import uuid
from typing import Any

from cryptonite_schemas.mandate import Mandate, MandateStatus, canonical_instrument

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
