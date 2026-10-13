"""Brain Home backend-for-frontend (AI-first blueprint A3.3–A3.5).

The web reaches these through the Next proxy ``/agent-api/brain/*`` → ``/api/v1/agents/brain/*``.
Operator auth (and CSRF on writes) happens here; the Brain itself runs in agent_worker, reached with
the internal HMAC signature. Sessions are scoped to the operator's user id, which the worker only
ever receives from this router.

Decision traces are read here directly, as ``cryptonite_api`` (SELECT granted in migration 024).

An order card is approved only through ``POST .../cards/{widget_id}/approve``, server-side: it resolves
the mandate the order trades in (A5.1; the named one, else the single ACTIVE mandate that allows the
instrument), claims the card (PENDING_APPROVAL -> APPROVED, so a concurrent reject cannot also win),
records an APPROVED ``ai_approvals`` row and enqueues one PAPER ``SUBMIT_ORDER`` bound to it, with the
idempotency key ``brain-card:<session>:<id>`` (a retry or double click never queues a second order).
The order is booked under the mandate's own instance, which the exit monitor's hard and trailing stops
protect, and sized by the allocation the operator saw. It is refused while the exit monitor is down,
because nothing else would stop the position out.
"""

import json
import logging
import os
import uuid
from decimal import Decimal
from typing import Any

import httpx
from cryptonite_api.routes.agents import sign_request
from cryptonite_api.routes.auth import OPERATOR_WRITE, require_human_operator, require_operator
from cryptonite_execution.approvals import ApprovalConflict, record_operator_approval
from cryptonite_execution.mandate_store import (
    entry_room_pct,
    instance_spot_holdings,
    resolve_mandate_for,
)
from cryptonite_schemas.mandate import MIN_HOLDING_QTY, MIN_MANDATE_ENTRY_PCT, canonical_instrument
from cryptonite_schemas.marks import read_marks
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("cryptonite.api.brain")

router = APIRouter(prefix="/api/v1/agents/brain", tags=["brain"])

EXIT_MONITOR_HEARTBEAT = "exit_monitor:heartbeat"
AGENT_WORKER_URL = os.getenv("AGENT_WORKER_URL", "http://cryptonite_agent_worker:8085").rstrip("/")
STREAM_TIMEOUT = httpx.Timeout(300.0, connect=5.0)


class BrainAskRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = Field(default=None, max_length=64)


class BrainNoteRequest(BaseModel):
    """An operator note; the only card transition a client may request is PENDING_APPROVAL -> REJECTED
    (approval goes through the approve endpoint)."""

    content: str = Field(min_length=1, max_length=2000)
    widget_id: str | None = Field(default=None, max_length=128)
    status: str | None = Field(default=None, pattern="^REJECTED$")


def operator_id(auth: dict) -> str:
    return str(auth.get("user_id") or f"machine:{auth.get('role', 'operator')}")


def _signed(method: str, path: str, body: bytes = b"") -> dict[str, str]:
    headers = sign_request(method, path, body)
    headers["Content-Type"] = "application/json"
    return headers


async def _worker_get(path: str) -> Any:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{AGENT_WORKER_URL}{path}", headers=_signed("GET", path))
    except httpx.HTTPError as e:
        logger.error("Brain worker unreachable (%s): %s", path, e)
        raise HTTPException(status_code=503, detail="Brain unavailable")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail="Brain worker error")
    return resp.json()


@router.post("/stream", dependencies=OPERATOR_WRITE)
async def brain_stream(req: BrainAskRequest, auth: dict = Depends(require_operator)):
    """Proxy one Brain turn as Server-Sent Events. Closing the request cancels the turn upstream."""
    path = "/api/v1/brain/stream"
    body = json.dumps({"operator_id": operator_id(auth), "message": req.message,
                       "session_id": req.session_id}).encode()
    client = httpx.AsyncClient(timeout=STREAM_TIMEOUT)
    try:
        upstream = await client.send(
            client.build_request("POST", f"{AGENT_WORKER_URL}{path}", content=body, headers=_signed("POST", path, body)),
            stream=True,
        )
    except httpx.HTTPError as e:
        await client.aclose()
        logger.error("Brain worker unreachable: %s", e)
        raise HTTPException(status_code=503, detail="Brain unavailable")
    if upstream.status_code != 200:
        detail = (await upstream.aread()).decode(errors="replace")[:300]
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=429 if upstream.status_code == 429 else 502, detail=detail or "Brain error")

    async def relay():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _worker_send(method: str, path: str, body: dict[str, Any]) -> Any:
    """A signed JSON call to the worker for a body-carrying request (the signature covers the body)."""
    raw = json.dumps(body).encode()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, f"{AGENT_WORKER_URL}{path}", content=raw,
                                        headers=_signed(method, path, raw))
    except httpx.HTTPError as e:
        logger.error("Brain worker unreachable (%s): %s", path, e)
        raise HTTPException(status_code=503, detail="Brain unavailable")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Session not found")
    if resp.status_code == 422:
        raise HTTPException(status_code=422, detail=resp.json().get("detail", "Invalid request"))
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail="Brain worker error")
    return resp.json()


