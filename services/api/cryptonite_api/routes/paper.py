"""FastAPI router for Paper Trading projections and command submission."""

import json
import logging
import uuid
from typing import Any

from cryptonite_api.routes.auth import require_operator, require_csrf
from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, Field

logger = logging.getLogger("cryptonite.api.paper")

router = APIRouter(prefix="/api/v1/paper", tags=["Paper Trading"])


# Pydantic request models
class DeployStrategyRequest(BaseModel):
    strategy_name: str
    strategy_version_id: str
    instrument_id: str
    timeframe: str
    allocation_usd: float
    parameters: dict[str, Any] = Field(default_factory=dict)
    account_id: str = "PAPER-ACCOUNT-001"


class TripKillSwitchRequest(BaseModel):
    reason: str = "Operator manual kill switch engagement"
    operator: str = "web-ui"


class ResetKillSwitchRequest(BaseModel):
    operator: str = "web-ui"


class ClosePositionRequest(BaseModel):
    instrument_id: str
    price: float | None = None


class ManualOrderRequest(BaseModel):
    instrument_id: str
    side: str
    order_type: str = "MARKET"
    quantity: float = Field(..., gt=0)
    price: float | None = None  # Required if LIMIT


# Helper to insert into execution_commands
async def _enqueue_command(
    pool: Any,
    command_type: str,
    target_instance_id: str | None,
    payload: dict[str, Any],
    requester_id: str = "web-ui",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    cmd_id = uuid.uuid4()
    idem_key = idempotency_key or f"{command_type}:{cmd_id}"

    # Anti-Corruption: Strip kill-switch bypass flags from AI-originated orders
    if requester_id.startswith("agent:") or requester_id.startswith("ai:"):
        payload.pop("reduce_only", None)
        payload.pop("is_kill_switch_exit", None)

    query = """
    INSERT INTO execution_commands (
        command_id, idempotency_key, command_type, target_instance_id,
        requester_id, payload, status, attempt_count, created_at
    ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'PENDING', 0, now())
    RETURNING command_id, command_type, status, created_at;
    """
    row = await pool.fetchrow(
        query,
        cmd_id,
        idem_key,
        command_type,
        target_instance_id,
        requester_id,
        json.dumps(payload),
    )
    return dict(row)


# --- READ ENDPOINTS (Projections) ---


@router.get("/accounts")
async def get_accounts(request: Request, auth: dict = Depends(require_operator)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    rows = await pool.fetch("SELECT * FROM paper_accounts ORDER BY account_id;")
    return [dict(r) for r in rows]


@router.get("/positions")
async def get_positions(request: Request, account_id: str | None = None, auth: dict = Depends(require_operator)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    if account_id:
        rows = await pool.fetch(
            "SELECT * FROM paper_positions WHERE account_id = $1 ORDER BY opened_at DESC;",
            account_id,
        )
    else:
        rows = await pool.fetch("SELECT * FROM paper_positions ORDER BY opened_at DESC;")
    return [dict(r) for r in rows]


@router.get("/orders")
async def get_orders(request: Request, account_id: str | None = None, limit: int = 100, auth: dict = Depends(require_operator)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    if account_id:
        rows = await pool.fetch(
            "SELECT * FROM paper_orders WHERE account_id = $1 ORDER BY created_at DESC LIMIT $2;",
            account_id,
            limit,
        )
    else:
        rows = await pool.fetch("SELECT * FROM paper_orders ORDER BY created_at DESC LIMIT $1;", limit)
    return [dict(r) for r in rows]


@router.get("/trades")
async def get_trades(request: Request, account_id: str | None = None, limit: int = 100, auth: dict = Depends(require_operator)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    if account_id:
        rows = await pool.fetch(
            "SELECT * FROM paper_trades WHERE account_id = $1 ORDER BY executed_at DESC LIMIT $2;",
            account_id,
            limit,
        )
    else:
        rows = await pool.fetch("SELECT * FROM paper_trades ORDER BY executed_at DESC LIMIT $1;", limit)
    return [dict(r) for r in rows]


@router.get("/strategies")
async def get_strategies(request: Request, account_id: str | None = None, auth: dict = Depends(require_operator)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    if account_id:
        rows = await pool.fetch(
            "SELECT * FROM paper_strategy_instances WHERE account_id = $1 ORDER BY created_at DESC;",
            account_id,
        )
    else:
        rows = await pool.fetch("SELECT * FROM paper_strategy_instances ORDER BY created_at DESC;")
    return [dict(r) for r in rows]


@router.get("/risk-events")
async def get_risk_events(request: Request, limit: int = 50, auth: dict = Depends(require_operator)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    rows = await pool.fetch("SELECT * FROM paper_risk_events ORDER BY occurred_at DESC LIMIT $1;", limit)
    return [dict(r) for r in rows]


@router.get("/kill-switch")
async def get_kill_switch(request: Request, auth: dict = Depends(require_operator)) -> dict[str, Any]:
    pool = request.app.state.pool
    row = await pool.fetchrow("SELECT * FROM execution_kill_switch WHERE id = 1;")
    if not row:
        return {"id": 1, "is_latched": False}
    return dict(row)


@router.get("/commands")
async def get_commands(request: Request, limit: int = 50, auth: dict = Depends(require_operator)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    rows = await pool.fetch("SELECT * FROM execution_commands ORDER BY created_at DESC LIMIT $1;", limit)
    return [dict(r) for r in rows]


# --- COMMAND ENDPOINTS (Writes to execution_commands ONLY) ---


@router.post("/strategies/deploy")
async def deploy_strategy(req: DeployStrategyRequest, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)) -> dict[str, Any]:
    pool = request.app.state.pool
    instance_id = f"INST-{uuid.uuid4().hex[:8].upper()}"
    payload = {
        "instance_id": instance_id,
        "account_id": req.account_id,
        "strategy_name": req.strategy_name,
        "strategy_version_id": req.strategy_version_id,
        "instrument_id": req.instrument_id,
        "timeframe": req.timeframe,
        "allocation_usd": req.allocation_usd,
        "parameters": req.parameters,
    }
    return await _enqueue_command(
        pool=pool,
        command_type="DEPLOY",
        target_instance_id=instance_id,
        payload=payload,
    )


@router.post("/strategies/{instance_id}/pause")
async def pause_strategy(instance_id: str, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)) -> dict[str, Any]:
    pool = request.app.state.pool
    return await _enqueue_command(
        pool=pool,
        command_type="PAUSE",
        target_instance_id=instance_id,
        payload={"instance_id": instance_id},
    )


@router.post("/strategies/{instance_id}/resume")
async def resume_strategy(instance_id: str, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)) -> dict[str, Any]:
    pool = request.app.state.pool
    return await _enqueue_command(
        pool=pool,
        command_type="RESUME",
        target_instance_id=instance_id,
        payload={"instance_id": instance_id},
    )


@router.post("/strategies/{instance_id}/stop")
async def stop_strategy(instance_id: str, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)) -> dict[str, Any]:
    pool = request.app.state.pool
    return await _enqueue_command(
        pool=pool,
        command_type="STOP",
        target_instance_id=instance_id,
        payload={"instance_id": instance_id},
    )


@router.post("/strategies/{instance_id}/close-position")
async def close_position(instance_id: str, req: ClosePositionRequest, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)) -> dict[str, Any]:
    pool = request.app.state.pool
    return await _enqueue_command(
        pool=pool,
        command_type="CLOSE_POSITION",
        target_instance_id=instance_id,
        payload={"instance_id": instance_id, "instrument_id": req.instrument_id, "price": req.price},
    )


@router.post("/orders/{client_order_id}/cancel")
async def cancel_order(client_order_id: str, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)) -> dict[str, Any]:
    pool = request.app.state.pool
    return await _enqueue_command(
        pool=pool,
        command_type="CANCEL_ORDER",
        target_instance_id=None,
        payload={"client_order_id": client_order_id},
    )


@router.post("/kill-switch")
async def trip_kill_switch(req: TripKillSwitchRequest, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)) -> dict[str, Any]:
    pool = request.app.state.pool
    return await _enqueue_command(
        pool=pool,
        command_type="TRIP_KILL_SWITCH",
        target_instance_id=None,
        payload={"reason": req.reason, "operator": req.operator},
        requester_id=req.operator,
    )


@router.post("/kill-switch/reset")
async def reset_kill_switch(req: ResetKillSwitchRequest, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)) -> dict[str, Any]:
    pool = request.app.state.pool
    return await _enqueue_command(
        pool=pool,
        command_type="RESET_SWITCH",
        target_instance_id=None,
        payload={"operator": req.operator},
        requester_id=req.operator,
    )


@router.post("/orders")
async def submit_manual_order(req: ManualOrderRequest, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)) -> dict[str, Any]:
    client_order_id = f"MANUAL-{uuid.uuid4().hex[:8].upper()}"
    payload = {
        "instrument_id": req.instrument_id,
        "symbol": req.instrument_id.replace(".BINANCE", ""),
        "side": req.side,
        "order_type": req.order_type,
        "quantity": str(req.quantity),
        "price": str(req.price) if req.price else None,
        "client_order_id": client_order_id,
    }
    return await _enqueue_command(
        pool=request.app.state.pool,
        command_type="MANUAL_ORDER",
        target_instance_id="MANUAL_TRADE",
        payload=payload,
    )
