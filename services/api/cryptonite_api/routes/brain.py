"""Brain Home backend-for-frontend (AI-first blueprint A3.3–A3.5).

The web reaches these through the Next proxy ``/agent-api/brain/*`` → ``/api/v1/agents/brain/*``.
Operator auth (and CSRF on writes) happens here; the Brain itself runs in agent_worker, reached with
the internal HMAC signature. Sessions are scoped to the operator's user id, which the worker only
ever receives from this router.

Decision traces are read here directly, as ``cryptonite_api`` (SELECT granted in migration 024).

An order card is approved only through ``POST .../cards/{widget_id}/approve``, server-side: it claims
the card (PENDING_APPROVAL -> APPROVED, so a concurrent reject cannot also win), then enqueues one
PAPER ``SUBMIT_ORDER`` for GuardedOrderDispatcher with the idempotency key ``brain-card:<id>`` (a
retry or double click never queues a second order), owned by the AI instance so the exit monitor's
hard and trailing stops protect it, and sized by the allocation the operator saw. It is refused while
the exit monitor is down, because nothing else would stop the position out.
"""

import json
import logging
import os
import uuid
from typing import Any

import asyncpg
import httpx
from cryptonite_api.routes.agents import sign_request
from cryptonite_api.routes.auth import OPERATOR_WRITE, require_operator
from cryptonite_api.routes.paper import _enqueue_command
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("cryptonite.api.brain")

router = APIRouter(prefix="/api/v1/agents/brain", tags=["brain"])

AI_INSTANCE = "AI_DEBATE_WORKER"  # positions owned by it get the exit monitor's deterministic stops
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


@router.get("/sessions")
async def brain_sessions(auth: dict = Depends(require_operator)):
    return await _worker_get(f"/api/v1/brain/operators/{operator_id(auth)}/sessions")


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


@router.post("/sessions/{sid}/cards/{widget_id}/approve", dependencies=OPERATOR_WRITE)
async def approve_order_card(sid: uuid.UUID, widget_id: str, request: Request, auth: dict = Depends(require_operator)):
    """Approve an order card: one PAPER order, at most once, protected by the exit monitor's stops."""
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

    summary = (f"{card['side']} {card['instrument_id']} {card['order_type']} at {card['allocation_pct']}% of equity")
    if card.get("status") == "PENDING_APPROVAL":  # claim it first: a concurrent reject can no longer win
        claimed = await _worker_note(sid, {
            "operator_id": op, "widget_id": widget_id, "status": "APPROVED", "expect_status": "PENDING_APPROVAL",
            "content": f"Operator approved order card {widget_id}: {summary}.",
        })
        if claimed == 409:
            raise HTTPException(status_code=409, detail="The card is no longer pending")

    payload: dict[str, Any] = {
        "instrument_id": card["instrument_id"], "side": card["side"], "order_type": card["order_type"],
        # sized from the allocation the operator approved (the dispatcher still clamps 1-25%)
        "allocation_pct": card["allocation_pct"],
        "source": "brain_order_card", "brain_session_id": str(sid), "brain_card_id": widget_id,
        "trace_id": card.get("trace_id"), "proposed_stop_loss": card.get("stop_loss"),
        "proposed_take_profit": card.get("take_profit"), "protection": protection,
    }
    if card["order_type"] == "LIMIT":
        payload["price"] = card["limit_price"]
    key = f"brain-card:{widget_id}"
    pool = request.app.state.pool
    try:
        cmd = await _enqueue_command(pool, "SUBMIT_ORDER", AI_INSTANCE, payload,
                                     requester_id=f"operator:{op}", idempotency_key=key)
        command_id = str(cmd["command_id"])
    except asyncpg.UniqueViolationError:  # already queued by an earlier attempt
        command_id = str(await pool.fetchval("SELECT command_id FROM execution_commands WHERE idempotency_key = $1", key))
    except Exception:
        logger.exception("Could not queue order card %s", widget_id)
        await _worker_note(sid, {"operator_id": op, "widget_id": widget_id, "status": "FAILED",
                                 "expect_status": "APPROVED",
                                 "content": f"Order card {widget_id} was approved but could not be queued."})
        raise HTTPException(status_code=502, detail="Could not queue the order")

    await _worker_note(sid, {
        "operator_id": op, "widget_id": widget_id, "status": "APPROVED", "command_id": command_id,
        "expect_status": "APPROVED",
        "content": (f"Paper command {command_id} queued for the dispatcher ({summary}); the exit monitor's "
                    f"{protection.get('stop_loss_pct')}% hard stop and {protection.get('trail_pct')}% trailing stop "
                    f"(after +{protection.get('trail_activate_pct')}%) protect the position."),
    })
    return {"command_id": command_id, "status": "APPROVED", "protection": protection}


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