@router.get("/sessions")
async def brain_sessions(
    auth: dict = Depends(require_operator),
    q: str | None = Query(default=None, max_length=200),
    archived: bool = False,
    limit: int = Query(default=30, ge=1, le=100),
    before: str | None = Query(default=None, max_length=120),
):
    """The operator's conversation history, newest activity first, searchable, one page at a time."""
    return await _worker_send("POST", f"/api/v1/brain/operators/{operator_id(auth)}/sessions/search",
                              {"query": q, "archived": archived, "limit": limit, "before": before})


class BrainSessionPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    archived: bool | None = None


@router.patch("/sessions/{sid}", dependencies=OPERATOR_WRITE)
async def brain_session_patch(sid: uuid.UUID, req: BrainSessionPatch, auth: dict = Depends(require_operator)):
    """Rename a conversation, or archive it (an archived one comes back when it gets a new message)."""
    if req.title is None and req.archived is None:
        raise HTTPException(status_code=422, detail="Nothing to change")
    body = {k: v for k, v in {"title": req.title, "archived": req.archived}.items() if v is not None}
    return await _worker_send("PATCH", f"/api/v1/brain/operators/{operator_id(auth)}/sessions/{sid}", body)


@router.get("/sessions/{sid}")  # not ``session_id``: that name is the auth cookie parameter
async def brain_session(sid: uuid.UUID, auth: dict = Depends(require_operator)):
    return await _worker_get(f"/api/v1/brain/operators/{operator_id(auth)}/sessions/{sid}")


async def _worker_note(sid: uuid.UUID, body: dict[str, Any]) -> int:
    path = f"/api/v1/brain/sessions/{sid}/notes"
    raw = json.dumps(body).encode()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{AGENT_WORKER_URL}{path}", content=raw, headers=_signed("POST", path, raw))
    except httpx.HTTPError as e:
        logger.error("Brain worker unreachable: %s", e)
        raise HTTPException(status_code=503, detail="Brain unavailable")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Session not found")
    if resp.status_code not in (200, 409):
        raise HTTPException(status_code=502, detail="Brain worker error")
    return resp.status_code


@router.post("/sessions/{sid}/notes", dependencies=OPERATOR_WRITE)
async def brain_note(sid: uuid.UUID, req: BrainNoteRequest, auth: dict = Depends(require_operator)):
    body = {"operator_id": operator_id(auth), "content": req.content, "widget_id": req.widget_id,
            "status": req.status, "expect_status": "PENDING_APPROVAL" if req.status else None}
    if await _worker_note(sid, body) == 409:
        raise HTTPException(status_code=409, detail="The card is no longer pending")
    return {"status": "ok"}


def _find_card(session: dict[str, Any], widget_id: str) -> dict[str, Any] | None:
    for msg in session.get("messages", []):
        for w in msg.get("widgets") or []:
            if w.get("id") == widget_id and w.get("kind") == "order_card":
                return w
    return None


class CardApproveRequest(BaseModel):
    mandate_id: uuid.UUID | None = None  # default: the single ACTIVE mandate that allows the instrument


