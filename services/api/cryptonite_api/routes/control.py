"""Privileged operator control plane API routes.

Provides audited endpoints for:
1. Emergency kill-switch tripping.
2. Step-up authenticated kill-switch resetting.
3. The risk limits the paper orchestrator enforces, and the operator's tighten-only overrides (F7):
   tightening is direct, loosening needs the operator's TOTP, nothing goes past the code defaults.
4. Strict CSRF header verification on all mutating requests.
"""

import json
import time
import uuid
from decimal import Decimal

from cryptonite_api.auth import OperatorAuthManager
from cryptonite_api.routes.ai import require_totp
from cryptonite_api.routes.auth import get_auth_manager, require_operator
from cryptonite_execution.risk_limits import (
    DEFAULTS,
    LIMIT_NAMES,
    MIN_ORDER_EQUITY_PCT,
    NOT_CONFIGURABLE,
    RiskLimitValues,
    effective,
    validate_change,
)
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1/control", tags=["control"])

# Published by the orchestrator from its GuardedOrderDispatcher (ops_monitor, R1.8); expires if it stops
EFFECTIVE_LIMITS_KEY = "risk:effective_limits"
RISK_LIMITS_APPLY_S = 60  # the orchestrator re-reads overrides every minute (ops_monitor)


class TripKillSwitchRequest(BaseModel):
    reason: str = Field(..., min_length=5, description="Audited operational reason for tripping kill switch")
    environment: str = Field(default="DEMO", description="Target environment")
    account_id: str = Field(default="BINANCE-SPOT-DEMO", description="Target account ID")


class ResetKillSwitchRequest(BaseModel):
    step_up_password: str = Field(..., min_length=1, description="Operator password for step-up authentication")
    reason: str = Field(..., min_length=5, description="Audited justification for clearing the kill switch")
    environment: str = Field(default="DEMO", description="Target environment")
    account_id: str = Field(default="BINANCE-SPOT-DEMO", description="Target account ID")


class UpdateRiskLimitsRequest(BaseModel):
    """The full desired override set, in percent; null = no override (the code default applies)."""

    environment: str = Field(default="PAPER", pattern=r"^(PAPER|DEMO|LIVE)$")
    account_id: str = Field(default="PAPER-ACCOUNT-001", min_length=1, max_length=64)
    max_daily_loss_pct: Decimal | None = Field(default=None, max_digits=8, decimal_places=4)
    max_order_equity_pct: Decimal | None = Field(default=None, max_digits=8, decimal_places=4)
    cross_mandate_exposure_pct: Decimal | None = Field(default=None, max_digits=8, decimal_places=4)
    reason: str = Field(..., min_length=5, max_length=300, description="Audited reason for the change")
    totp_code: str | None = Field(default=None, description="Operator TOTP; required only to loosen a limit")


def verify_csrf(session: dict, x_csrf_token: str | None) -> None:
    """Enforce CSRF token match between header and authenticated session."""
    if not x_csrf_token or x_csrf_token != session.get("csrf_token"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Invalid or missing CSRF token",
        )


