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
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from cryptonite_agents.brain.widgets import INSTRUMENT_ID
from cryptonite_api.brain_outcomes import (
    card_patch,
    command_outcome,
    debate_outcome,
    debate_patch,
    debate_read,
    followed_cards,
    followed_debates,
)
from cryptonite_api.entry_stops import entry_requires_stop, resolve_entry_stop
from cryptonite_api.leverage_card import (
    HIGH_LEVERAGE_CONFIRM,
    card_entry_price,
    card_leverage_plan,
    is_perp_entry,
    perp_mark,
)
from cryptonite_api.mandate_choice import (
    choose_mandate_remedy,
    entry_filter_detail,
    instrument_conflict,
)
from cryptonite_api.routes.agents import sign_request
from cryptonite_api.routes.ai import _stopped_mandate_on, require_totp
from cryptonite_api.routes.auth import OPERATOR_WRITE, require_human_operator, require_operator
from cryptonite_api.universe import require_fed
from cryptonite_execution.approvals import ApprovalConflict, record_operator_approval
from cryptonite_execution.mandate_store import (
    MANDATE_COLUMNS,
    entry_room_pct,
    held_values,
    instance_spot_holdings,
    load_mandate,
    resolve_mandate_for,
)
from cryptonite_execution.mandates import entry_scope_reject
from cryptonite_execution.profile_lifecycle import coin_lock_conflict
from cryptonite_execution.sizing import grade_size_pct
from cryptonite_schemas.mandate import (
    MAX_PERP_LEVERAGE_CAP,
    MAX_POSITION_PCT_CAP,
    MIN_HOLDING_QTY,
    MIN_MANDATE_ENTRY_PCT,
    Mandate,
    MandateReject,
    canonical_instrument,
    is_perp_instrument,
    room_remedy,
)
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
    """A card rejection by the operator; the only card transition a client may request is PENDING_APPROVAL -> REJECTED
    (approval goes through the approve endpoint). The conversation line is written by the server from the card, so
    ``content`` is accepted for the web client and ignored: a client's text never becomes a system event."""

    widget_id: str = Field(min_length=1, max_length=128)
    status: str = Field(pattern="^REJECTED$")
    content: str | None = Field(default=None, max_length=2000)


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
async def brain_stream(req: BrainAskRequest, request: Request, auth: dict = Depends(require_operator)):
    """Proxy one Brain turn as Server-Sent Events. Closing the request cancels the turn upstream."""
    # The Brain reads its conversation as context: bring approved cards up to what their orders did first, so it
    # never builds on an order that was refused, or misses one that filled
    try:
        if req.session_id:
            await sync_session_cards(request, operator_id(auth), uuid.UUID(req.session_id))
    except ValueError:
        pass  # not a session id; the worker answers SESSION_NOT_FOUND
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


async def _worker_send(method: str, path: str, body: dict[str, Any], params: dict[str, str] | None = None) -> Any:
    """A signed JSON call to the worker for a body-carrying request (the signature covers the path and body, not
    ``params``)."""
    raw = json.dumps(body).encode()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, f"{AGENT_WORKER_URL}{path}", content=raw, params=params,
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


@router.delete("/sessions/{sid}", dependencies=OPERATOR_WRITE)
async def brain_session_delete(sid: uuid.UUID, auth: dict = Depends(require_human_operator)):
    """Delete a conversation for good, with its messages and summaries."""
    return await _worker_send("DELETE", f"/api/v1/brain/operators/{operator_id(auth)}/sessions/{sid}", {})


@router.delete("/sessions", dependencies=OPERATOR_WRITE)
async def brain_sessions_delete_archived(archived: bool = False, auth: dict = Depends(require_human_operator)):
    """Delete every archived conversation for good; returns ``{deleted: n}``."""
    if not archived:
        raise HTTPException(status_code=422, detail="Only archived conversations can be deleted in bulk")
    return await _worker_send("DELETE", f"/api/v1/brain/operators/{operator_id(auth)}/sessions", {},
                              params={"archived": "true"})


async def sync_session_cards(request: Request, op: str, sid: uuid.UUID, session: dict[str, Any] | None = None) -> int:
    """Move every approved order card of the conversation to what its order did (WORKING, FILLED, NOT_EXECUTED), and
    every queued Brain debate card to its run's final state (VERDICT, NO_TRADE, GATE_REFUSED, FAILED), recording each
    change as a system line, once. Returns how many cards changed. Never raises: an outcome that cannot be read now is
    read on the next load or turn."""
    try:
        if session is None:
            session = await _worker_get(f"/api/v1/brain/operators/{op}/sessions/{sid}")
        cards = followed_cards(session)
        debates = followed_debates(session)
        if not cards and not debates:
            return 0
        changed = 0
        async with request.app.state.pool.acquire() as conn:
            outcomes = [(c, await command_outcome(conn, c["command_id"])) for c in cards]
            debate_outs = []
            for d in debates:  # its own guard here too: a run that cannot be read now never costs an order card
                try:
                    debate_outs.append((d, *(await debate_read(conn, d["message_ref"]))))
                except Exception as e:
                    logger.warning("Debate card %s not read for session %s: %s", d.get("id"), sid, e)
        for card, out in outcomes:
            note = card_patch(card, out) if out else None
            if note and await _worker_note(sid, {"operator_id": op, **{k: v for k, v in note.items() if v is not None}}) == 200:
                changed += 1
        for card, out, run_status in debate_outs:  # its own guard: a refused debate note costs no order card
            note = debate_patch(card, out, run_status)
            try:
                if note and await _worker_note(sid, {"operator_id": op, **note}) == 200:
                    changed += 1
            except Exception as e:
                logger.warning("Debate card %s outcome not recorded for session %s: %s", card.get("id"), sid, e)
        return changed
    except Exception as e:  # the conversation still loads; the next load or turn tries again
        logger.warning("Brain card outcome sync failed for session %s: %s", sid, e)
        return 0