@router.post("/sessions/{sid}/cards/{widget_id}/approve", dependencies=OPERATOR_WRITE)
async def approve_order_card(sid: uuid.UUID, widget_id: str, request: Request, auth: dict = Depends(require_human_operator),
                             req: CardApproveRequest | None = None):
    """Approve an order card: one PAPER order inside a mandate, at most once, protected by the exit
    monitor's stops. The approval is recorded in ``ai_approvals`` and bound to its command (A5.3)."""
    op = operator_id(auth)
    if len(widget_id) > 128:
        raise HTTPException(status_code=422, detail="Invalid card id")
    card = _find_card(await _worker_get(f"/api/v1/brain/operators/{op}/sessions/{sid}"), widget_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Order card not found")
    if card.get("status") == "APPROVED" and card.get("command_id"):
        return {"command_id": card["command_id"], "status": "APPROVED", "replayed": True}
    if card.get("status") not in ("PENDING_APPROVAL", "APPROVED"):
        raise HTTPException(status_code=409, detail=f"The card is {card.get('status')}")

    raw = await request.app.state.stream_hub.redis.get(EXIT_MONITOR_HEARTBEAT)
    if not raw:
        raise HTTPException(status_code=409, detail="EXIT_MONITOR_DOWN: start the exit monitor "
                            "(docker compose --profile monitoring up -d exit_monitor); without it the position has no stop")
    protection = json.loads(raw)

    # Every AI order trades inside a mandate (A5.1); resolve it before the card is claimed
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        mandate, why = await resolve_mandate_for(conn, card["instrument_id"], req.mandate_id if req else None)
    if mandate is None:
        raise HTTPException(status_code=409, detail=f"{why}: approve this card inside an ACTIVE mandate that allows "
                            f"{card['instrument_id']} (Mission Control → Mandates)")

    # Spot is long-only: a SELL closes what the mandate holds, sized to that holding as a debate SELL is, never to the
    # budget. Refused before the card is claimed, so the operator rejects it here instead of finding a failed order later
    held = Decimal("0")
    if card["side"] == "SELL":
        async with pool.acquire() as conn:
            held = (await instance_spot_holdings(conn, mandate.instance_id)).get(
                canonical_instrument(card["instrument_id"]), (Decimal("0"), Decimal("0")))[0]
        if held < MIN_HOLDING_QTY:
            raise HTTPException(status_code=409, detail=f"SPOT_SELL_NOT_HELD: mandate {mandate.name} holds no "
                                f"{card['instrument_id']}, and a spot sell only closes a holding; reject this card")

    # One Brain decision, one order (D4): the Brain can emit the same order as several cards in one turn
    # (3 XRP cards of one trace were approved within 6 s); a twin of an approved card is refused, unclaimed
    trace = card.get("trace_id")
    trace_id = uuid.UUID(trace) if _is_uuid(trace) else None
    source_ref = f"{sid}:{widget_id}"
    if trace_id is not None:
        async with pool.acquire() as conn:
            twin = await _approved_twin(conn, trace_id, card, source_ref)
        if twin is not None:
            raise HTTPException(status_code=409, detail=_duplicate_detail(twin))

    allocation = Decimal(str(card["allocation_pct"]))
    if card["side"] == "BUY":  # an entry only gets the room the mandate has left under its position limit
        marks = await read_marks(getattr(getattr(request.app.state, "stream_hub", None), "redis", None))
        async with pool.acquire() as conn:
            room = await entry_room_pct(conn, mandate, card["instrument_id"], marks)
        if room < MIN_MANDATE_ENTRY_PCT:
            raise HTTPException(status_code=409, detail=(
                f"MANDATE_POSITION_LIMIT_REACHED: mandate {mandate.name} already holds its limit of "
                f"{mandate.max_position_pct}% in {card['instrument_id']}; nothing was queued"))
        allocation = min(allocation, room.quantize(Decimal("0.01")))
    budget = mandate.capital_budget_usd * allocation / 100
    if card["side"] == "SELL":
        summary = f"SELL {card['instrument_id']} {card['order_type']} the whole holding of {held:g} in mandate {mandate.name}"
    else:
        summary = (f"{card['side']} {card['instrument_id']} {card['order_type']} at {allocation:g}% of mandate "
                   f"{mandate.name} (${budget:,.0f})")
    if card.get("status") == "PENDING_APPROVAL":  # claim it first: a concurrent reject can no longer win
        claimed = await _worker_note(sid, {
            "operator_id": op, "widget_id": widget_id, "status": "APPROVED", "expect_status": "PENDING_APPROVAL",
            "content": f"Operator approved order card {widget_id}: {summary}.",
        })
        if claimed == 409:
            raise HTTPException(status_code=409, detail="The card is no longer pending")

    payload: dict[str, Any] = {
        "instrument_id": card["instrument_id"], "side": card["side"], "order_type": card["order_type"],
        # sized from the allocation the operator approved, as a share of the mandate's budget
        # (the mandate check and the dispatcher's 1-25% clamp still apply)
        "allocation_pct": float(allocation),
        "source": "brain_order_card", "brain_session_id": str(sid), "brain_card_id": widget_id,
        "trace_id": card.get("trace_id"), "proposed_stop_loss": card.get("stop_loss"),
        "proposed_take_profit": card.get("take_profit"), "protection": protection,
    }
    if card["order_type"] == "LIMIT":
        payload["price"] = card["limit_price"]
    if card["side"] == "SELL":
        payload["quantity"] = format(held, "f")  # exact: a float drops the last digits of a fill-sized holding  # the orchestrator sells this quantity, not a share of the budget
    payload["mandate_id"] = str(mandate.id)
    key = f"brain-card:{sid}:{widget_id}"
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if trace_id is not None:  # serialise the decision's cards; the twin check above can race
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"brain-decision:{trace_id}")
                    if (twin := await _approved_twin(conn, trace_id, card, source_ref)) is not None:
                        raise _DuplicateDecision(twin)
                decision = await record_operator_approval(
                    conn, mandate, "SUBMIT_ORDER", payload, summary=f"Brain order card: {summary}", source="brain_card",
                    source_ref=source_ref, decided_by=f"operator:{op}", idempotency_key=key, trace_id=trace_id)
        command_id = str(decision.command_id)
    except ApprovalConflict as e:
        raise HTTPException(status_code=409, detail=f"The approval is {e.status}")
    except _DuplicateDecision as e:
        await _worker_note(sid, {"operator_id": op, "widget_id": widget_id, "status": "REJECTED", "expect_status": "APPROVED",
                                 "content": f"Order card {widget_id} not queued: {_duplicate_detail(e.twin)}"})
        raise HTTPException(status_code=409, detail=_duplicate_detail(e.twin))
    except Exception:
        logger.exception("Could not queue order card %s", widget_id)
        await _worker_note(sid, {"operator_id": op, "widget_id": widget_id, "status": "FAILED",
                                 "expect_status": "APPROVED",
                                 "content": f"Order card {widget_id} was approved but could not be queued."})
        raise HTTPException(status_code=502, detail="Could not queue the order")

    await _worker_note(sid, {
        "operator_id": op, "widget_id": widget_id, "status": "APPROVED", "command_id": command_id,
        "expect_status": "APPROVED",
        "content": (f"Paper command {command_id} queued for the dispatcher inside mandate {mandate.name} ({summary}); "
                    f"the exit monitor's "
                    f"{protection.get('stop_loss_pct')}% hard stop and {protection.get('trail_pct')}% trailing stop "
                    f"(after +{protection.get('trail_activate_pct')}%) protect the position."),
    })
    return {"command_id": command_id, "status": "APPROVED", "protection": protection, "mandate_id": str(mandate.id),
            "approval_id": str(decision.approval_id)}