@router.get("/kill-switch/status")
async def get_kill_switch_status(
    request: Request,
    environment: str = "DEMO",
    account_id: str = "DEMO-ACCOUNT-001",
    auth: dict = Depends(require_operator),
):
    """Retrieve the current state and latch generation of the execution kill switch."""
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT is_latched, latch_generation, updated_at
            FROM execution_kill_switch
            WHERE environment = $1 AND account_id = $2;
            """,
            environment,
            account_id,
        )
        if not row:
            # If no row exists, assume armed (unlatched) but log a warning? 
            # Migration 007 backfilled this row.
            return {
                "environment": environment,
                "account_id": account_id,
                "is_latched": False,
                "latch_generation": 0,
                "updated_at": None,
            }
        return {
            "environment": environment,
            "account_id": account_id,
            "is_latched": row["is_latched"],
            "latch_generation": row["latch_generation"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

@router.post("/kill-switch/trip")
async def trip_kill_switch(
    req: TripKillSwitchRequest,
    request: Request,
    auth: dict = Depends(require_operator),
    auth_mgr: OperatorAuthManager = Depends(get_auth_manager),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    """Manually trip the emergency kill switch with full audit logging via SECURITY DEFINER function."""
    verify_csrf(auth, x_csrf_token)
    username = auth["username"]
    client_ip = request.client.host if request.client else None

    # Latch kill switch in PostgreSQL via SECURITY DEFINER function
    async with request.app.state.pool.acquire() as conn:
        latch_gen = await conn.fetchval(
            """
            SELECT trip_execution_kill_switch($1, $2, $3, $4);
            """,
            req.environment,
            req.account_id,
            req.reason,
            f"OPERATOR:{username}",
        )

    # Audit log
    await auth_mgr.log_audit_event(
        username=username,
        action="KILL_SWITCH_TRIP",
        ip_address=client_ip,
        details={"reason": req.reason, "environment": req.environment, "account_id": req.account_id, "latch_generation": latch_gen},
    )

    return {
        "success": True,
        "kill_switch_latched": True,
        "tripped_by": username,
        "latch_generation": latch_gen,
    }


@router.post("/kill-switch/reset")
async def reset_kill_switch(
    req: ResetKillSwitchRequest,
    request: Request,
    auth: dict = Depends(require_operator),
    auth_mgr: OperatorAuthManager = Depends(get_auth_manager),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    """Request reset of the emergency kill switch requiring step-up password authentication.
    
    Enqueues an authenticated KILL_SWITCH_RESET_REQUEST execution command for the executor
    to evaluate against its 7-point safety checklist and execute CAS unlatching.
    """
    verify_csrf(auth, x_csrf_token)
    username = auth["username"]
    client_ip = request.client.host if request.client else None

    # 1. Step-up password authentication
    is_valid_pw = await auth_mgr.verify_operator_step_up(username, req.step_up_password)
    if not is_valid_pw:
        await auth_mgr.log_audit_event(
            username=username,
            action="KILL_SWITCH_RESET_FAILED_AUTH",
            ip_address=client_ip,
            details={"reason": "Incorrect step-up password attempt"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Step-up authentication failed: Invalid password",
        )

    # 2. Audit log reset request
    audit_event_id = await auth_mgr.log_audit_event(
        username=username,
        action="KILL_SWITCH_RESET_REQUESTED",
        ip_address=client_ip,
        details={"reason": req.reason, "environment": req.environment, "account_id": req.account_id},
    )

    # 3. Check current latch generation and enqueue KILL_SWITCH_RESET_REQUEST
    async with request.app.state.pool.acquire() as conn:
        ks_row = await conn.fetchrow(
            """
            SELECT is_latched, latch_generation
            FROM execution_kill_switch
            WHERE environment = $1 AND account_id = $2;
            """,
            req.environment,
            req.account_id,
        )
        if not ks_row or not ks_row["is_latched"]:
            return {
                "success": True,
                "status": "ALREADY_UNLATCHED",
                "kill_switch_latched": False,
                "message": "Kill switch is not currently latched",
            }
        expected_gen = ks_row["latch_generation"]

        cmd_id = uuid.uuid4()
        idempotency_key = f"reset-ks-{expected_gen}-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"
        now_ns = time.time_ns()
        payload = {
            "actor_id": f"OPERATOR:{username}",
            "reason": req.reason,
            "auth_audit_event_id": str(audit_event_id),
            "expected_generation": expected_gen,
            "request_timestamp_ns": now_ns,
            "expires_at_ns": now_ns + (60 * 1_000_000_000),  # 60s validity
            "environment": req.environment,
            "account_id": req.account_id,
        }
        await conn.execute(
            """
            INSERT INTO execution_commands (
                command_id, idempotency_key, command_type, requester_id,
                target_instance_id, status, payload, created_at, attempt_id, attempt_count,
                environment, account_id
            ) VALUES (
                $1, $2, 'KILL_SWITCH_RESET_REQUEST', $3, 'cryptonite-executor',
                'PENDING', $4::jsonb, now(), gen_random_uuid(), 0, $5, $6
            );
            """,
            cmd_id,
            idempotency_key,
            f"OPERATOR:{username}",
            json.dumps(payload),
            req.environment,  # DEMO -> executor (as before); PAPER -> paper orchestrator
            req.account_id,
        )

    return {
        "success": True,
        "status": "REQUESTED",
        "command_id": str(cmd_id),
        "expected_generation": expected_gen,
        "requested_by": username,
        "kill_switch_latched": True,
    }


def _risk_limits_view(env: str, account_id: str, row) -> dict:
    overrides = RiskLimitValues.from_row(row)
    return {
        "environment": env,
        "account_id": account_id,
        "defaults": DEFAULTS.as_dict(),
        "overrides": overrides.as_dict(),
        "effective": effective(overrides).as_dict(),
        "min_order_equity_pct": format(MIN_ORDER_EQUITY_PCT, "f"),
        "not_configurable": {k: format(v, "f") for k, v in NOT_CONFIGURABLE.items()},
        "updated_by": row["updated_by"] if row else None,
        "updated_at": row["updated_at"].isoformat() if row and row["updated_at"] else None,
        "reason": row["reason"] if row else None,
    }


_OVERRIDE_SQL = """SELECT max_daily_loss_pct, max_order_equity_pct, cross_mandate_exposure_pct, updated_by, updated_at, reason
                   FROM risk_limit_overrides WHERE environment = $1 AND account_id = $2"""


@router.get("/risk-limits")
async def get_risk_limits(request: Request, environment: str = "PAPER", account_id: str = "PAPER-ACCOUNT-001",
                          auth: dict = Depends(require_operator)):
    """Stored overrides, the code defaults (= ceilings) and the limits that apply (F7)."""
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(_OVERRIDE_SQL, environment, account_id)
    return _risk_limits_view(environment, account_id, row)


@router.post("/risk-limits")
async def update_risk_limits(
    req: UpdateRiskLimitsRequest,
    request: Request,
    auth: dict = Depends(require_operator),
    auth_mgr: OperatorAuthManager = Depends(get_auth_manager),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    """Store the operator's override set (F7). Rules (``cryptonite_execution.risk_limits``): no value above
    its code default; tightening needs no TOTP; loosening (a higher value, or removing an override) needs
    the operator's TOTP. Refusals answer 422 with one error per field and change nothing."""
    verify_csrf(auth, x_csrf_token)
    username = auth.get("username") or f"machine:{auth.get('role', 'operator')}"
    client_ip = request.client.host if request.client else None
    requested = RiskLimitValues(**{name: getattr(req, name) for name in LIMIT_NAMES})

    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            # Serialise concurrent edits of one account: the TOTP decision is made against the stored row
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('risk_limit_overrides:' || $1 || ':' || $2))",
                               req.environment, req.account_id)
            row = await conn.fetchrow(_OVERRIDE_SQL + " FOR UPDATE", req.environment, req.account_id)
            current = RiskLimitValues.from_row(row)
            check = validate_change(current, requested)
            if not check.ok:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=[{"loc": ["body", name], "msg": f"{name}: {msg}", "type": "risk_limit"}
                            for name, msg in check.errors.items()],
                )
            if check.needs_totp:
                try:
                    await require_totp(request, auth, req.totp_code)
                except HTTPException as e:
                    await auth_mgr.log_audit_event(
                        username=username, action="RISK_LIMITS_UPDATE_FAILED_AUTH", ip_address=client_ip,
                        details={"environment": req.environment, "account_id": req.account_id,
                                 "loosened": list(check.loosened), "status": e.status_code})
                    if e.status_code == status.HTTP_401_UNAUTHORIZED:
                        raise HTTPException(status_code=401, detail=(
                            "TOTP_REQUIRED: loosening " + ", ".join(check.loosened) + " needs your current TOTP code"))
                    raise
            saved = await conn.fetchrow(
                """INSERT INTO risk_limit_overrides (environment, account_id, max_daily_loss_pct, max_order_equity_pct,
                       cross_mandate_exposure_pct, updated_by, updated_at, reason)
                   VALUES ($1, $2, $3, $4, $5, $6, now(), $7)
                   ON CONFLICT (environment, account_id) DO UPDATE SET
                       max_daily_loss_pct = EXCLUDED.max_daily_loss_pct,
                       max_order_equity_pct = EXCLUDED.max_order_equity_pct,
                       cross_mandate_exposure_pct = EXCLUDED.cross_mandate_exposure_pct,
                       updated_by = EXCLUDED.updated_by, updated_at = now(), reason = EXCLUDED.reason
                   RETURNING max_daily_loss_pct, max_order_equity_pct, cross_mandate_exposure_pct, updated_by, updated_at, reason""",
                req.environment, req.account_id, requested.max_daily_loss_pct, requested.max_order_equity_pct,
                requested.cross_mandate_exposure_pct, username, req.reason)

    await auth_mgr.log_audit_event(
        username=username, action="RISK_LIMITS_UPDATE", ip_address=client_ip,
        details={"environment": req.environment, "account_id": req.account_id, "reason": req.reason,
                 "before": current.as_dict(), "after": requested.as_dict(),
                 "tightened": list(check.tightened), "loosened": list(check.loosened),
                 "totp_verified": check.needs_totp})
    return {
        "success": True,
        **_risk_limits_view(req.environment, req.account_id, saved),
        "tightened": list(check.tightened),
        "loosened": list(check.loosened),
        "applies_within_s": RISK_LIMITS_APPLY_S,
    }


