"""FastAPI router for Paper Trading projections and command submission."""

import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from cryptonite_api.kill_switch import PAPER_ACCOUNT_ID, PAPER_ENVIRONMENT, read_kill_switch
from cryptonite_api.leverage_card import manual_ticket_preview
from cryptonite_api.routes.auth import require_csrf, require_operator
from cryptonite_api.universe import require_fed
from cryptonite_schemas.mandate import MANDATE_INSTANCE_PREFIX
from cryptonite_schemas.marks import read_marks, resolve_mark, unrealized_pnl
from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
    # Perpetual-only; the dispatcher rejects them on spot and defaults leverage 1 / ISOLATED. 10x account maximum
    # (ADR 0009 D2; was 20)
    leverage: int | None = Field(None, ge=1, le=10)
    margin_mode: Literal["ISOLATED", "CROSS"] | None = None
    reduce_only: bool = False
    # A perp entry's stop (leverage plan 4.5): the dispatcher checks it (liquidation buffer, loss within 2% of
    # equity) and places a resting reduce-only STOP_MARKET on the mark once the entry fills
    stop_loss: float | None = Field(None, gt=0)


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


def _resolve_strategy_name(instance_id: str | None, joined_name: str | None) -> str:
    if joined_name and joined_name not in ("ManualTrading", "AIDebateTrading"):
        return joined_name
    inst = (instance_id or "").upper()
    if "MANUAL" in inst or joined_name == "ManualTrading":
        return "Manual Trade"
    if "DEBATE" in inst or "AI" in inst or "MANDATE" in inst or joined_name == "AIDebateTrading":
        return "AI Debate Worker"
    return joined_name or instance_id or "Manual Trade"


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
            """
            SELECT p.*, s.strategy_name
            FROM paper_positions p
            LEFT JOIN paper_strategy_instances s ON p.instance_id = s.instance_id
            WHERE p.account_id = $1
            ORDER BY p.opened_at DESC;
            """,
            account_id,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT p.*, s.strategy_name
            FROM paper_positions p
            LEFT JOIN paper_strategy_instances s ON p.instance_id = s.instance_id
            ORDER BY p.opened_at DESC;
            """
        )

    # Dynamic exit rules from system_settings or default exit thresholds (stop loss 2.5%, trail activate / take profit 4.0%)
    stop_loss_pct = 2.5
    take_profit_pct = 4.0
    if pool:
        try:
            settings_rows = await pool.fetch(
                "SELECT key, value FROM system_settings WHERE key IN ('exit.stop_loss_pct', 'exit.trail_activate_pct', 'exit.take_profit_pct');"
            )
            for sr in settings_rows:
                k = sr["key"]
                v = sr["value"]
                if isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except Exception:
                        pass
                try:
                    val = float(v)
                    if k == "exit.stop_loss_pct":
                        stop_loss_pct = val
                    elif k in ("exit.trail_activate_pct", "exit.take_profit_pct"):
                        take_profit_pct = val
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass

    now_dt = datetime.now(UTC)
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

        # Strategy name resolution
        row["strategy_name"] = _resolve_strategy_name(row.get("instance_id"), row.get("strategy_name"))

        # opened_at formatted and duration_s calculation
        opened_at = row.get("opened_at")
        opened_at_dt = None
        if isinstance(opened_at, datetime):
            opened_at_dt = opened_at if opened_at.tzinfo else opened_at.replace(tzinfo=UTC)
            row["opened_at"] = opened_at_dt.isoformat()
        elif isinstance(opened_at, str):
            row["opened_at"] = opened_at
            try:
                opened_at_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                if opened_at_dt.tzinfo is None:
                    opened_at_dt = opened_at_dt.replace(tzinfo=UTC)
            except Exception:
                opened_at_dt = None
        else:
            row["opened_at"] = now_dt.isoformat()

        if opened_at_dt:
            row["duration_s"] = round(max(0.0, (now_dt - opened_at_dt).total_seconds()), 2)
        else:
            row["duration_s"] = 0.0

        # Dynamic stop loss and take profit calculation
        entry_price = float(row["entry_price"]) if row.get("entry_price") is not None else 0.0
        side = str(row.get("side") or "").upper()
        if entry_price > 0:
            if side == "SHORT":
                sl_price = entry_price * (1.0 + stop_loss_pct / 100.0)
                tp_price = entry_price * (1.0 - take_profit_pct / 100.0)
            else:
                sl_price = entry_price * (1.0 - stop_loss_pct / 100.0)
                tp_price = entry_price * (1.0 + take_profit_pct / 100.0)
            row["stop_loss_price"] = round(sl_price, 8)
            row["take_profit_price"] = round(tp_price, 8)
        else:
            row["stop_loss_price"] = None
            row["take_profit_price"] = None

        out.append(row)
    return out


@router.get("/orders")
async def get_orders(request: Request, account_id: str | None = None, limit: int = 100, auth: dict = Depends(require_operator)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    if account_id:
        rows = await pool.fetch(
            """
            SELECT o.*, s.strategy_name
            FROM paper_orders o
            LEFT JOIN paper_strategy_instances s ON o.instance_id = s.instance_id
            WHERE o.account_id = $1
            ORDER BY o.created_at DESC
            LIMIT $2;
            """,
            account_id,
            limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT o.*, s.strategy_name
            FROM paper_orders o
            LEFT JOIN paper_strategy_instances s ON o.instance_id = s.instance_id
            ORDER BY o.created_at DESC
            LIMIT $1;
            """,
            limit,
        )
    out = []
    for r in rows:
        row = dict(r)
        row["strategy_name"] = _resolve_strategy_name(row.get("instance_id"), row.get("strategy_name"))
        out.append(row)
    return out


