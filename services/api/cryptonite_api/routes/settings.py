import json
import logging
import os
import uuid
from decimal import Decimal
from typing import Any

from cryptonite_agents.gateway.budget import DEFAULT_DAILY_CAP_USD, parse_cap
from cryptonite_api.auth import OperatorAuthManager
from cryptonite_api.routes.ai import require_totp
from cryptonite_api.routes.auth import get_auth_manager, require_csrf, require_operator
from cryptonite_api.routes.control import _OVERRIDE_SQL, verify_csrf
from cryptonite_api.settings_registry import (
    REGISTRY,
    WRITE_PATHS,
    WriteRefused,
    current_values,
    entry_view,
    validate_value,
    write_needs_totp,
)
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

class AgentConfigUpdate(BaseModel):
    model_name: str | None = Field(default=None, max_length=100)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    system_instructions: str | None = Field(default=None, max_length=5000)

@router.get("/agents")
async def get_all_agent_configs(request: Request, auth: dict = Depends(require_operator)) -> dict[str, Any]:
    pool = request.app.state.pool
    rows = await pool.fetch("SELECT agent_name, model_name, temperature, system_instructions FROM agent_configs")
    configs = {}
    for r in rows:
        configs[r["agent_name"]] = {
            "model_name": r["model_name"],
            "temperature": r["temperature"],
            "system_instructions": r["system_instructions"]
        }
    return configs

@router.patch("/agents/{agent_name}")
@router.put("/agents/{agent_name}")
async def update_agent_config(agent_name: str, body: AgentConfigUpdate, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)):
    valid_agents = ["research", "explainer", "generator", "hunter", "strategist"]
    if agent_name not in valid_agents:
        raise HTTPException(status_code=400, detail="Invalid agent name")
    
    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        return {"status": "success", "message": "No fields to update"}
        
    pool = request.app.state.pool
    
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Get existing to merge
            row = await conn.fetchrow("SELECT model_name, temperature, system_instructions FROM agent_configs WHERE agent_name = $1", agent_name)
            current = dict(row) if row else {"model_name": "gemini-1.5-pro", "temperature": 0.0, "system_instructions": ""}
            
            merged = {**current, **update_data}
            
            await conn.execute(
                """
                INSERT INTO agent_configs (agent_name, model_name, temperature, system_instructions)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (agent_name) DO UPDATE SET
                    model_name = EXCLUDED.model_name,
                    temperature = EXCLUDED.temperature,
                    system_instructions = EXCLUDED.system_instructions,
                    updated_at = now()
                """,
                agent_name,
                merged["model_name"],
                merged["temperature"],
                merged["system_instructions"]
            )
            
            # Extract user info for audit from session or auth_manager if available (defaulting to system)
            # Transactionally insert audit record
            # In auth_audit_events table: id, user_id, event_type, ip_address, user_agent, event_metadata
            try:
                operator_username = auth.get("username", "system") if auth else "system"
                client_ip = request.client.host if request.client else "127.0.0.1"
                user_agent = request.headers.get("user-agent", "unknown")
                
                diff = {k: {"old": current.get(k), "new": update_data[k]} for k in update_data if current.get(k) != update_data[k]}
                
                await conn.execute(
                    """
                    INSERT INTO auth_audit_events (event_id, username, action, ip_address, user_agent, details, occurred_at)
                    VALUES ($1, $2, $3, $4, $5, $6, now())
                    """,
                    uuid.uuid4(),
                    operator_username,
                    "agent_config_update",
                    client_ip,
                    user_agent,
                    str(diff)
                )
            except Exception as e:
                logger.warning(f"Failed to write audit log: {e}")
                
    return {"status": "success"}

@router.get("/environment")
async def get_environment_health(auth: dict = Depends(require_operator)) -> dict[str, Any]:
    gemini_key = os.getenv("GEMINI_API_KEY")
    binance_key = os.getenv("BINANCE_DEMO_API_KEY")
    env_mode = os.getenv("ENVIRONMENT", "DEMO")
    
    # Check if kill switch marker exists
    kill_switch_path = os.getenv("EMERGENCY_MARKER_PATH", "/data/executor_state/demo/emergency_kill_switch.marker")
    kill_switch_active = os.path.exists(kill_switch_path)
    
    return {
        "gemini_key_set": bool(gemini_key and len(gemini_key) > 10),
        "binance_key_set": bool(binance_key and len(binance_key) > 10),
        "environment_mode": env_mode,
        "kill_switch_active": kill_switch_active
    }


