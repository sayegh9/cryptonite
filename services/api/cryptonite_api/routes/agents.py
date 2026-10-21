import datetime
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import httpx
from cryptonite_api.mandate_choice import (
    choose_mandate_remedy,
    entry_filter_detail,
    instrument_conflict,
)
from cryptonite_api.routes.auth import (
    OPERATOR_WRITE,
    require_auth,
    require_csrf,
    require_human_operator,
    require_operator,
)
from cryptonite_api.similar import similar_or_none
from cryptonite_execution.approvals import (
    ApprovalConflict,
    decide_approval,
    record_operator_approval,
)
from cryptonite_execution.mandate_store import entry_room_pct, resolve_mandate_for
from cryptonite_schemas.debate import verdict_direction
from cryptonite_schemas.mandate import (
    MIN_MANDATE_ENTRY_PCT,
    ActionClass,
    Mandate,
    is_perp_instrument,
)
from cryptonite_schemas.marks import read_marks
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

# Ensure HMAC secret exists
INTERNAL_HMAC_SECRET = os.getenv("INTERNAL_HMAC_SECRET", "default_internal_secret")
DEBATE_QUEUE_MAXLEN = 10_000  # same cap as the scanner's writer (D1 / F-H2): XADD trims the oldest entries
# WS-1E: debates the Brain starts (origin "brain") per UTC day. Operator triggers are not counted.
BRAIN_DEBATE_DAILY_CAP = int(os.getenv("BRAIN_DEBATE_DAILY_CAP", "6"))
BRAIN_DEBATE_CAP_TTL_S = 2 * 86400  # a day's counter expires two days after it was last touched

class ChatRequest(BaseModel):
    message: str

class ExplainRequest(BaseModel):
    backtest_id: str
    message: str

class StrategistRequest(BaseModel):
    message: str
    instructions: str | None = None

class CRORequest(BaseModel):
    message: str

class OnChainRequest(BaseModel):
    message: str

class DebateTriggerRequest(BaseModel):
    symbol: str
    timeframe: str = "1m"
    custom_thesis: str | None = None
    origin: Literal["operator", "brain"] = "operator"

class ApproveRequestOverrides(BaseModel):
    allocation_pct: float | None = Field(default=None, ge=1.0, le=25.0)  # % of the mandate's budget
    # AMBIGUOUS_MANDATE: the ACTIVE mandate (allowing the instrument) the operator chose in the Inbox
    mandate_id: uuid.UUID | None = None
    order_type: str | None = None
    limit_offset_pct: float | None = None
    stop_loss_pct: float | None = None

