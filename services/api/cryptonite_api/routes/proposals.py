"""H13b: scanner proposals, a setup the debate said HOLD on, for the operator to decide.

The operator's approve here is the only way from a proposal to an order: nothing sweeps a proposal into execution and
unanswered it expires. Approve runs the Brain order card's checks (``_plan_card``) and the same leverage confirmation,
TOTP and payload, then queues one command as an ``ai_approvals`` row of source ``scanner_proposal``, which neither flips
the debate's run (``_sync_source``) nor counts as veto evidence (``autonomy_metrics``).
"""

import json
import logging
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal

from cryptonite_api.leverage_card import card_entry_price, card_leverage_plan, is_perp_entry
from cryptonite_api.routes.auth import OPERATOR_WRITE, require_human_operator, require_operator
from cryptonite_api.routes.brain import (
    CardPlan,
    _plan_card,
    card_command_payload,
    operator_id,
    preflight_refusal,
    preflight_result,
    require_leverage_confirmation,
)
from cryptonite_execution.approvals import ApprovalConflict, record_operator_approval
from cryptonite_execution.mandate_store import entry_room_pct, load_mandate
from cryptonite_execution.related_positions import (
    OPPOSITE_POSITION_OPEN,
    opposite_positions,
    read_related_positions,
)
from cryptonite_execution.sizing import grade_size_pct
from cryptonite_schemas.mandate import MAX_PERP_LEVERAGE_CAP
from cryptonite_schemas.marks import read_marks
from cryptonite_schemas.reward_risk import MIN_REWARD_TO_RISK, meets_min_rr, reward_to_risk
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("cryptonite.api.proposals")

router = APIRouter(prefix="/api/v1/proposals", tags=["proposals"])

PAPER_ACCOUNT_ID = "PAPER-ACCOUNT-001"
RECENT_DAYS = 7
ROWS_MAX = 100
COLUMNS = ("p.id, p.candidate_id, p.run_id, p.mandate_id, p.instrument_id, p.side, p.card, p.status, p.refusal, "
           "p.command_id, p.approval_id, p.expires_at, p.created_at, p.decided_at, p.decided_by")


class ProposalApproveRequest(BaseModel):
    acknowledge_against_debate: bool = False
    leverage: int | None = Field(default=None, ge=1, le=MAX_PERP_LEVERAGE_CAP)
    totp_code: str | None = None
    confirm_high_leverage: bool = False


def _redis(request: Request):
    return getattr(getattr(request.app.state, "stream_hub", None), "redis", None)


def _card_of(row: Any) -> dict[str, Any]:
    return json.loads(row["card"]) if isinstance(row["card"], str) else dict(row["card"])


def _view(row: Any) -> dict[str, Any]:
    out = {k: str(row[k]) if row[k] is not None else None
           for k in ("id", "candidate_id", "run_id", "mandate_id", "command_id", "approval_id")}
    out.update({k: row[k] for k in ("instrument_id", "side", "status", "refusal", "decided_by")})
    out.update({k: row[k].isoformat() if row[k] else None for k in ("expires_at", "created_at", "decided_at")})
    out["card"] = _card_of(row)
    out["mandate_name"] = row["mandate_name"]
    return out


async def _expire_due(conn, proposal_id: uuid.UUID | None = None) -> None:
    """A pending row past its deadline is EXPIRED, written on read. Nothing runs: expiry only ever means no."""
    await conn.execute(
        """UPDATE setup_proposals SET status = 'EXPIRED', decided_at = now(), decided_by = 'system'
           WHERE status = 'PENDING' AND expires_at <= now() AND ($1::uuid IS NULL OR id = $1)""", proposal_id)


