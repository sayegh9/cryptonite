import datetime
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from typing import Literal

import httpx
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
from cryptonite_execution.mandate_store import resolve_mandate_for
from cryptonite_schemas.debate import verdict_direction
from cryptonite_schemas.mandate import ActionClass
from fastapi import APIRouter, Depends, HTTPException, Request
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
    allocation_pct: float | None = Field(default=None, ge=1.0, le=25.0)
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
        count = await redis_client.incr(day_key)
        await redis_client.expire(day_key, BRAIN_DEBATE_CAP_TTL_S)
        if count > BRAIN_DEBATE_DAILY_CAP:
            await redis_client.decr(day_key)
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


@router.post("/debates/{run_id}/approve")
async def approve_debate(
    run_id: str,
    request: Request,
    overrides: ApproveRequestOverrides | None = None,
    _op=Depends(require_human_operator),
    _csrf=Depends(require_csrf)
):
    pool = request.app.state.pool
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid run_id format")

    async with pool.acquire() as conn:
        run = await conn.fetchrow("SELECT status, decision_trace_id FROM debate_runs WHERE id = $1", run_uuid)
        if not run:
            raise HTTPException(status_code=404, detail="Debate run not found")
        if run['status'] != 'PENDING_APPROVAL':
            raise HTTPException(status_code=409, detail=f"Debate run has invalid status for approval: {run['status']}")

        decision = await conn.fetchrow("SELECT instrument_id, recommendation, conviction_score, allocation_pct FROM portfolio_decisions WHERE run_id = $1", run_uuid)
        if not decision:
            raise HTTPException(status_code=404, detail="No portfolio decision found for this debate run")

        account = await conn.fetchrow("SELECT total_equity FROM paper_accounts WHERE account_id = $1", PAPER_ACCOUNT_ID)

    side = order_side(decision['recommendation'])
    if side is None:
        raise HTTPException(status_code=409, detail=f"Recommendation '{decision['recommendation']}' has no order to execute; reject the debate instead")

    if not account:
        raise HTTPException(status_code=503, detail=f"Paper account {PAPER_ACCOUNT_ID} not found")
    equity = float(account['total_equity'])

    sym = decision['instrument_id'].split(".")[0].upper()
    redis_client = _get_redis(request)
    decision_price = await _fetch_decision_price(redis_client, sym)
    if not decision_price:
        raise HTTPException(status_code=503, detail="Could not fetch live price for sizing.")

    alloc_pct = float(decision['allocation_pct'])
    if overrides and overrides.allocation_pct is not None:
        alloc_pct = float(overrides.allocation_pct)

    quantity = (equity * alloc_pct / 100.0) / decision_price

    payload = {
        "instrument_id": decision['instrument_id'],
        "symbol": sym,
        "side": side,
        "quantity": float(round(quantity, 4)),
        "price": float(decision_price),
        "order_type": overrides.order_type if overrides and overrides.order_type else "LIMIT",
        "recommendation": decision['recommendation'],
        "conviction_score": float(decision['conviction_score']),
        "allocation_pct": alloc_pct,
        "account_id": PAPER_ACCOUNT_ID,
    }
    if run['decision_trace_id']:  # R7.4: candidate -> debate -> approval -> command -> fill
        payload["decision_trace_id"] = str(run['decision_trace_id'])

    if overrides:
        if overrides.limit_offset_pct is not None:
            payload["limit_offset_pct"] = overrides.limit_offset_pct
        if overrides.stop_loss_pct is not None:
            payload["stop_loss_pct"] = overrides.stop_loss_pct

    # Every AI order trades inside a mandate (A5.1): the one the debate named, else the single ACTIVE
    # mandate that allows the instrument. The approval is recorded and bound to its order (A5.3).
    async with pool.acquire() as conn:
        mandate_ref = await conn.fetchval("SELECT mandate_id FROM debate_runs WHERE id = $1", run_uuid)
        mandate, why = await resolve_mandate_for(conn, decision['instrument_id'], mandate_ref)
    if mandate is None:
        raise HTTPException(status_code=409, detail=f"{why}: approve this debate inside an ACTIVE mandate that allows "
                            f"{decision['instrument_id']} (Mission Control → Mandates)")
    payload["mandate_id"] = str(mandate.id)
    if side == "SELL":
        # spot is long-only: a debate SELL sells the mandate's own holding, never a budget-sized amount
        async with pool.acquire() as conn:
            held = await conn.fetchval(
                """SELECT COALESCE(SUM(CASE WHEN side = 'BUY' THEN filled_qty ELSE -filled_qty END), 0) FROM paper_orders
                   WHERE instance_id = $1 AND instrument_id = $2 AND filled_qty > 0""",
                mandate.instance_id, decision['instrument_id'])
        if not held or float(held) <= 0:
            raise HTTPException(status_code=409, detail="The mandate holds nothing to sell; reject the debate instead")
        payload.update(quantity=float(held), order_type="MARKET")
        payload.pop("price", None)

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
                summary=f"Debate verdict: {side} {decision['instrument_id']} at {alloc_pct}% of equity",
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
    return {"status": "approved", "run_id": str(run_id), **payload}

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