@router.get("/sessions/{sid}")  # not ``session_id``: that name is the auth cookie parameter
async def brain_session(sid: uuid.UUID, request: Request, auth: dict = Depends(require_operator)):
    """A conversation, with every approved card brought up to what its order did."""
    path = f"/api/v1/brain/operators/{operator_id(auth)}/sessions/{sid}"
    session = await _worker_get(path)
    if await sync_session_cards(request, operator_id(auth), sid, session):
        session = await _worker_get(path)
    return session


@router.get("/sessions/{sid}/cards/{widget_id}/outcome")
async def brain_card_outcome(sid: uuid.UUID, widget_id: str, request: Request, auth: dict = Depends(require_operator)):
    """What an approved card's order did, for a card waiting on screen. Records a change in the conversation."""
    card = await _get_card(request, operator_id(auth), sid, widget_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    if not card.get("command_id"):
        return {"status": card.get("status"), "state": None}
    async with request.app.state.pool.acquire() as conn:
        out = await command_outcome(conn, card["command_id"])
    if out is None:
        return {"status": card.get("status"), "state": None}
    note = card_patch(card, out) if card.get("status") in ("APPROVED", "WORKING") else None
    status = card.get("status")
    if note and await _worker_note(sid, {"operator_id": operator_id(auth), **{k: v for k, v in note.items() if v is not None}}) == 200:
        status = note["status"]
    return {"status": status, **out}


@router.get("/sessions/{sid}/cards/{widget_id}/debate")
async def brain_debate_outcome(sid: uuid.UUID, widget_id: str, request: Request,
                               auth: dict = Depends(require_operator)):
    """Where a Brain debate card's run is, for a card waiting on screen: QUEUED, RUNNING, VERDICT (with its approval),
    NO_TRADE, GATE_REFUSED or FAILED. A card with no message id (queued before Wave 3) is UNTRACKED: there is no run to
    follow, and that is not a failure. Read only: the conversation's line is written by the sync, never by this read."""
    if len(widget_id) > 128:
        raise HTTPException(status_code=422, detail="Invalid card id")
    session = await _worker_get(f"/api/v1/brain/operators/{operator_id(auth)}/sessions/{sid}")
    card = next((w for m in session.get("messages", []) for w in (m.get("widgets") or [])
                 if w.get("id") == widget_id and w.get("kind") == "debate_summary"), None)
    if card is None:
        raise HTTPException(status_code=404, detail="Debate card not found")
    if not card.get("message_ref"):  # no message id to follow: the card has no run to reach a verdict
        return {"status": "UNTRACKED", "run_id": None, "verdict": None, "reason": None}
    async with request.app.state.pool.acquire() as conn:
        return await debate_outcome(conn, card["message_ref"])


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


def _js_number(value: Any) -> str:
    """A number as the web prints it (5.0 reads as 5), so the reject line is the one the operator saw."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def _shown_instrument(card: dict[str, Any]) -> str:
    """The instrument a server line may echo from a stored card: the card's own when it matches INSTRUMENT_ID, else
    "an order card" (a card stored before the instrument pattern existed is not echoed as it was stored)."""
    raw = card.get("instrument_id")
    return raw if isinstance(raw, str) and re.fullmatch(INSTRUMENT_ID, raw) else "an order card"


def _find_card(session: dict[str, Any], widget_id: str) -> dict[str, Any] | None:
    for msg in session.get("messages", []):
        for w in msg.get("widgets") or []:
            if w.get("id") == widget_id and w.get("kind") == "order_card":
                return w
    return None


async def _get_card(request: Request, op: str, sid: uuid.UUID, widget_id: str) -> dict[str, Any] | None:
    """Find an order card in the conversation or from the active staged Redis cache if still streaming."""
    session = await _worker_get(f"/api/v1/brain/operators/{op}/sessions/{sid}")
    card = _find_card(session, widget_id)
    if card is not None:
        return card
    redis_client = getattr(getattr(request.app.state, "stream_hub", None), "redis", None)
    if redis_client:
        try:
            raw = await redis_client.get(f"brain:active_card:{widget_id}")
            if raw:
                cached = json.loads(raw)
                if isinstance(cached, dict) and cached.get("kind") == "order_card":
                    return cached
        except Exception:
            pass
    return None


@router.post("/sessions/{sid}/notes", dependencies=OPERATOR_WRITE)
async def brain_note(sid: uuid.UUID, req: BrainNoteRequest, request: Request, auth: dict = Depends(require_operator)):
    op = operator_id(auth)
    card = await _get_card(request, op, sid, req.widget_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    content = (f"Operator rejected order card {card['id']} ({card['side']} {_shown_instrument(card)}, "
               f"{_js_number(card['allocation_pct'])}% of mandate).")
    body = {"operator_id": op, "content": content, "widget_id": req.widget_id, "status": req.status,
            "expect_status": "PENDING_APPROVAL"}
    if await _worker_note(sid, body) == 409:
        raise HTTPException(status_code=409, detail="The card is no longer pending")
    return {"status": "ok"}


async def _held_qty(conn, instance_id: str, instrument_id: str) -> Decimal:
    """The net spot quantity the mandate's instance holds of ``instrument_id`` (0 when none)."""
    held = await instance_spot_holdings(conn, instance_id)
    return held.get(canonical_instrument(instrument_id), (Decimal("0"), Decimal("0")))[0]


async def _holding_mandate_for_sell(conn, instrument_id: str) -> tuple[Mandate | None, str]:
    """The mandate a SELL on ``instrument_id`` closes when the mandate that would be used holds none of it: the one
    non-archived (PAUSED or HALTED) mandate whose instance holds the instrument. (None, NO_MANDATE) when none holds it,
    (None, AMBIGUOUS_MANDATE) when more than one does."""
    inst = canonical_instrument(instrument_id)
    holders: list[Mandate] = []
    for row in await conn.fetch(
            "SELECT id FROM ai_mandates WHERE status <> 'ARCHIVED' AND $1 = ANY(instruments) ORDER BY created_at", inst):
        m = await load_mandate(conn, row["id"])
        if await _held_qty(conn, m.instance_id, inst) >= MIN_HOLDING_QTY:
            holders.append(m)
    if not holders:
        return None, "NO_MANDATE"
    if len(holders) > 1:
        return None, "AMBIGUOUS_MANDATE"
    return holders[0], ""


class CardApproveRequest(BaseModel):
    mandate_id: uuid.UUID | None = None  # default: the single ACTIVE mandate that allows the instrument
    # Perp entries (ADR 0009 D3: every risk-adding perp order is approved with TOTP). ``leverage`` is the operator's
    # choice on the card, at most what the dry run allows; ``confirm_high_leverage`` the second confirmation >= 8x
    totp_code: str | None = None
    leverage: int | None = Field(default=None, ge=1, le=MAX_PERP_LEVERAGE_CAP)
    confirm_high_leverage: bool = False


class _PerpRefusal(HTTPException):
    """A perp card refused by its leverage plan: the plan rides along so the dry run can show every ceiling."""

    def __init__(self, detail: str, plan: dict[str, Any] | None) -> None:
        super().__init__(status_code=409, detail=detail)
        self.leverage_plan = plan


@dataclass(frozen=True)
class CardPlan:
    """An order card as approve sizes and queues it now. Built only by ``_plan_card``, which reads and never writes."""
    mandate: Mandate
    allocation: Decimal  # % of the mandate budget: the card's, cut to the room a BUY has left under its position limit
    budget: Decimal  # USD: the allocation of the mandate's capital budget
    summary: str  # the audit text (the approval record and the paper command's summary)
    shown: str  # the same text as the server's note lines show it (see _shown_instrument)
    held: Decimal  # the spot quantity a SELL closes (0 for a BUY)
    protection: dict[str, Any]  # the exit monitor's heartbeat: the stops that protect the position
    trace_id: uuid.UUID | None
    source_ref: str  # the approval is recorded under this: "<session>:<card>"
    structural_stop: Decimal | None = None  # the entry protection written to the command payload
    leverage_plan: dict[str, Any] | None = None  # a perp entry's plan (leverage.py), as the dry run shows it
    perp_reduce: bool = False  # a perp card with reduce_pct: a reduce-only close of the mandate's position


async def _plan_card(request: Request, sid: uuid.UUID, card: dict[str, Any], mandate_id: uuid.UUID | None,
                     leverage: int | None = None) -> CardPlan:
    """Every check approve makes before it claims a card, in the same order, with the same refusals and details.
    Reads only: nothing is claimed, noted, recorded or queued here, so the dry run (preflight) cannot disagree with
    approve about what would be refused or how big the order would be."""
    if card.get("status") not in ("PENDING_APPROVAL", "APPROVED"):
        raise HTTPException(status_code=409, detail=f"The card is {card.get('status')}")

    raw = await request.app.state.stream_hub.redis.get(EXIT_MONITOR_HEARTBEAT)
    if not raw:
        raise HTTPException(status_code=409, detail="EXIT_MONITOR_DOWN: start the exit monitor "
                            "(docker compose --profile monitoring up -d exit_monitor); without it the position has no stop")
    protection = json.loads(raw)

    # An entry needs a live feed; a SELL closes a holding and is never blocked by the feed (a perp SELL is a short:
    # an entry)
    perp_entry = is_perp_entry(card)
    perp_reduce = is_perp_instrument(card["instrument_id"]) and not perp_entry
    if card["side"] != "SELL" or perp_entry:
        await require_fed(request, [card["instrument_id"]])

    structural_stop = None

    # Every AI order trades inside a mandate (A5.1); resolve it before the card is claimed
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        mandate, why = await resolve_mandate_for(conn, card["instrument_id"], mandate_id)
    if card["side"] == "SELL" and not perp_entry and not perp_reduce and not mandate_id and (mandate is not None or why == "NO_MANDATE"):
        # A SELL closes a holding, and REDUCE_RISK is allowed on a PAUSED or HALTED mandate (check_mandate and the
        # orchestrator gate). When the mandate that would be used holds none of the instrument, the stopped mandate
        # that holds it is the one the SELL closes in. An ACTIVE mandate that holds it is used as before
        async with pool.acquire() as conn:
            holds_it = mandate is not None and await _held_qty(conn, mandate.instance_id, card["instrument_id"]) >= MIN_HOLDING_QTY
            if not holds_it:
                holder, holder_why = await _holding_mandate_for_sell(conn, card["instrument_id"])
                if holder_why == "AMBIGUOUS_MANDATE":
                    raise HTTPException(status_code=409, detail=f"AMBIGUOUS_MANDATE: more than one stopped mandate holds "
                                        f"{card['instrument_id']}; nothing was queued; resume one of them in Mission "
                                        "Control, or reject this card")
                if holder is not None:
                    mandate, why = holder, ""
        # Otherwise: no mandate holds it, so NO_MANDATE below, or the ACTIVE mandate's SPOT_SELL_NOT_HELD further down
    if mandate is None and why == "NO_MANDATE" and (card["side"] == "BUY" or perp_entry):
        # A PAUSED or HALTED mandate on the instrument is the real reason: the operator stopped it. Say so, not "no
        # mandate", which would send the operator to create one inline, where the same stop refuses it
        async with pool.acquire() as conn:
            stopped = await _stopped_mandate_on(conn, [canonical_instrument(card["instrument_id"])])
        if stopped is not None:
            # The helper's reason is "<status>: <mandate name> already covers <instrument>; <tail>". A mandate name is
            # free text and may hold a ";", so cut at the last one: the tail is the helper's own and is never repeated
            reason = stopped.rsplit(";", 1)[0]
            raise HTTPException(status_code=409, detail=f"{reason}; resume it in Mission Control, then approve this card")
    if mandate is None and why == "AMBIGUOUS_MANDATE" and (card["side"] == "BUY" or perp_entry) and not mandate_id:
        # The card offers the choice (and stopping the overlap) itself: the detail says so, not "go to Mission Control"
        raise HTTPException(status_code=409, detail=f"AMBIGUOUS_MANDATE: more than one ACTIVE mandate allows "
                            f"{card['instrument_id']}; nothing was queued; choose the mandate on this card")
    if mandate is None:
        raise HTTPException(status_code=409, detail=f"{why}: approve this card inside an ACTIVE mandate that allows "
                            f"{card['instrument_id']} (Mission Control → Mandates)")

    # Spot is long-only: a SELL closes what the mandate holds, sized to that holding as a debate SELL is, never to the
    # budget. Refused before the card is claimed, so the operator rejects it here instead of finding a failed order later
    held = Decimal("0")
    if perp_reduce:
        # A perp reduction closes reduce_pct of the mandate's own position, on its closing side
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT side, quantity FROM paper_positions WHERE instance_id = $1 AND instrument_id = $2 AND status = 'OPEN'",
                mandate.instance_id, canonical_instrument(card["instrument_id"]))
        closing = {"LONG": "SELL", "SHORT": "BUY"}.get(row["side"]) if row else None
        if row is None or closing != card["side"]:
            raise HTTPException(status_code=409, detail=f"PERP_REDUCE_NOT_HELD: mandate {mandate.name} holds no "
                                f"{card['instrument_id']} position this {card['side']} closes; reject this card")
        held = (Decimal(str(row["quantity"])) * Decimal(int(card.get("reduce_pct") or 100)) / Decimal(100)).normalize()
    elif card["side"] == "SELL" and not perp_entry:
        async with pool.acquire() as conn:
            held = (await instance_spot_holdings(conn, mandate.instance_id)).get(
                canonical_instrument(card["instrument_id"]), (Decimal("0"), Decimal("0")))[0]
        if held < MIN_HOLDING_QTY:
            raise HTTPException(status_code=409, detail=f"SPOT_SELL_NOT_HELD: mandate {mandate.name} holds no "
                                f"{card['instrument_id']}, and a spot sell only closes a holding; reject this card")
        reduce_pct = card.get("reduce_pct")
        if reduce_pct in (25, 50):
            sell_qty = (held * Decimal(int(reduce_pct)) / Decimal(100)).normalize()
            if sell_qty < MIN_HOLDING_QTY:
                raise HTTPException(status_code=409, detail=f"SPOT_SELL_TOO_SMALL: {reduce_pct}% of {held:g} {card['instrument_id']} is below the minimum holding size; sell 100% or reject this card")
            held = sell_qty

    # One Brain decision, one order (D4): the Brain can emit the same order as several cards in one turn
    # (3 XRP cards of one trace were approved within 6 s); a twin of an approved card is refused, unclaimed
    trace = card.get("trace_id")
    trace_id = uuid.UUID(trace) if _is_uuid(trace) else None
    source_ref = f"{sid}:{card['id']}"  # _find_card matched this card by id: that is the widget id
    if trace_id is not None:
        async with pool.acquire() as conn:
            twin = await _approved_twin(conn, trace_id, card, source_ref)
        if twin is not None:
            raise HTTPException(status_code=409, detail=_duplicate_detail(twin))

    allocation = Decimal(str(card["allocation_pct"]))
    leverage_plan = None
    if perp_entry:
        # ADR 0011 D4: perps in a mandate whose markets include PERP, at any execution mode
        if not mandate.allow_perps:
            raise HTTPException(status_code=409, detail=(
                f"{MandateReject.PERPS_NOT_ALLOWED.value}: mandate {mandate.name} does not trade perps "
                f"(PERP is not in its markets); nothing was queued"))
    # An entry only gets the room the mandate has left under its position limit. An exit never does: a perp reduce
    # card closing a short is a BUY, but it is not an entry
    if (card["side"] == "BUY" and not perp_reduce) or perp_entry:
        # The orchestrator's check_mandate refuses an entry outside the mandate's markets or direction (a short in a
        # LONG-only mandate): the same rule, here, before the card is claimed
        if (scope := entry_scope_reject(mandate, card["instrument_id"], card["side"])) is not None:
            side = "long" if card["side"] == "BUY" else "short"
            raise HTTPException(status_code=409, detail=(
                f"{scope.value}: mandate {mandate.name} ({mandate.direction}, markets "
                f"{', '.join(sorted(getattr(m, "value", m) for m in mandate.markets))}) does not allow a {side} on "
                f"{card['instrument_id']}; nothing was queued"))
        # Another owner holding the instrument refuses the entry in the orchestrator's gate: refused here too, before
        # sizing and before the card is claimed, so the dry run and approve give the same answer
        async with pool.acquire() as conn:
            # ADR 0011 D6: the coin belongs to another profile (the gate refuses it at execution too)
            conflict = await coin_lock_conflict(conn, mandate, card["instrument_id"], "card")
            if conflict is None:
                conflict = await instrument_conflict(conn, mandate, card["instrument_id"],
                                                     Decimal(str(card.get("quantity") or 0)), "card")
        if conflict is not None:
            raise HTTPException(status_code=409, detail=conflict)

        stop_pct = card.get("stop_loss_pct")
        if stop_pct is None:
            stop_pct = protection.get("stop_loss_pct")
        # A mandate min-R:R requires the card to carry its own stop and target. The exit monitor's
        # default stop is still used for structural protection, but it is not a submitted RR input.
        has_stop_source = card.get("stop_loss") is not None or card.get("stop_loss_pct") is not None
        if mandate.min_rr is not None and not has_stop_source:
            # Keep the established refusal when this card has no stop to measure. The session and conviction checks
            # run first inside entry_filter_detail, just as they do for other entries.
            async with pool.acquire() as conn:
                refused = await entry_filter_detail(conn, mandate, "card", conviction=card.get("confidence"),
                                                    take_profit=card.get("take_profit"), side=card["side"],
                                                    require_inputs=True)
            if refused is not None:
                raise HTTPException(status_code=409, detail=refused)
        else:
            # Session and conviction refusals do not depend on market data. Preserve those operator-facing reasons
            # before requiring a fresh quote for stop resolution; R:R is checked below with the resolved stop.
            pre_stop_mandate = mandate.model_copy(update={"min_rr": None})
            async with pool.acquire() as conn:
                refused = await entry_filter_detail(conn, pre_stop_mandate, "card",
                                                    conviction=card.get("confidence"), side=card["side"],
                                                    require_inputs=True)
            if refused is not None:
                raise HTTPException(status_code=409, detail=refused)

        redis = getattr(getattr(request.app.state, "stream_hub", None), "redis", None)
        if perp_entry:
            mark, _ = await perp_mark(redis, card["instrument_id"])
            if mark is None or not mark.is_finite() or mark <= 0:
                no_mark_plan = await card_leverage_plan(pool, redis, mandate, card, requested_leverage=leverage)
                if not no_mark_plan.ok:
                    raise _PerpRefusal(f"{no_mark_plan.reject}: the {no_mark_plan.binding_ceiling or 'leverage plan'} "
                                       "allows no order for this card; nothing was queued", no_mark_plan.as_dict())

        if entry_requires_stop(card["instrument_id"], card["side"], reduce_only=perp_reduce):
            structural_stop = await resolve_entry_stop(
                pool,
                redis,
                card["instrument_id"],
                card["side"],
                explicit_stop=card.get("stop_loss"),
                stop_loss_pct=stop_pct,
                order_reference=(card.get("limit_price") if str(card.get("order_type", "")).upper() == "LIMIT" else None),
            )
            # The rest of the read-only plan, including R:R checks, must use the exact stop that will be queued.
            card = {**card, "stop_loss": format(structural_stop, "f")}

        # ADR 0010: the mandate's daily session, its conviction filter and its R:R filter. A card must carry what a
        # filter the mandate sets measures (a confidence; a stop and a target): without it the entry is refused
        redis = getattr(getattr(request.app.state, "stream_hub", None), "redis", None)
        take_profit = Decimal(str(card["take_profit"])) if card.get("take_profit") else None
        stop_loss = Decimal(str(card["stop_loss"])) if card.get("stop_loss") else None
        entry = await card_entry_price(redis, card) if (mandate.min_rr is not None and stop_loss and take_profit) else None
        async with pool.acquire() as conn:
            refused = await entry_filter_detail(conn, mandate, "card", conviction=card.get("confidence"), entry=entry,
                                                stop_loss=stop_loss, take_profit=take_profit, side=card["side"],
                                                require_inputs=True)
        if refused is not None:
            raise HTTPException(status_code=409, detail=refused)
        marks = await read_marks(getattr(getattr(request.app.state, "stream_hub", None), "redis", None))
        async with pool.acquire() as conn:
            room = await entry_room_pct(conn, mandate, card["instrument_id"], marks)
        if room < MIN_MANDATE_ENTRY_PCT:
            raise HTTPException(status_code=409, detail=(
                f"MANDATE_POSITION_LIMIT_REACHED: mandate {mandate.name} already holds its limit of "
                f"{mandate.max_position_pct}% in {card['instrument_id']}; nothing was queued"))
        if perp_entry:
            # Perps are sized by the leverage plan below (the card's allocation within the risk-at-stop budget); the
            # setup grade caps their leverage
            allocation = min(allocation, room.quantize(Decimal("0.01")))
        else:
            # O2: a spot or margin entry is sized by its setup grade as a debate entry is (A or B the profile's full
            # max position, C, NO_TRADE or unknown half), never the model's allocation; a NO_TRADE card stays
            # approvable (the card warns)
            allocation = Decimal(str(grade_size_pct(card.get("setup_grade"), mandate.max_position_pct,
                                                    room.quantize(Decimal("0.01")))))
    if perp_entry:
        redis = getattr(getattr(request.app.state, "stream_hub", None), "redis", None)
        plan = await card_leverage_plan(pool, redis, mandate, card, requested_leverage=leverage)
        leverage_plan = plan.as_dict()
        if not plan.ok:
            raise _PerpRefusal(f"{plan.reject}: the {plan.binding_ceiling or 'leverage plan'} allows no order "
                               f"for this card; nothing was queued", leverage_plan)
        allocation = (plan.margin_usd / mandate.capital_budget_usd * 100).quantize(Decimal("0.01"))
    budget = mandate.capital_budget_usd * allocation / 100
    lev = (leverage_plan or {}).get("leverage")
    summary = _card_summary(card, card["instrument_id"], mandate, allocation, budget, held, lev)
    shown = _card_summary(card, _shown_instrument(card), mandate, allocation, budget, held, lev)
    return CardPlan(mandate=mandate, allocation=allocation, budget=budget, summary=summary, shown=shown, held=held,
                    protection=protection, trace_id=trace_id, source_ref=source_ref, structural_stop=structural_stop,
                    leverage_plan=leverage_plan, perp_reduce=perp_reduce)