@router.get("/registry")
async def get_settings_registry(request: Request, auth: dict = Depends(require_operator)) -> dict[str, Any]:
    """The settings registry (ADR 0007): every setting with its tier and apply mode, and the value the API can read."""
    async with request.app.state.pool.acquire() as conn:
        budget_value = await conn.fetchval("SELECT value FROM system_settings WHERE key = 'LLM_DAILY_BUDGET_USD'")
        row = await conn.fetchrow(_OVERRIDE_SQL, "PAPER", "PAPER-ACCOUNT-001")
    values = current_values(budget_value, row)
    return {"settings": [entry_view(setting, values.get(setting.key)) for setting in REGISTRY]}


class RegistryWriteRequest(BaseModel):
    value: Decimal = Field(max_digits=12, decimal_places=2)
    reason: str = Field(..., min_length=5, max_length=300, description="Audited reason for the change")
    totp_code: str | None = Field(default=None, description="Operator TOTP; required only to raise the value")


@router.put("/registry/{key}")
async def update_registry_setting(
    key: str,
    body: RegistryWriteRequest,
    request: Request,
    auth: dict = Depends(require_operator),
    auth_mgr: OperatorAuthManager = Depends(get_auth_manager),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    """Write one registry setting (ADR 0007). Lowering the LLM budget is free; raising it needs the operator's TOTP.
    Refusals: 404 unknown key, 403 no write path or tier, 422 out of bounds, 401 TOTP missing or wrong. A refusal
    changes nothing."""
    verify_csrf(auth, x_csrf_token)
    setting = next((s for s in REGISTRY if s.key == key), None)
    if setting is None:
        raise HTTPException(status_code=404, detail="Unknown setting")
    if key not in WRITE_PATHS or not setting.writable:
        raise HTTPException(status_code=403, detail=f"{key} is not writable from the Settings module")
    try:
        validate_value(setting, body.value)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=[
            {"loc": ["body", "value"], "msg": f"{key}: {e}", "type": "registry_bounds"}])
    new = body.value.quantize(Decimal("0.01"))
    username = auth.get("username") or f"machine:{auth.get('role', 'operator')}"
    client_ip = request.client.host if request.client else None

    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            # One writer per key: the TOTP decision is made against the stored value
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"system_settings:{key}")
            stored = await conn.fetchval("SELECT value FROM system_settings WHERE key = $1", key)
            current = parse_cap(stored, DEFAULT_DAILY_CAP_USD).quantize(Decimal("0.01"))
            if new == current:
                return {"success": True, "key": key, "changed": False, "previous": format(current, "f"),
                        "value": format(current, "f"), "totp_verified": False}
            try:
                needs_totp = write_needs_totp(setting, current, new)
            except WriteRefused as e:  # the policy's own refusal, kept here so the rule lives in one place
                raise HTTPException(status_code=403, detail=str(e))
            if needs_totp:
                try:
                    await require_totp(request, auth, body.totp_code)
                except HTTPException as e:
                    await auth_mgr.log_audit_event(
                        username=username, action="SETTING_UPDATE_FAILED_AUTH", ip_address=client_ip,
                        details={"key": key, "before": format(current, "f"), "after": format(new, "f"),
                                 "status": e.status_code})
                    if e.status_code == status.HTTP_401_UNAUTHORIZED:
                        raise HTTPException(status_code=401, detail=f"TOTP_REQUIRED: raising {key} needs your current TOTP code")
                    raise
            await conn.execute(
                """INSERT INTO system_settings (key, value, updated_at) VALUES ($1, $2::jsonb, now())
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                key, json.dumps(format(new, "f")))

    await auth_mgr.log_audit_event(
        username=username, action="SETTING_UPDATE", ip_address=client_ip,
        details={"key": key, "before": format(current, "f"), "after": format(new, "f"), "reason": body.reason,
                 "totp_verified": needs_totp})
    return {"success": True, "key": key, "changed": True, "previous": format(current, "f"),
            "value": format(new, "f"), "totp_verified": needs_totp, "applies_within_s": 10}  # BudgetGuard re-reads every 10 s