@router.get("/trades")
async def get_trades(request: Request, account_id: str | None = None, limit: int = 100, auth: dict = Depends(require_operator)) -> list[dict[str, Any]]:
    pool = request.app.state.pool
    if account_id:
        rows = await pool.fetch(
            """
            SELECT t.*, s.strategy_name, o.price AS order_price
            FROM paper_trades t
            LEFT JOIN paper_strategy_instances s ON t.instance_id = s.instance_id
            LEFT JOIN paper_orders o ON t.client_order_id = o.client_order_id
            WHERE t.account_id = $1
            ORDER BY t.executed_at DESC
            LIMIT $2;
            """,
            account_id,
            limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT t.*, s.strategy_name, o.price AS order_price
            FROM paper_trades t
            LEFT JOIN paper_strategy_instances s ON t.instance_id = s.instance_id
            LEFT JOIN paper_orders o ON t.client_order_id = o.client_order_id
            ORDER BY t.executed_at DESC
            LIMIT $1;
            """,
            limit,
        )
    out = []
    for r in rows:
        row = dict(r)
        row["strategy_name"] = _resolve_strategy_name(row.get("instance_id"), row.get("strategy_name"))

        order_price = row.pop("order_price", None)
        slippage_bps = None
        if order_price is not None:
            try:
                op_val = float(order_price)
                tp_val = float(row.get("price") or 0.0)
                if op_val > 0 and tp_val > 0:
                    diff = ((tp_val - op_val) / op_val) * 10000.0
                    side = str(row.get("side") or "").upper()
                    slippage_bps = round(diff if side == "BUY" else -diff, 2)
            except (ValueError, TypeError, ZeroDivisionError):
                slippage_bps = None
        row["slippage_bps"] = slippage_bps
        out.append(row)
    return out


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


@router.get("/perp/plan")
async def perp_ticket_plan(request: Request, instrument_id: str, side: Literal["BUY", "SELL"],
                           quantity: float = Query(..., gt=0), leverage: int = Query(..., ge=1, le=10),
                           price: float | None = None, stop_loss: float | None = None,
                           auth: dict = Depends(require_operator)) -> dict[str, Any]:
    """The manual perp ticket's preview (leverage plan 4.5): which leverage pills the dispatcher would accept for this
    quantity, and the margin, liquidation price, buffer and losses at the chosen one. Read only; the dispatcher
    checks the order again when it is sent."""
    if not instrument_id.split(".")[0].upper().endswith("-PERP"):
        raise HTTPException(status_code=422, detail="NOT_A_PERP: the preview is for perp instruments")
    if (price is not None and price <= 0) or (stop_loss is not None and stop_loss <= 0):
        raise HTTPException(status_code=422, detail="INVALID_ORDER: quantity, price and stop must be positive")
    return await manual_ticket_preview(
        request.app.state.pool, getattr(getattr(request.app.state, "stream_hub", None), "redis", None), instrument_id, side,
        Decimal(str(quantity)), leverage, price=Decimal(str(price)) if price else None,
        stop_loss=Decimal(str(stop_loss)) if stop_loss else None)


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
    if req.stop_loss is not None:
        if not req.instrument_id.split(".")[0].upper().endswith("-PERP") or req.reduce_only:
            raise HTTPException(status_code=422, detail="STOP_ONLY_ON_PERP_ENTRY: an attached stop is for a perp entry")
        payload["stop_loss"] = str(req.stop_loss)
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
