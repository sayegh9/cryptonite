"""FastAPI router for Paper Trading projections and command submission."""

import json
import logging
import uuid
from typing import Any, Literal

from cryptonite_api.kill_switch import PAPER_ACCOUNT_ID, PAPER_ENVIRONMENT, read_kill_switch
from cryptonite_api.routes.auth import require_csrf, require_operator
from cryptonite_api.universe import require_fed
from cryptonite_schemas.mandate import MANDATE_INSTANCE_PREFIX
from cryptonite_schemas.marks import read_marks, resolve_mark, unrealized_pnl
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("cryptonite.api.paper")

router = APIRouter(prefix="/api/v1/paper", tags=["Paper Trading"])

MANDATE_INSTANCE_REFUSAL = (
    "MANDATE_INSTANCE: AI mandates are paused and resumed in Mission Control, not as strategy instances"
)


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
    # Perpetual-only; the dispatcher rejects them on spot and defaults leverage 1 / CROSS
    leverage: int | None = Field(None, ge=1, le=20)
    margin_mode: Literal["ISOLATED", "CROSS"] | None = None
    reduce_only: bool = False


# Helper to insert into execution_commands
async def _enqueue_command(
    pool: Any,
    command_type: str,
    target_instance_id: str | None,
    payload: dict[str, Any],
    requester_id: str = "web-ui",
    idempotency_key: str | None = None,
    environment: str = "PAPER",
    account_id: str = "PAPER-ACCOUNT-001",
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
        requester_id, payload, status, attempt_count, environment, account_id, created_at
    ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'PENDING', 0, $7, $8, now())
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
        environment,
        account_id,
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
    # B3 (F-H3): an open position shows its live mark and PnL, else its stored mark flagged stale
    marks = await read_marks(getattr(getattr(request.app.state, "stream_hub", None), "redis", None))
    out = []
    for r in rows:
        row = dict(r)
        if row.get("status") == "OPEN":
            view = resolve_mark(row["instrument_id"], row["current_price"], marks)
            if view.source == "live":
                row["current_price"] = view.price
                row["unrealized_pnl"] = unrealized_pnl(row["side"], row["quantity"], row["entry_price"], view.price)
            row.update(view.fields())
        out.append(row)
    return out


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
    # The paper engine's durable latch (written by the orchestrator), read the same way as the AI status
    row = await read_kill_switch(request.app.state.pool, PAPER_ENVIRONMENT, PAPER_ACCOUNT_ID)
    if row is None:
        raise HTTPException(status_code=503, detail="PAPER kill-switch row is missing: its state is unknown")
    return row


@router.get("/commands")
async def get_commands(request: Request, limit: int = 50, auth: dict = Depends(require_operator)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    rows = await pool.fetch("SELECT * FROM execution_commands ORDER BY created_at DESC LIMIT $1;", limit)
    return [dict(r) for r in rows]


# --- COMMAND ENDPOINTS (Writes to execution_commands ONLY) ---


def _refuse_mandate_instance(instance_id: str) -> None:
    """An AI mandate is paused, resumed and stopped in Mission Control. As a strategy instance it would reach the
    orchestrator, where a RESUME raised a duplicate-key error on the mandate and blocked the queue."""
    if instance_id.startswith(MANDATE_INSTANCE_PREFIX):
        raise HTTPException(status_code=409, detail=MANDATE_INSTANCE_REFUSAL)


@router.post("/strategies/deploy")
async def deploy_strategy(req: DeployStrategyRequest, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)) -> dict[str, Any]:
    pool = request.app.state.pool
    # D4: an instance on a symbol or timeframe ingest does not feed would sit RUNNING on no bars
    await require_fed(request, [req.instrument_id], req.timeframe)
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
    _refuse_mandate_instance(instance_id)
    pool = request.app.state.pool
    return await _enqueue_command(
        pool=pool,
        command_type="PAUSE",
        target_instance_id=instance_id,
        payload={"instance_id": instance_id},
    )


@router.post("/strategies/{instance_id}/resume")
async def resume_strategy(instance_id: str, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)) -> dict[str, Any]:
    _refuse_mandate_instance(instance_id)
    pool = request.app.state.pool
    return await _enqueue_command(
        pool=pool,
        command_type="RESUME",
        target_instance_id=instance_id,
        payload={"instance_id": instance_id},
    )


@router.post("/strategies/{instance_id}/stop")
async def stop_strategy(instance_id: str, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)) -> dict[str, Any]:
    _refuse_mandate_instance(instance_id)
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
        environment="PAPER",  # claimed by the paper orchestrator, which latches the engine
        account_id="PAPER-ACCOUNT-001",
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
        environment="PAPER",
        account_id="PAPER-ACCOUNT-001",
    )


@router.post("/orders")
async def submit_manual_order(req: ManualOrderRequest, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)) -> dict[str, Any]:
    client_order_id = f"MANUAL-{uuid.uuid4().hex[:8].upper()}"
    payload = {
        "account_id": "PAPER-ACCOUNT-001",
        "instrument_id": req.instrument_id,
        "symbol": req.instrument_id.replace(".BINANCE", ""),
        "side": req.side,
        "order_type": req.order_type,
        "quantity": str(req.quantity),
        "price": str(req.price) if req.price else None,
        "client_order_id": client_order_id,
        "leverage": req.leverage,
        "margin_mode": req.margin_mode,
        "reduce_only": req.reduce_only,
    }
    # A machine token (agent/research workers) is not the operator's hand: its orders are AI orders,
    # so the orchestrator holds them to a mandate (A5.1) instead of treating them as manual trades.
    machine = auth.get("type") == "machine_token"
    return await _enqueue_command(
        pool=request.app.state.pool,
        command_type="MANUAL_ORDER",
        target_instance_id="MANUAL_TRADE",
        payload=payload,
        requester_id="ai:machine-token" if machine else "web-ui",
        environment="PAPER",
        account_id="PAPER-ACCOUNT-001",
    )