async def _load(conn, proposal_id: uuid.UUID) -> Any:
    await _expire_due(conn, proposal_id)
    row = await conn.fetchrow(
        f"""SELECT {COLUMNS}, m.name AS mandate_name FROM setup_proposals p
            LEFT JOIN ai_mandates m ON m.id = p.mandate_id WHERE p.id = $1""", proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return row


async def _sized_card(request: Request, row: Any, card: dict[str, Any], leverage: int | None) -> dict[str, Any]:
    """A perp entry asks the margin its grade sizes, cut to what fits at 1x, exactly as ``_route_perp`` sizes a debate
    entry: ``_plan_card`` keeps a card's own allocation (refused, never cut) and the mandate's whole position limit
    never fits under its exposure ceiling. The ceilings themselves are unchanged. A spot entry is sized by ``_plan_card``."""
    if not is_perp_entry(card):
        return card
    pool, redis = request.app.state.pool, _redis(request)
    marks = await read_marks(redis)
    async with pool.acquire() as conn:
        mandate = await load_mandate(conn, row["mandate_id"])
        if mandate is None:
            return card  # _plan_card refuses: MANDATE_NOT_FOUND
        room = await entry_room_pct(conn, mandate, card["instrument_id"], marks)
    asked = {**card, "allocation_pct": grade_size_pct(card.get("setup_grade"), mandate.max_position_pct, room)}
    plan = await card_leverage_plan(pool, redis, mandate, asked, requested_leverage=leverage, shrink_margin_to_fit=True)
    if not plan.ok:
        return asked  # _plan_card gives the refusal
    pct = (plan.margin_usd / mandate.capital_budget_usd * 100).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return {**card, "allocation_pct": float(pct)}


async def _plan(request: Request, row: Any, card: dict[str, Any], leverage: int | None) -> CardPlan:
    """The Brain card's plan, then the debate entry's no-hedge rule: an opposite position that opened after the proposal
    (the gate rechecks at execution too; here it costs the operator no TOTP)."""
    plan = await _plan_card(request, row["id"], card, row["mandate_id"], leverage)
    async with request.app.state.pool.acquire() as conn:
        related = await read_related_positions(conn, card["instrument_id"], PAPER_ACCOUNT_ID)
    if opposite_positions(related, card["side"]):
        raise HTTPException(status_code=409, detail=(f"{OPPOSITE_POSITION_OPEN}: {card['side']} is opposite to an open "
                                                     f"position on this coin; no hedge is opened; nothing was queued"))
    return plan


async def _live_rr(request: Request, card: dict[str, Any]) -> dict[str, Any]:
    """The reward-to-risk at the live price, against the floor the approve's slip check enforces."""
    live = await card_entry_price(_redis(request), card)
    rr = reward_to_risk(live, card["stop_loss"], card["take_profit"]) if live is not None else None
    return {"live_entry": float(live) if live is not None else None, "live_rr": float(rr) if rr is not None else None,
            "rr_floor": float(MIN_REWARD_TO_RISK), "meets_rr_floor": meets_min_rr(rr) if rr is not None else None}


@router.get("")
async def list_proposals(request: Request, auth: dict = Depends(require_operator),
                         status: Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED"] | None = Query(default=None)):
    """Pending rows plus the last week's decided ones, or one status."""
    async with request.app.state.pool.acquire() as conn:
        await _expire_due(conn)
        rows = await conn.fetch(
            f"""SELECT {COLUMNS}, m.name AS mandate_name FROM setup_proposals p
                LEFT JOIN ai_mandates m ON m.id = p.mandate_id
                WHERE ($1::text IS NULL AND (p.status = 'PENDING' OR p.created_at > now() - make_interval(days => $2)))
                   OR p.status = $1
                ORDER BY (p.status = 'PENDING') DESC, p.created_at DESC LIMIT $3""", status, RECENT_DAYS, ROWS_MAX)
        now = await conn.fetchval("SELECT now()")
    views = [_view(r) for r in rows]
    return {"now": now.isoformat(), "pending": sum(v["status"] == "PENDING" for v in views), "proposals": views}


@router.post("/{proposal_id}/preflight", dependencies=OPERATOR_WRITE)
async def preflight_proposal(proposal_id: uuid.UUID, request: Request, auth: dict = Depends(require_operator),
                             leverage: int | None = Query(default=None, ge=1, le=MAX_PERP_LEVERAGE_CAP)):
    """The dry run of approve, with the same answers as a Brain card's, and the live R:R. Writes no decision."""
    async with request.app.state.pool.acquire() as conn:
        row = await _load(conn, proposal_id)
    if row["status"] != "PENDING":
        return {"ok": False, "code": "NOT_PENDING", "detail": f"The proposal is {row['status']}", "needs_mandate": False,
                "remedy": None}
    card = await _sized_card(request, row, _card_of(row), leverage)
    rr = await _live_rr(request, card)
    try:
        plan = await _plan(request, row, card, leverage)
    except HTTPException as e:
        return {**await preflight_refusal(request, card, e, row["mandate_id"]), **rr}
    return {**preflight_result(card, plan, leverage), **rr}


@router.post("/{proposal_id}/approve", dependencies=OPERATOR_WRITE)
async def approve_proposal(proposal_id: uuid.UUID, request: Request, auth: dict = Depends(require_human_operator),
                           req: ProposalApproveRequest | None = None):
    """One PAPER order inside the proposal's mandate, at most once, and only from this call. Every refusal before the
    claim leaves the proposal pending."""
    op = operator_id(auth)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await _load(conn, proposal_id)
    if row["status"] != "PENDING":
        raise HTTPException(status_code=409, detail=f"The proposal is {row['status']}")
    if not (req and req.acknowledge_against_debate):
        raise HTTPException(status_code=409, detail=("AGAINST_DEBATE_VERDICT: the debate said HOLD on this setup; "
                                                     "acknowledge it to approve"))
    leverage = req.leverage
    card = await _sized_card(request, row, _card_of(row), leverage)
    try:
        plan = await _plan(request, row, card, leverage)
    except HTTPException as e:
        await pool.execute("UPDATE setup_proposals SET refusal = $2 WHERE id = $1 AND status = 'PENDING'",
                           proposal_id, str(e.detail))
        raise
    await require_leverage_confirmation(request, auth, plan.leverage_plan, req)
    trace = card.get("trace_id")
    payload = card_command_payload(card, plan, req, source="scanner_proposal", origin={
        "proposal_id": str(proposal_id), **({"decision_trace_id": trace} if trace else {})})
    decided_by = f"operator:{op}"
    try:
        # One transaction: the claim, the approval and its command stand or fall together, so a failure to queue
        # leaves the proposal pending, and a concurrent approve or reject finds it claimed
        async with pool.acquire() as conn:
            async with conn.transaction():
                claimed = await conn.fetchval(
                    """UPDATE setup_proposals SET status = 'APPROVED', decided_at = now(), decided_by = $2, refusal = NULL
                       WHERE id = $1 AND status = 'PENDING' AND expires_at > now() RETURNING id""",
                    proposal_id, decided_by)
                if claimed is None:
                    raise ApprovalConflict("no longer pending")
                decision = await record_operator_approval(
                    conn, plan.mandate, "SUBMIT_ORDER", payload, summary=f"Scanner proposal (debate said HOLD): {plan.summary}",
                    source="scanner_proposal", source_ref=str(proposal_id), decided_by=decided_by,
                    idempotency_key=f"scanner-proposal:{proposal_id}", trace_id=plan.trace_id)
                await conn.execute("UPDATE setup_proposals SET command_id = $2, approval_id = $3 WHERE id = $1",
                                   proposal_id, decision.command_id, decision.approval_id)
    except ApprovalConflict as e:
        raise HTTPException(status_code=409, detail=f"The proposal is {e.status}")
    except Exception:
        logger.exception("Could not queue proposal %s", proposal_id)
        raise HTTPException(status_code=502, detail="Could not queue the order; the proposal stays pending")
    lev_plan = plan.leverage_plan
    return {"command_id": str(decision.command_id), "status": "APPROVED", "protection": plan.protection,
            "mandate_id": str(plan.mandate.id), "approval_id": str(decision.approval_id),
            "leverage": lev_plan["leverage"] if lev_plan is not None else None}


@router.post("/{proposal_id}/reject", dependencies=OPERATOR_WRITE)
async def reject_proposal(proposal_id: uuid.UUID, request: Request, auth: dict = Depends(require_human_operator)):
    async with request.app.state.pool.acquire() as conn:
        row = await _load(conn, proposal_id)
        if row["status"] != "PENDING":
            raise HTTPException(status_code=409, detail=f"The proposal is {row['status']}")
        claimed = await conn.fetchval(
            """UPDATE setup_proposals SET status = 'REJECTED', decided_at = now(), decided_by = $2
               WHERE id = $1 AND status = 'PENDING' AND expires_at > now() RETURNING id""",
            proposal_id, f"operator:{operator_id(auth)}")
    if claimed is None:
        raise HTTPException(status_code=409, detail="The proposal is no longer pending")
    return {"status": "REJECTED"}
