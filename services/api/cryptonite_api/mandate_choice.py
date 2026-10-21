"""Choosing the mandate an AI entry trades in, shared by the Brain order card and the Inbox debate approval.

When more than one ACTIVE mandate allows an instrument, ``resolve_mandate_for`` returns AMBIGUOUS_MANDATE and approve
needs one named. ``choose_mandate_remedy`` lists the candidates with what the entry would size to in each, and
``instrument_conflict`` refuses an entry in a mandate when another owner already holds the instrument, as the
orchestrator's mandate gate would, so a choice that cannot trade is refused before anything is queued.
"""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from cryptonite_execution.mandate_store import MANDATE_COLUMNS, entry_room_pct, held_values
from cryptonite_execution.mandates import MandateOrder, check_mandate
from cryptonite_orchestrator.mandate_gate import mandate_exposure
from cryptonite_schemas.mandate import (
    MIN_MANDATE_ENTRY_PCT,
    Mandate,
    MandateReject,
    canonical_instrument,
    mandate_id_from_instance,
)

PAPER_ACCOUNT_ID = "PAPER-ACCOUNT-001"


async def instrument_conflict(conn, mandate: Mandate, instrument_id: str, quantity: Decimal, what: str) -> str | None:
    """The MANDATE_INSTRUMENT_CONFLICT detail when another owner holds the instrument, else None. ``what`` names the
    thing to reject ("card", "debate").

    The orchestrator's mandate gate refuses an entry this way: ``mandate_gate.mandate_exposure`` finds who else holds
    the instrument and ``mandates.check_mandate`` returns the verdict. Both run here unchanged and read-only, and only
    the verdict is read, so the dry run cannot disagree with the gate. The API has no engine, so the dispatcher's
    in-memory orders are not visible here; the gate still refuses those at execution. Nothing is written."""
    instrument = canonical_instrument(instrument_id)
    engine = SimpleNamespace(dispatcher=SimpleNamespace(), account_id=PAPER_ACCOUNT_ID)
    exposure = await mandate_exposure(conn, engine, mandate, instrument, {})
    order = MandateOrder(instrument_id=instrument, side="BUY", quantity=quantity,
                         ref_price=None)  # the conflict is decided before the notional, so no price is needed
    if check_mandate(mandate, order, exposure, approval_ok=True) is not MandateReject.INSTRUMENT_CONFLICT:
        return None
    holder = await conn.fetchval(
        """SELECT instance_id FROM paper_positions WHERE account_id = $1 AND status = 'OPEN' AND quantity > 0
           AND instrument_id = $2 AND instance_id <> $3 LIMIT 1""", PAPER_ACCOUNT_ID, instrument, mandate.instance_id)
    who = holder or "another owner"
    held_by = mandate_id_from_instance(holder)
    if held_by is not None:  # a mandate's instance: name the mandate, not its instance id
        who = await conn.fetchval("SELECT name FROM ai_mandates WHERE id = $1", held_by) or who
    return (f"MANDATE_INSTRUMENT_CONFLICT: {who} holds {instrument_id}; mandate {mandate.name} cannot add to "
            f"it, so nothing was queued; reject this {what}")


async def choose_mandate_remedy(pool, instrument: str, requested: Decimal, marks) -> dict[str, Any] | None:
    """AMBIGUOUS_MANDATE: several ACTIVE mandates allow the instrument, so approve needs one named. Each candidate says
    what approve would size the entry to in it (``requested``, % of its budget, cut to its room). When one of them
    holds the instrument it is the only one that can add to it (the others would be refused as
    MANDATE_INSTRUMENT_CONFLICT); when an owner outside them holds it, no choice helps.

    Each other candidate may also be tidied, so the overlap does not come back: the instrument removed from it, or the
    mandate paused when the instrument is all it covers. Both take room away, so neither needs TOTP. A candidate that
    holds the instrument is never tidied: its position stays in the mandate that owns it."""
    instrument = canonical_instrument(instrument)
    candidates = []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE status = 'ACTIVE' AND $1 = ANY(instruments) ORDER BY created_at",
            instrument)
        mandates = [Mandate.from_row(r) for r in rows]
        if len(mandates) < 2:
            return None
        outside = await conn.fetchval(
            """SELECT 1 FROM paper_positions WHERE account_id = $1 AND status = 'OPEN' AND quantity > 0
               AND instrument_id = $2 AND NOT (instance_id = ANY($3::text[])) LIMIT 1""",
            PAPER_ACCOUNT_ID, instrument, [m.instance_id for m in mandates])
        if outside:
            return None
        for m in mandates:
            inst, _ = await held_values(conn, m, instrument, marks)
            room = await entry_room_pct(conn, m, instrument, marks)
            candidates.append((m, inst > 0, room))
    holder = next((m for m, holds, _ in candidates if holds), None)
    out = []
    for m, holds, room in candidates:
        if holds:
            blocked = None
        elif holder is not None:
            blocked = f"{holder.name} holds {instrument}, so only it can add to it"
        elif room < MIN_MANDATE_ENTRY_PCT:
            blocked = f"no room left under its {m.max_position_pct:g}% limit"
        else:
            blocked = None
        tidy = None if holds else ("PAUSE" if m.instruments == [instrument] else "REMOVE_INSTRUMENT")
        out.append({"id": str(m.id), "name": m.name, "autonomy_level": int(m.autonomy_level),
                    "instruments": m.instruments, "max_position_pct": float(m.max_position_pct),
                    "capital_budget_usd": float(m.capital_budget_usd), "room_pct": float(room.quantize(Decimal("0.01"))),
                    "allocation_pct": float(min(requested, room).quantize(Decimal("0.01"))), "holds": holds,
                    "blocked": blocked, "tidy": tidy})
    if all(c["blocked"] for c in out):
        return None
    return {"kind": "CHOOSE_MANDATE", "candidates": out}