def _card_summary(card: dict[str, Any], instrument: str, mandate: Mandate, allocation: Decimal, budget: Decimal,
                  held: Decimal, lev: int | None = None) -> str:
    """What an approved card does, in words, for the instrument given (the card's own, or the shown one)."""
    if is_perp_instrument(card["instrument_id"]):
        if not is_perp_entry(card):
            return (f"{card['side']} {instrument} reduce-only {held:g} ({card.get('reduce_pct') or 100}% of the position) "
                    f"in mandate {mandate.name}")
        return (f"{'LONG' if card['side'] == 'BUY' else 'SHORT'} {instrument} {card['order_type']} "
                f"{f'{lev}x ' if lev else ''}ISOLATED, margin {allocation:g}% of mandate {mandate.name} (${budget:,.0f})")
    if card["side"] == "SELL":
        if card.get("reduce_pct") in (25, 50):
            return f"SELL {instrument} {card['order_type']} {int(card['reduce_pct'])}% ({held:g}) of the holding in mandate {mandate.name}"
        return f"SELL {instrument} {card['order_type']} the whole holding of {held:g} in mandate {mandate.name}"
    return (f"{card['side']} {instrument} {card['order_type']} at {allocation:g}% of mandate "
            f"{mandate.name} (${budget:,.0f})")