def sign_request(method: str, path: str, body: bytes) -> dict:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(8)
    body_digest = hashlib.sha256(body).hexdigest()
    
    msg = f"{method}:{path}:{body_digest}:{timestamp}:{nonce}".encode()
    signature = hmac.new(INTERNAL_HMAC_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    
    return {
        "X-Internal-Timestamp": timestamp,
        "X-Internal-Nonce": nonce,
        "X-Internal-Signature": signature
    }

async def forward_request(path: str, req_body: BaseModel):
    method = "POST"
    body_bytes = req_body.model_dump_json().encode("utf-8")
    headers = sign_request(method, path, body_bytes)
    headers["Content-Type"] = "application/json"
    
    worker_url = f"http://cryptonite_agent_worker:8085{path}"
    
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(worker_url, content=body_bytes, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Failed to forward agent request to {worker_url}: {e}")
        raise HTTPException(status_code=500, detail="Internal agent worker error")

@router.post("/chat", dependencies=OPERATOR_WRITE)
async def agent_chat(req: ChatRequest, request: Request):
    return await forward_request("/api/v1/agents/chat", req)

@router.post("/explain", dependencies=OPERATOR_WRITE)
async def agent_explain(req: ExplainRequest, request: Request):
    return await forward_request("/api/v1/agents/explain", req)

@router.post("/strategist", dependencies=OPERATOR_WRITE)
async def agent_strategist(req: StrategistRequest, request: Request):
    return await forward_request("/api/v1/agents/strategist", req)

@router.post("/cro", dependencies=OPERATOR_WRITE)
async def agent_cro(req: CRORequest, request: Request):
    return await forward_request("/api/v1/agents/cro", req)

@router.post("/onchain", dependencies=OPERATOR_WRITE)
async def agent_onchain(req: OnChainRequest, request: Request):
    return await forward_request("/api/v1/agents/onchain", req)

@router.post("/debates/trigger", dependencies=OPERATOR_WRITE)
async def trigger_debate(req: DebateTriggerRequest, request: Request):
    inst_id = req.symbol if "." in req.symbol else f"{req.symbol.upper()}.BINANCE"
    
    # Resolve Redis client safely from app state
    redis_client = getattr(request.app.state, "redis", None)
    if not redis_client and hasattr(request.app.state, "stream_hub") and request.app.state.stream_hub:
        redis_client = request.app.state.stream_hub.redis
        
    if not redis_client:
        logger.error("Redis client not available on app.state")
        raise HTTPException(status_code=503, detail="Redis connection unavailable")
    
    brain = req.origin == "brain"
    day_key = f"brain:debates:{datetime.datetime.now(datetime.UTC):%Y-%m-%d}"
    if brain:
        # WS-1E: the day's count is taken before the queue write; a refused or failed trigger gives its slot back
        try:
            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.incr(day_key)
                pipe.expire(day_key, BRAIN_DEBATE_CAP_TTL_S)
                count, _ = await pipe.execute()
            over_cap = count > BRAIN_DEBATE_DAILY_CAP
            if over_cap:
                await redis_client.decr(day_key)
        except Exception as e:
            logger.error(f"Brain debate cap unavailable for {inst_id}: {e}")
            raise HTTPException(status_code=503, detail="Brain debate cap unavailable: Redis error; nothing was queued")
        if over_cap:
            raise HTTPException(
                status_code=429,
                detail=(f"Brain debate cap reached ({BRAIN_DEBATE_DAILY_CAP} per day); "
                        "ask the operator to start it from the Debates page"),
            )

    payload = {
        "instrument_id": inst_id,
        "trigger_source": "brain_on_demand" if brain else "user_on_demand",
        "timeframe": req.timeframe,
        "custom_thesis": req.custom_thesis or "",
        "timestamp": str(time.time()),
    }
    
    try:
        msg_id = await redis_client.xadd("stream:debate_queue", payload, maxlen=DEBATE_QUEUE_MAXLEN, approximate=True)
        logger.info(f"Queued on-demand debate for {inst_id} (msg ID: {msg_id})")
        return {"status": "queued", "id": str(msg_id), "instrument_id": inst_id, "payload": payload}
    except Exception as e:
        if brain:
            await redis_client.decr(day_key)  # nothing was queued, so the slot goes back
        logger.error(f"Failed to queue debate trigger for {inst_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to queue debate: {str(e)}")

@router.post("/debates/exit/trigger")
async def trigger_exit_debate(req: DebateTriggerRequest, request: Request, _op=Depends(require_operator), _csrf=Depends(require_csrf)):
    inst_id = req.symbol if "." in req.symbol else f"{req.symbol.upper()}.BINANCE"
    
    redis_client = getattr(request.app.state, "redis", None)
    if not redis_client and hasattr(request.app.state, "stream_hub") and request.app.state.stream_hub:
        redis_client = request.app.state.stream_hub.redis
        
    if not redis_client:
        logger.error("Redis client not available on app.state")
        raise HTTPException(status_code=503, detail="Redis connection unavailable")
    
    run_id = uuid.uuid4()
    
    payload = {
        "instrument_id": inst_id,
        "run_id": str(run_id)
    }
    
    try:
        pool = request.app.state.pool
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at) VALUES ($1, $2, 'RUNNING', 'user_on_demand', now())",
                run_id, inst_id
            )
            
        msg_id = await redis_client.xadd("stream:exit_debate_queue", payload, maxlen=DEBATE_QUEUE_MAXLEN, approximate=True)
        logger.info(f"Queued exit debate for {inst_id} (msg ID: {msg_id})")
        return {"status": "queued", "id": str(msg_id), "run_id": str(run_id), "instrument_id": inst_id, "payload": payload}
    except Exception as e:
        logger.error(f"Failed to queue exit debate for {inst_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to queue exit debate: {str(e)}")