class _DuplicateDecision(Exception):
    def __init__(self, twin: Any) -> None:
        super().__init__(str(twin["id"]))
        self.twin = twin


async def _approved_twin(conn, trace_id: uuid.UUID, card: dict[str, Any], source_ref: str) -> Any:
    """Another card of the same Brain decision (trace), instrument and side that was already approved."""
    return await conn.fetchrow(
        """SELECT id, command_id, source_ref FROM ai_approvals
           WHERE source = 'brain_card' AND trace_id = $1 AND status = 'APPROVED' AND source_ref <> $4
             AND payload->>'instrument_id' = $2 AND payload->>'side' = $3
           ORDER BY created_at LIMIT 1""",
        trace_id, card["instrument_id"], card["side"], source_ref)


def _duplicate_detail(twin: Any) -> str:
    return (f"DUPLICATE_DECISION: this Brain decision already queued its order (approval {twin['id']}, "
            f"command {twin['command_id']}); reject this card")


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return value is not None
    except ValueError:
        return False


@router.get("/spend")
async def brain_spend(auth: dict = Depends(require_operator)):
    return await _worker_get("/api/v1/brain/spend")


def _jsonb(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _trace(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]), "parent_id": str(row["parent_id"]) if row["parent_id"] else None,
        "role": row["role"], "model": row["model"], "session_id": row["session_id"],
        "mandate_id": row["mandate_id"], "status": row["status"], "tool_calls": _jsonb(row["tool_calls"]) or [],
        "output": _jsonb(row["output"]), "error": row["error"], "cost_usd": str(row["cost_usd"]),
        "latency_ms": row["latency_ms"], "created_at": row["created_at"].isoformat(),
    }


TRACE_COLUMNS = ("id, parent_id, role, model, session_id, mandate_id, status, tool_calls, output, error, "
                 "cost_usd, latency_ms, created_at")


@router.get("/traces/{trace_id}")
async def brain_trace(trace_id: uuid.UUID, request: Request, auth: dict = Depends(require_operator)):
    """A decision trace with its parent and child calls (the Decision Trace drawer)."""
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {TRACE_COLUMNS} FROM ai_decision_traces WHERE id = $1", trace_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        children = await conn.fetch(
            f"SELECT {TRACE_COLUMNS} FROM ai_decision_traces WHERE parent_id = $1 ORDER BY created_at", trace_id)
        parent = None
        if row["parent_id"]:
            parent = await conn.fetchrow(f"SELECT {TRACE_COLUMNS} FROM ai_decision_traces WHERE id = $1",
                                         row["parent_id"])
    return {"trace": _trace(row), "parent": _trace(parent) if parent else None,
            "children": [_trace(c) for c in children]}