@router.get("/enforced-limits")
async def get_enforced_limits(request: Request, auth: dict = Depends(require_operator)):
    """The limits the paper orchestrator actually enforces, as it last published them; ``paper: null``
    when unknown (orchestrator not running, or not published yet)."""
    hub = getattr(request.app.state, "stream_hub", None)
    redis = getattr(hub, "redis", None)
    raw = None
    if redis is not None:
        try:
            raw = await redis.get(EFFECTIVE_LIMITS_KEY)
        except Exception:
            raw = None
    try:
        data = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("paper"), dict):
        return {"as_of": None, "paper": None}
    return {"as_of": data.get("as_of"), "paper": {k: str(v) for k, v in data["paper"].items()}}


@router.get("/balances")
async def get_trading_balances(
    request: Request,
    environment: str = "DEMO",
    auth: dict = Depends(require_operator),
):
    """Retrieve multi-asset balances from trading_balances."""
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT environment, account_id, asset, free, locked,
                   reconciliation_status, updated_at
            FROM trading_balances
            WHERE environment = $1
            ORDER BY asset ASC;
            """,
            environment,
        )
        return [
            {
                "environment": r["environment"],
                "account_id": r["account_id"],
                "asset": r["asset"],
                "free": float(r["free"]),
                "locked": float(r["locked"]),
                "total": float(r["free"] + r["locked"]),
                "reconciliation_status": r["reconciliation_status"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]


@router.get("/orders")
async def get_trading_orders(
    request: Request,
    environment: str = "DEMO",
    limit: int = 50,
    auth: dict = Depends(require_operator),
):
    """Retrieve recent orders from trading_orders."""
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT client_order_id, venue_order_id, strategy_id, instrument_id,
                   side, order_type, time_in_force, price, quantity, filled_qty,
                   status, raw_venue_status, reject_reason, created_at, updated_at
            FROM trading_orders
            WHERE environment = $1
            ORDER BY created_at DESC
            LIMIT $2;
            """,
            environment,
            limit,
        )
        return [
            {
                "client_order_id": r["client_order_id"],
                "venue_order_id": r["venue_order_id"],
                "strategy_id": r["strategy_id"],
                "instrument_id": r["instrument_id"],
                "side": r["side"],
                "order_type": r["order_type"],
                "time_in_force": r["time_in_force"],
                "price": float(r["price"]) if r["price"] is not None else None,
                "quantity": float(r["quantity"]) if r["quantity"] is not None else None,
                "filled_qty": float(r["filled_qty"]) if r["filled_qty"] is not None else None,
                "status": r["status"],
                "raw_venue_status": r["raw_venue_status"],
                "reject_reason": r["reject_reason"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]


@router.get("/reconciliation")
async def get_reconciliation_overview(
    request: Request,
    environment: str = "DEMO",
    auth: dict = Depends(require_operator),
):
    """Retrieve latest reconciliation runs and active differences."""
    async with request.app.state.pool.acquire() as conn:
        latest_run = await conn.fetchrow(
            """
            SELECT run_id, environment, account_id, trigger_type, status,
                   balances_matched, orders_matched, positions_matched,
                   diff_summary, started_at, completed_at
            FROM reconciliation_runs
            WHERE environment = $1
            ORDER BY started_at DESC
            LIMIT 1;
            """,
            environment,
        )

        diff_rows = await conn.fetch(
            """
            SELECT difference_id, run_id, entity_type, entity_id,
                   discrepancy_type, local_state, venue_state,
                   action_taken, resolved, created_at
            FROM reconciliation_differences
            ORDER BY created_at DESC
            LIMIT 20;
            """,
        )

        return {
            "latest_run": {
                "run_id": str(latest_run["run_id"]),
                "environment": latest_run["environment"],
                "account_id": latest_run["account_id"],
                "trigger_type": latest_run["trigger_type"],
                "status": latest_run["status"],
                "balances_matched": latest_run["balances_matched"],
                "orders_matched": latest_run["orders_matched"],
                "positions_matched": latest_run["positions_matched"],
                "diff_summary": latest_run["diff_summary"],
                "started_at": latest_run["started_at"].isoformat() if latest_run["started_at"] else None,
                "completed_at": latest_run["completed_at"].isoformat() if latest_run["completed_at"] else None,
            } if latest_run else None,
            "differences": [
                {
                    "difference_id": str(d["difference_id"]),
                    "run_id": str(d["run_id"]),
                    "entity_type": d["entity_type"],
                    "entity_id": d["entity_id"],
                    "discrepancy_type": d["discrepancy_type"],
                    "local_state": d["local_state"],
                    "venue_state": d["venue_state"],
                    "action_taken": d["action_taken"],
                    "resolved": d["resolved"],
                    "created_at": d["created_at"].isoformat() if d["created_at"] else None,
                }
                for d in diff_rows
            ],
            "unresolved_count": sum(1 for d in diff_rows if not d["resolved"]),
        }


@router.get("/status")
async def get_system_status():
    import os
    use_mock = os.getenv("USE_MOCK_VENUE", "false").lower() in ("1", "true", "yes")
    return {"execution_mode": "MOCK" if use_mock else "LIVE_TESTNET"}