PAPER_ACCOUNT_ID = "PAPER-ACCOUNT-001"


def order_side(recommendation: str | None) -> str | None:
    """The order side of a stored verdict (PM or CRO wording, any case; ``normalize_verdict``'s mapping, B2),
    so the approve endpoint and routing cannot disagree. None for HOLD or an unknown verdict."""
    direction = verdict_direction(recommendation)
    return direction if direction in ("BUY", "SELL") else None


def _get_redis(request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    if not redis_client and getattr(request.app.state, "stream_hub", None):
        redis_client = request.app.state.stream_hub.redis
    return redis_client


async def _fetch_decision_price(redis_client, sym: str) -> float | None:
    if redis_client:
        try:
            cached_market = await redis_client.get(f"market:24h:{sym}")
            if cached_market:
                price = float(json.loads(cached_market).get("last_price", 0))
                if price > 0:
                    return price
        except Exception:
            pass
    try:
        async with httpx.AsyncClient(timeout=4.0) as http_client:
            bn_r = await http_client.get("https://api.binance.com/api/v3/ticker/24hr", params={"symbol": sym})
            if bn_r.status_code == 200:
                price = float(bn_r.json().get("lastPrice", 0))
                return price if price > 0 else None
    except Exception as e:
        logger.warning(f"Could not fetch live price for {sym}: {e}")
    return None


# The operator's allocation of a debate BUY, as % of the mandate's budget: the Inbox slider's bounds
MIN_DEBATE_ALLOCATION_PCT = Decimal("1")
MAX_DEBATE_ALLOCATION_PCT = Decimal("25")


@dataclass(frozen=True)
class DebatePlan:
    """A debate's order as approve sizes it now. Built only by ``_plan_debate``, which reads and never writes."""
    mandate: Mandate
    side: str
    requested: Decimal  # % of the mandate budget asked for: the operator's, else the decision's, within 1-25%
    allocation: Decimal  # the requested %, cut to the room a BUY has left under the mandate's position limit
    room: Decimal | None  # % of the mandate budget a BUY has left (None for a SELL)
    held: Decimal  # the mandate's holding a SELL sells (0 for a BUY)

    @property
    def budget(self) -> Decimal:
        return self.mandate.capital_budget_usd * self.allocation / 100


def _run_uuid(run_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid run_id format")


async def _load_debate(pool, run_uuid: uuid.UUID):
    async with pool.acquire() as conn:
        run = await conn.fetchrow("SELECT status, decision_trace_id, mandate_id FROM debate_runs WHERE id = $1", run_uuid)
        if not run:
            raise HTTPException(status_code=404, detail="Debate run not found")
        decision = await conn.fetchrow(
            "SELECT instrument_id, recommendation, conviction_score, allocation_pct FROM portfolio_decisions WHERE run_id = $1",
            run_uuid)
        if not decision:
            raise HTTPException(status_code=404, detail="No portfolio decision found for this debate run")
    return run, decision


def _requested_pct(decision, allocation_pct: float | None) -> Decimal:
    raw = Decimal(str(allocation_pct if allocation_pct is not None else decision["allocation_pct"] or 0))
    return min(max(raw, MIN_DEBATE_ALLOCATION_PCT), MAX_DEBATE_ALLOCATION_PCT)


async def _plan_debate(request: Request, run, decision, mandate_id: uuid.UUID | None,
                       allocation_pct: float | None) -> DebatePlan:
    """Every mandate check approve makes before it records the approval, in the same order, with the same refusals.
    Reads only, so the Inbox's sizing read cannot disagree with approve.

    Every AI order trades inside a mandate (A5.1): the one the debate named, else the one the operator chose
    (``mandate_id``), else the single ACTIVE mandate that allows the instrument. A BUY is sized as a share of that
    mandate's budget, as Brain cards and the orchestrator size a mandate order, cut to the room it has left."""
    side = order_side(decision["recommendation"])
    if side is None:
        raise HTTPException(status_code=409, detail=f"Recommendation '{decision['recommendation']}' has no order to "
                            "execute; reject the debate instead")
    instrument = decision["instrument_id"]
    named = run["mandate_id"]
    if named is not None and mandate_id is not None and mandate_id != named:
        # The debate worker routed the run into its mandate and opened its approval there: that binds the order
        raise HTTPException(status_code=409, detail="MANDATE_MISMATCH: this debate was routed into another mandate; "
                            "approve it without choosing one")
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        mandate, why = await resolve_mandate_for(conn, instrument, named or mandate_id)
    if mandate is None and why == "AMBIGUOUS_MANDATE" and side == "BUY":
        # The Inbox offers the choice itself: the detail says so, not "go to Mission Control"
        raise HTTPException(status_code=409, detail=f"AMBIGUOUS_MANDATE: more than one ACTIVE mandate allows "
                            f"{instrument}; nothing was queued; choose the mandate in the Inbox")
    if mandate is None:
        raise HTTPException(status_code=409, detail=f"{why}: approve this debate inside an ACTIVE mandate that allows "
                            f"{instrument} (Mission Control → Mandates)")
    requested = _requested_pct(decision, allocation_pct)
    if side == "SELL":
        # spot is long-only: a debate SELL sells the mandate's own holding, never a budget-sized amount
        async with pool.acquire() as conn:
            held = await conn.fetchval(
                """SELECT COALESCE(SUM(CASE WHEN side = 'BUY' THEN filled_qty ELSE -filled_qty END), 0) FROM paper_orders
                   WHERE instance_id = $1 AND instrument_id = $2 AND filled_qty > 0""",
                mandate.instance_id, instrument)
        if not held or held <= 0:
            raise HTTPException(status_code=409, detail="The mandate holds nothing to sell; reject the debate instead")
        return DebatePlan(mandate=mandate, side=side, requested=requested, allocation=requested, room=None,
                          held=Decimal(str(held)).normalize())
    # Another owner holding the instrument refuses the entry in the orchestrator's gate: refused here first
    async with pool.acquire() as conn:
        conflict = await instrument_conflict(conn, mandate, instrument, Decimal("0"), "debate")
    if conflict is not None:
        raise HTTPException(status_code=409, detail=conflict)
    # ADR 0010: the mandate's daily session and its conviction filter refuse the entry as the debate worker would
    async with pool.acquire() as conn:
        refused = await entry_filter_detail(conn, mandate, "debate", conviction=decision["conviction_score"])
    if refused is not None:
        raise HTTPException(status_code=409, detail=refused)
    if mandate.min_rr is not None:  # a debate carries no stop or target: the R:R filter cannot measure it
        logger.info("Debate approval on %s: R:R filter of mandate %s skipped: min_rr skipped: no stop/target",
                    instrument, mandate.name)
    marks = await read_marks(_get_redis(request))
    async with pool.acquire() as conn:
        room = await entry_room_pct(conn, mandate, instrument, marks)
    if room < MIN_MANDATE_ENTRY_PCT:
        raise HTTPException(status_code=409, detail=(
            f"MANDATE_POSITION_LIMIT_REACHED: mandate {mandate.name} already holds its limit of "
            f"{mandate.max_position_pct}% in {instrument}; nothing was queued"))
    return DebatePlan(mandate=mandate, side=side, requested=requested,
                      allocation=min(requested, room.quantize(Decimal("0.01"))), room=room, held=Decimal("0"))


PERP_APPROVAL_IN_QUEUE = ("PERP_APPROVAL_IN_QUEUE: the debate worker routed this verdict to a perps mandate; its "
                          "approval waits in Mission Control's approval queue, where approving a perp entry needs "
                          "your TOTP")


async def _pending_perp_approval(pool, run_uuid: uuid.UUID) -> uuid.UUID | None:
    """The PENDING approval the debate worker opened for this run on a perpetual (Phase B), or None. The Inbox does
    not decide it: its sizing reads the spot instrument, and the perp entry's TOTP is asked in the approval queue."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, payload->>'instrument_id' AS instrument_id FROM ai_approvals
               WHERE source = 'debate' AND source_ref = $1 AND status = 'PENDING'""", str(run_uuid))
    return row["id"] if row and row["instrument_id"] and is_perp_instrument(row["instrument_id"]) else None


def _mandate_view(m: Mandate) -> dict:
    return {"id": str(m.id), "name": m.name, "autonomy_level": int(m.autonomy_level),
            "capital_budget_usd": float(m.capital_budget_usd), "max_position_pct": float(m.max_position_pct)}


@router.get("/debates/{run_id}/sizing")
async def debate_sizing(run_id: str, request: Request, mandate_id: uuid.UUID | None = Query(default=None),
                        allocation_pct: float | None = Query(default=None, ge=1.0, le=25.0), _auth=Depends(require_auth)):
    """What approving the debate would do now (the Inbox's dry run): the mandate it trades in, the room a BUY has
    left there and the size it would get, as % of that mandate's budget; or the code and detail of the refusal approve
    would give, with the mandates to choose from when more than one ACTIVE mandate allows the instrument. Read only."""
    run_uuid = _run_uuid(run_id)
    run, decision = await _load_debate(request.app.state.pool, run_uuid)
    if run["status"] != "PENDING_APPROVAL":
        return {"ok": False, "code": "NOT_PENDING", "detail": f"The debate is {run['status']}", "remedy": None}
    if await _pending_perp_approval(request.app.state.pool, run_uuid) is not None:
        return {"ok": False, "code": "PERP_APPROVAL_IN_QUEUE", "detail": PERP_APPROVAL_IN_QUEUE, "remedy": None}
    try:
        plan = await _plan_debate(request, run, decision, mandate_id, allocation_pct)
    except HTTPException as e:
        detail = str(e.detail)
        code, sep, _ = detail.partition(":")
        code = code if sep and code.isupper() else "REFUSED"
        remedy = None
        if code == "AMBIGUOUS_MANDATE":
            marks = await read_marks(_get_redis(request))
            remedy = await choose_mandate_remedy(request.app.state.pool, decision["instrument_id"],
                                                 _requested_pct(decision, allocation_pct), marks)
        return {"ok": False, "code": code, "detail": detail, "remedy": remedy}
    m = plan.mandate
    return {
        "ok": True, "side": plan.side, "mandate": _mandate_view(m),
        "room_pct": float(plan.room.quantize(Decimal("0.01"))) if plan.room is not None else None,
        "room_usd": float((m.capital_budget_usd * plan.room / 100).quantize(Decimal("0.01"))) if plan.room is not None else None,
        "requested_pct": float(plan.requested), "allocation_pct": float(plan.allocation),
        "budget_usd": float(plan.budget.quantize(Decimal("0.01"))),
        "sell_quantity": format(plan.held, "f") if plan.side == "SELL" else None,
    }


@router.post("/debates/{run_id}/approve")
async def approve_debate(
    run_id: str,
    request: Request,
    overrides: ApproveRequestOverrides | None = None,
    _op=Depends(require_human_operator),
    _csrf=Depends(require_csrf)
):
    pool = request.app.state.pool
    run_uuid = _run_uuid(run_id)
    run, decision = await _load_debate(pool, run_uuid)
    if run['status'] != 'PENDING_APPROVAL':
        raise HTTPException(status_code=409, detail=f"Debate run has invalid status for approval: {run['status']}")
    if await _pending_perp_approval(pool, run_uuid) is not None:
        raise HTTPException(status_code=409, detail=PERP_APPROVAL_IN_QUEUE)

    plan = await _plan_debate(request, run, decision, overrides.mandate_id if overrides else None,
                              overrides.allocation_pct if overrides else None)
    mandate, side = plan.mandate, plan.side

    sym = decision['instrument_id'].split(".")[0].upper()
    redis_client = _get_redis(request)
    decision_price = await _fetch_decision_price(redis_client, sym)
    if not decision_price:
        raise HTTPException(status_code=503, detail="Could not fetch live price for sizing.")

    # A BUY carries its allocation, as % of the mandate's budget, and no quantity: the orchestrator sizes it from the
    # budget and cuts it to the mandate's room at execution, as it does a Brain card's (the dispatcher's 1-25% equity
    # clamp still applies)
    payload = {
        "instrument_id": decision['instrument_id'],
        "symbol": sym,
        "side": side,
        "price": float(decision_price),
        "order_type": overrides.order_type if overrides and overrides.order_type else "LIMIT",
        "recommendation": decision['recommendation'],
        "conviction_score": float(decision['conviction_score']),
        "allocation_pct": float(plan.allocation),
        "account_id": PAPER_ACCOUNT_ID,
        "mandate_id": str(mandate.id),
    }
    if run['decision_trace_id']:  # R7.4: candidate -> debate -> approval -> command -> fill
        payload["decision_trace_id"] = str(run['decision_trace_id'])

    if overrides:
        if overrides.limit_offset_pct is not None:
            payload["limit_offset_pct"] = overrides.limit_offset_pct
        if overrides.stop_loss_pct is not None:
            payload["stop_loss_pct"] = overrides.stop_loss_pct

    if side == "SELL":
        payload.update(quantity=float(plan.held), order_type="MARKET")
        payload.pop("price", None)
        payload.pop("allocation_pct", None)
        summary = f"Debate verdict: SELL {decision['instrument_id']} the whole holding of {plan.held:g} in mandate {mandate.name}"
    else:
        summary = (f"Debate verdict: BUY {decision['instrument_id']} at {float(plan.allocation):g}% of mandate "
                   f"{mandate.name} (${plan.budget:,.0f})")

    # The debate worker may already have opened an approval for this run (mandate L1/L2, A5.7):
    # approving it here decides that approval, so the run never gets a second order.
    async with pool.acquire() as conn:
        pending_id = await conn.fetchval(
            "SELECT id FROM ai_approvals WHERE source = 'debate' AND source_ref = $1 AND status = 'PENDING'", str(run_uuid))
        if pending_id is not None:
            try:
                d = await decide_approval(conn, pending_id, True, via="web", decided_by="operator:debate_approval")
            except ApprovalConflict as e:
                raise HTTPException(status_code=409, detail=f"The debate's approval is {e.status}")
            return {"status": "approved", "run_id": str(run_id), "approval_id": str(d.approval_id),
                    "command_id": str(d.command_id) if d.command_id else None}

    # Mark the run decided and enqueue its order atomically, so a run is never
    # COMPLETED without an order (or approved twice by concurrent requests).
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.execute(
                "UPDATE debate_runs SET status = 'COMPLETED', completed_at = now() WHERE id = $1 AND status = 'PENDING_APPROVAL'",
                run_uuid,
            )
            if updated != "UPDATE 1":
                raise HTTPException(status_code=409, detail="Debate run was already decided")
            await record_operator_approval(
                conn, mandate, "SUBMIT_ORDER", payload,
                summary=summary,
                source="debate", source_ref=str(run_uuid), decided_by="operator:debate_approval",
                idempotency_key=f"debate_approve:{run_id}",
                action=ActionClass.ADD_RISK if side == "BUY" else ActionClass.REDUCE_RISK,
            )

    if redis_client:
        await redis_client.publish(f"channel:debates:{decision['instrument_id']}", json.dumps({
            "type": "DEBATE_APPROVED",
            "run_id": str(run_id)
        }))

    logger.info("Debate %s approved (decision_trace_id=%s)", run_id, payload.get("decision_trace_id"))
    return {"status": "approved", "run_id": str(run_id), **payload, "mandate_name": mandate.name,
            "budget_usd": float(plan.budget.quantize(Decimal("0.01"))) if side == "BUY" else None}

@router.post("/debates/{run_id}/reject")
async def reject_debate(run_id: str, request: Request, _op=Depends(require_operator), _csrf=Depends(require_csrf)):
    pool = request.app.state.pool
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid run_id format")
        
    async with pool.acquire() as conn:
        run = await conn.fetchrow("SELECT status FROM debate_runs WHERE id = $1", run_uuid)
        if not run:
            raise HTTPException(status_code=404, detail="Debate run not found")
        if run['status'] == 'REJECTED':
            return {"status": "rejected", "run_id": str(run_id)}
        if run['status'] == 'COMPLETED':
            raise HTTPException(status_code=409, detail="Cannot reject an already completed/executed debate run")
            
        await conn.execute("UPDATE debate_runs SET status = 'REJECTED', completed_at = now() WHERE id = $1", run_uuid)
        decision = await conn.fetchrow("SELECT instrument_id FROM portfolio_decisions WHERE run_id = $1", run_uuid)
        
    redis_client = getattr(request.app.state, "redis", None)
    if not redis_client and hasattr(request.app.state, "stream_hub") and request.app.state.stream_hub:
        redis_client = request.app.state.stream_hub.redis
    if redis_client and decision:
        await redis_client.publish(f"channel:debates:{decision['instrument_id']}", json.dumps({
            "type": "DEBATE_REJECTED",
            "run_id": str(run_id)
        }))
        
    return {"status": "rejected", "run_id": str(run_id)}

@router.get("/debates/{run_id}/decision")
async def get_debate_decision(run_id: str, request: Request, _auth=Depends(require_auth)):
    pool = request.app.state.pool
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid run_id format")
        
    async with pool.acquire() as conn:
        decision = await conn.fetchrow("SELECT * FROM portfolio_decisions WHERE run_id = $1", run_uuid)
        if not decision:
            raise HTTPException(status_code=404, detail="No portfolio decision found for this debate run")
            
    # Convert asyncpg Record to JSON-safe dict (Decimal -> float)
    result = {k: (float(v) if hasattr(v, 'as_tuple') else str(v) if hasattr(v, 'hex') else v) for k, v in dict(decision).items()}
    similar = await similar_or_none(pool, decision["instrument_id"], decision["recommendation"], exclude_run_id=run_uuid)
    if similar is not None:
        result["similar_decisions"] = similar
    return result

@router.get("/debates")
async def list_debates(request: Request, limit: int = 50, _auth=Depends(require_auth)):
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT r.id as run_id, r.instrument_id, r.status, r.created_at, 
                   d.recommendation, d.conviction_level, d.conviction_score, d.allocation_pct, d.reasoning
            FROM debate_runs r
            LEFT JOIN portfolio_decisions d ON d.run_id = r.id
            ORDER BY r.created_at DESC
            LIMIT $1
        """, limit)
        
    result = []
    for row in rows:
        d = dict(row)
        decision_str = None
        level = d.get("conviction_level")
        if d.get("recommendation"):
            # B2: recommendation is the direction; the CRO's own wording (Overweight, ...) is shown next to it
            wording = f" · {level}" if level and level.strip().upper() != str(d["recommendation"]).upper() else ""
            decision_str = f"{d['recommendation']}{wording} ({(float(d['conviction_score'])*100) if d['conviction_score'] else 0}% Conviction) • {d['reasoning'] or ''}"
            
        result.append({
            "runId": str(d["run_id"]),
            "instrumentId": d["instrument_id"],
            "status": d["status"],
            "decision": decision_str,
            "convictionLevel": level,
            "timestamp": d["created_at"].timestamp() * 1000,
            "turns": [] # We omit turns in the list view for performance
        })
    return result
