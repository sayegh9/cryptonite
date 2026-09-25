"""Brain Home backend-for-frontend (AI-first blueprint A3.3–A3.5).

The web reaches these through the Next proxy ``/agent-api/brain/*`` → ``/api/v1/agents/brain/*``.
Operator auth (and CSRF on writes) happens here; the Brain itself runs in agent_worker, reached with
the internal HMAC signature. Sessions are scoped to the operator's user id, which the worker only
ever receives from this router.

Decision traces are read here directly, as ``cryptonite_api`` (SELECT granted in migration 024).
Orders never pass through this router: an approved order card goes to ``POST /api/v1/paper/orders``.
"""

import json
import logging
import os
import uuid
from typing import Any

import httpx
from cryptonite_api.routes.agents import sign_request
from cryptonite_api.routes.auth import OPERATOR_WRITE, require_operator
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("cryptonite.api.brain")

router = APIRouter(prefix="/api/v1/agents/brain", tags=["brain"])

AGENT_WORKER_URL = os.getenv("AGENT_WORKER_URL", "http://cryptonite_agent_worker:8085").rstrip("/")
STREAM_TIMEOUT = httpx.Timeout(300.0, connect=5.0)


class BrainAskRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = Field(default=None, max_length=64)


class BrainNoteRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    widget_id: str | None = Field(default=None, max_length=128)
    status: str | None = Field(default=None, pattern="^(APPROVED|REJECTED|FAILED)$")
    command_id: str | None = Field(default=None, max_length=128)


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


@router.post("/sessions/{sid}/notes", dependencies=OPERATOR_WRITE)
async def brain_note(sid: uuid.UUID, req: BrainNoteRequest, auth: dict = Depends(require_operator)):
    path = f"/api/v1/brain/sessions/{sid}/notes"
    body = json.dumps({"operator_id": operator_id(auth), **req.model_dump()}).encode()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{AGENT_WORKER_URL}{path}", content=body, headers=_signed("POST", path, body))
    except httpx.HTTPError as e:
        logger.error("Brain worker unreachable: %s", e)
        raise HTTPException(status_code=503, detail="Brain unavailable")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Session not found")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail="Brain worker error")
    return resp.json()


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