@router.post("/sessions/{sid}/cards/{widget_id}/approve", dependencies=OPERATOR_WRITE)
async def approve_order_card(sid: uuid.UUID, widget_id: str, request: Request, auth: dict = Depends(require_human_operator),
                             req: CardApproveRequest | None = None):
    """Approve an order card: one PAPER order inside a mandate, at most once, protected by the exit
    monitor's stops. The approval is recorded in ``ai_approvals`` and bound to its command (A5.3)."""
    op = operator_id(auth)
    if len(widget_id) > 128:
        raise HTTPException(status_code=422, detail="Invalid card id")
    card = await _get_card(request, op, sid, widget_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Order card not found")
    if card.get("status") == "APPROVED" and card.get("command_id"):
        return {"command_id": card["command_id"], "status": "APPROVED", "replayed": True}
    plan = await _plan_card(request, sid, card, req.mandate_id if req else None, req.leverage if req else None)
    mandate, allocation, summary, held = plan.mandate, plan.allocation, plan.summary, plan.held
    protection, trace_id, source_ref = plan.protection, plan.trace_id, plan.source_ref
    shown = plan.shown  # what the note lines say: the audit summary above keeps the card's own text
    pool = request.app.state.pool
    lev_plan = plan.leverage_plan
    if lev_plan is not None:
        # A leveraged entry: a second confirmation at 8x and above, then the operator's TOTP (ADR 0009 D3), both
        # before the card is claimed, so a refusal leaves it pending
        if lev_plan["leverage"] >= HIGH_LEVERAGE_CONFIRM and not (req and req.confirm_high_leverage):
            raise HTTPException(status_code=409, detail=(
                f"HIGH_LEVERAGE_CONFIRMATION_REQUIRED: {lev_plan['leverage']}x needs a second confirmation; "
                f"the loss if liquidated is the margin, ${Decimal(lev_plan['loss_if_liquidated_usd']):,.2f}"))
        await require_totp(request, auth, req.totp_code if req else None)

    if card.get("status") == "PENDING_APPROVAL":  # claim it first: a concurrent reject can no longer win
        claimed = await _worker_note(sid, {
            "operator_id": op, "widget_id": widget_id, "status": "APPROVED", "expect_status": "PENDING_APPROVAL",
            "content": f"Operator approved order card {widget_id}: {shown}.",
        })
        if claimed == 409:
            raise HTTPException(status_code=409, detail="The card is no longer pending")

    payload: dict[str, Any] = {
        "instrument_id": card["instrument_id"], "side": card["side"], "order_type": card["order_type"],
        # sized from the allocation the operator approved, as a share of the mandate's budget
        # (the mandate check and the dispatcher's 1-25% clamp still apply)
        "allocation_pct": float(allocation),
        "source": "brain_order_card", "brain_session_id": str(sid), "brain_card_id": widget_id,
        "trace_id": card.get("trace_id"),
        "proposed_take_profit": card.get("take_profit"), "protection": protection,
        # Stored with the order for scoring (leverage plan 3.3): the system's setup grade sets a perp's leverage
        # ceiling; the model's confidence never does
        "setup_grade": card.get("setup_grade"), "grade_reasons": list(card.get("grade_reasons") or [])[:12],
        "confidence": card.get("confidence"),
    }
    if plan.structural_stop is not None:
        stop_text = format(plan.structural_stop, "f")
        payload.update({"stop_loss": stop_text, "proposed_stop_loss": stop_text})
    elif card.get("stop_loss") is not None:
        # Existing protection on an exit is informational; exits do not require a stop.
        payload["proposed_stop_loss"] = card["stop_loss"]
    if card["order_type"] == "LIMIT":
        payload["price"] = card["limit_price"]
    if lev_plan is not None:
        # The orchestrator sizes it again with the same plan, at most at this leverage; the entry carries its stop,
        # which becomes a resting stop once it fills (ISOLATED only for mandate perps, D5)
        payload.update({"leverage": lev_plan["leverage"], "margin_mode": "ISOLATED",
                        "leverage_plan": {k: lev_plan[k] for k in ("leverage", "binding_ceiling", "notional_usd",
                                                                   "margin_usd", "liquidation_price",
                                                                   "loss_at_stop_usd")}})
    elif plan.perp_reduce:
        payload.update({"quantity": format(held, "f"), "reduce_only": True})
    elif card["side"] == "SELL":
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

    guard = (f"a resting stop at {card.get('stop_loss')} (on the mark) and the exit monitor's liquidation-proximity "
             f"close protect the position." if lev_plan is not None else
             f"the exit monitor's {protection.get('stop_loss_pct')}% hard stop and {protection.get('trail_pct')}% "
             f"trailing stop (after +{protection.get('trail_activate_pct')}%) protect the position.")
    await _worker_note(sid, {
        "operator_id": op, "widget_id": widget_id, "status": "APPROVED", "command_id": command_id,
        "expect_status": "APPROVED",
        "content": f"Paper command {command_id} queued for the dispatcher inside mandate {mandate.name} ({shown}); {guard}",
    })
    return {"command_id": command_id, "status": "APPROVED", "protection": protection, "mandate_id": str(mandate.id),
            "approval_id": str(decision.approval_id),
            "leverage": lev_plan["leverage"] if lev_plan is not None else None}


def _refusal_code(detail: str) -> str:
    """The reason code a refusal's detail starts with: "MANDATE_PAUSED: ..." is MANDATE_PAUSED. The details that have
    no "CODE:" prefix are the card's own status ("The card is REJECTED", NOT_PENDING) and, for any other, REFUSED."""
    if detail.startswith("The card is "):
        return "NOT_PENDING"
    code, sep, _ = detail.partition(":")
    return code if sep else "REFUSED"


MAX_MANDATE_INSTRUMENTS = 20  # MandatePatch's instruments limit: a mandate this full cannot take one more


async def _card_remedy(request: Request, card: dict[str, Any], code: str, mandate_id: uuid.UUID | None) -> dict[str, Any] | None:
    """The mandate change that would let approve queue a refused BUY, for the refusals a mandate change fixes; None
    for the others. Reads only, like the dry run. The fix stays the operator's: the card sends it through the mandate
    routes (TOTP, the hard caps, the audit event), then approves, and approve checks everything again.

    - NO_MANDATE: create a mandate for the instrument, or add it to an ACTIVE mandate with room for it.
    - MANDATE_POSITION_LIMIT_REACHED: the one edit ``room_remedy`` picks (the position limit, else the budget).
    - MANDATE_PAUSED: resume it. A HALTED mandate hit its hard stop; that is looked at in Mission Control, not here."""
    if card["side"] != "BUY" and not is_perp_entry(card):
        return None
    instrument = canonical_instrument(card["instrument_id"])
    pool = request.app.state.pool
    marks = await read_marks(getattr(getattr(request.app.state, "stream_hub", None), "redis", None))
    if code == "NO_MANDATE":
        add_to = []
        async with pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE status = 'ACTIVE' ORDER BY created_at")
            for row in rows:
                m = Mandate.from_row(row)
                if len(m.instruments) >= MAX_MANDATE_INSTRUMENTS or (is_perp_instrument(instrument) and not m.allow_perps):
                    continue
                room = await entry_room_pct(conn, m, instrument, marks)
                if room >= MIN_MANDATE_ENTRY_PCT:
                    add_to.append({"id": str(m.id), "name": m.name, "autonomy_level": int(m.autonomy_level),
                                   "instruments": m.instruments, "max_position_pct": float(m.max_position_pct),
                                   "capital_budget_usd": float(m.capital_budget_usd),
                                   "room_pct": float(room.quantize(Decimal("0.01")))})
        # ADR 0009 D3: a new mandate (L1) may allow perps, so a perp card can create one with the perps opt-in
        return {"kind": "CREATE_MANDATE", "can_create": True, "allow_perps": is_perp_instrument(instrument),
                "position_pct": float(MAX_POSITION_PCT_CAP), "add_to": add_to}
    if code == MandateReject.PERPS_NOT_ALLOWED.value:
        async with pool.acquire() as conn:
            mandate, _ = await resolve_mandate_for(conn, instrument, mandate_id)
        # ADR 0011 D4: offered at any execution mode; the edit adds PERP to the profile's markets
        if mandate is None:
            return None
        return {"kind": "ALLOW_PERPS", "mandate_id": str(mandate.id), "mandate_name": mandate.name,
                "autonomy_level": int(mandate.autonomy_level), "max_leverage_after": max(mandate.max_leverage, 3)}
    if code == MandateReject.POSITION_LIMIT_REACHED:
        async with pool.acquire() as conn:
            mandate, _ = await resolve_mandate_for(conn, instrument, mandate_id)
            if mandate is None:
                return None
            inst, gross = await held_values(conn, mandate, instrument, marks)
        requested = Decimal(str(card["allocation_pct"]))
        fix = room_remedy(mandate.capital_budget_usd, mandate.max_position_pct, inst, gross, requested)
        if fix is None:
            return None
        return {"kind": "EDIT_MANDATE", "mandate_id": str(mandate.id), "mandate_name": mandate.name,
                "autonomy_level": int(mandate.autonomy_level), "field": fix.field, "from": float(fix.old),
                "to": float(fix.new), "max_position_pct": float(mandate.max_position_pct),
                "capital_budget_usd": float(mandate.capital_budget_usd),
                "held_pct": float((inst / mandate.capital_budget_usd * 100).quantize(Decimal("0.01"))),
                "requested_pct": float(requested),
                "allocation_after_pct": float(min(requested, fix.room_after_pct).quantize(Decimal("0.01")))}
    if code == "AMBIGUOUS_MANDATE":
        return await choose_mandate_remedy(pool, instrument, Decimal(str(card["allocation_pct"])), marks)
    if code == "MANDATE_PAUSED":
        async with pool.acquire() as conn:  # the stopped mandate approve named: the first, by creation
            row = await conn.fetchrow(
                """SELECT id, name, status FROM ai_mandates WHERE status IN ('PAUSED', 'HALTED') AND $1 = ANY(instruments)
                   ORDER BY created_at LIMIT 1""", instrument)
        if row is None or row["status"] != "PAUSED":
            return None
        return {"kind": "RESUME_MANDATE", "mandate_id": str(row["id"]), "mandate_name": row["name"]}
    return None


@router.get("/sessions/{sid}/cards/{widget_id}/preflight")
async def preflight_order_card(sid: uuid.UUID, widget_id: str, request: Request, auth: dict = Depends(require_operator),
                               mandate_id: uuid.UUID | None = Query(default=None),
                               leverage: int | None = Query(default=None, ge=1, le=MAX_PERP_LEVERAGE_CAP)):
    """The dry run before approve (WS-3A): the mandate and size approve would queue for this card now, or the code and
    detail of the refusal approve would give. Read only: it claims, notes, records and queues nothing, so the operator
    sees the outcome before pressing Approve. Auth is an operator session or an operator-scoped machine token, as the
    other reads of this router; approve itself stays human-only."""
    if len(widget_id) > 128:
        raise HTTPException(status_code=422, detail="Invalid card id")
    card = await _get_card(request, operator_id(auth), sid, widget_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Order card not found")
    if card.get("status") != "PENDING_APPROVAL":  # approve refuses such a card before it plans it too
        detail = f"The card is {card.get('status')}"
        return {"ok": False, "code": "NOT_PENDING", "detail": detail, "needs_mandate": False, "remedy": None}
    try:
        plan = await _plan_card(request, sid, card, mandate_id, leverage)
    except HTTPException as e:
        detail = str(e.detail)
        code = _refusal_code(detail)
        # approve's NO_MANDATE for a BUY makes the card offer to create a mandate; the remedy says how (and which
        # other refusals a mandate change fixes)
        needs_mandate = code == "NO_MANDATE" and (card["side"] == "BUY" or is_perp_entry(card))
        remedy = await _card_remedy(request, card, code, mandate_id)
        return {"ok": False, "code": code, "detail": detail, "needs_mandate": needs_mandate, "remedy": remedy,
                "leverage_plan": getattr(e, "leverage_plan", None), "needs_totp": is_perp_entry(card)}
    # A spot or margin entry asks its grade size (O2), so the room left cut it from that; a perp asks its own allocation
    max_pct = plan.mandate.max_position_pct
    requested = (Decimal(str(card["allocation_pct"])) if plan.leverage_plan is not None
                 else Decimal(str(grade_size_pct(card.get("setup_grade"), max_pct, max_pct))))
    if plan.leverage_plan is not None:
        lp = plan.leverage_plan
        asked = leverage if leverage is not None else card.get("leverage")
        # The mandate's own maximum cut the request: the card offers raising it (TOTP), up to what was asked (10x cap)
        leverage_remedy = None
        if lp["binding_ceiling"] == "mandate maximum" and asked and int(asked) > plan.mandate.max_leverage:
            leverage_remedy = {"kind": "RAISE_MAX_LEVERAGE", "mandate_id": str(plan.mandate.id),
                               "mandate_name": plan.mandate.name, "from": plan.mandate.max_leverage,
                               "to": min(int(asked), MAX_PERP_LEVERAGE_CAP)}
        return {
            "ok": True, "mandate": {"id": str(plan.mandate.id), "name": plan.mandate.name,
                                    "status": str(plan.mandate.status)},
            "side": card["side"], "allocation_pct": float(plan.allocation), "budget_usd": float(plan.budget),
            "clipped_from_pct": float(requested) if plan.allocation < requested else None,
            "sell_quantity": None, "summary": plan.summary, "leverage_plan": lp, "needs_totp": True,
            "high_leverage_confirm": lp["leverage"] >= HIGH_LEVERAGE_CONFIRM, "leverage_remedy": leverage_remedy,
        }
    return {
        "ok": True, "mandate": {"id": str(plan.mandate.id), "name": plan.mandate.name,
                                "status": str(plan.mandate.status)},
        "side": card["side"], "allocation_pct": float(plan.allocation), "budget_usd": float(plan.budget),
        "clipped_from_pct": float(requested) if card["side"] == "BUY" and plan.allocation < requested else None,
        "sell_quantity": format(plan.held, "f") if card["side"] == "SELL" else None, "summary": plan.summary,
    }


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
