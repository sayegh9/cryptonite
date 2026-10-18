import json
import logging
import os
import uuid
from decimal import Decimal
from typing import Any

from cryptonite_agents.gateway.routes import DEFAULT_ROUTES, PROTECTED_ROLES
from cryptonite_api.auth import OperatorAuthManager
from cryptonite_api.routes.ai import require_totp
from cryptonite_api.routes.auth import get_auth_manager, require_csrf, require_operator
from cryptonite_api.routes.control import _OVERRIDE_SQL, verify_csrf
from cryptonite_api.routing_policy import EDITABLE, route_view, route_violations, same_value
from cryptonite_api.settings_registry import (
    EXIT_MONITOR_HEARTBEAT,
    PROD_ENVS,
    REGISTRY,
    WRITE_PATHS,
    WriteRefused,
    current_values,
    entry_view,
    exit_rule_violations,
    health_warnings,
    heartbeat_exit_values,
    quantum,
    stored_decimal,
    validate_value,
    write_needs_totp,
)
from cryptonite_schemas.exit_thresholds import SETTING_KEYS
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

def _truthy_setting(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().strip('"').lower() in {"true", "1", "yes", "on"}


def _parse_details(details: Any) -> Any:
    if isinstance(details, str):
        try:
            return json.loads(details)
        except (json.JSONDecodeError, TypeError):
            return details
    return details


@router.get("/environment")
async def get_environment_health(
    request: Request = None,
    auth: dict = Depends(require_operator),
) -> dict[str, Any]:
    gemini_key = os.getenv("GEMINI_API_KEY")
    binance_key = os.getenv("BINANCE_DEMO_API_KEY")
    env_mode = os.getenv("ENVIRONMENT", "DEMO")
    
    # Check if kill switch marker exists
    kill_switch_path = os.getenv("EMERGENCY_MARKER_PATH", "/data/executor_state/demo/emergency_kill_switch.marker")
    kill_switch_active = os.path.exists(kill_switch_path)
    
    # The same env names and defaults the gates read: the login (auth.py), the AI approvals (ai.py), the cookie (routes/auth.py)
    gate_env = os.getenv("ENVIRONMENT", "DEV").upper()
    dev_totp_login = os.getenv("ALLOW_DEV_TOTP", "true").lower() in ("true", "1", "yes") and gate_env not in PROD_ENVS
    dev_totp_approvals = os.getenv("AI_ALLOW_DEV_TOTP", "false").lower() in ("true", "1", "yes")
    secure_cookie = os.getenv("SECURE_COOKIE", "false").lower() in ("true", "1", "yes")

    shadow_allow_paid = False
    if request is not None:
        pool = getattr(getattr(request, "app", None), "state", None)
        pool = getattr(pool, "pool", None)
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    raw = await conn.fetchval("SELECT value FROM system_settings WHERE key = 'AI_SHADOW_ALLOW_PAID'")
                    shadow_allow_paid = _truthy_setting(raw)
            except Exception as e:
                logger.warning(f"Failed to fetch shadow_allow_paid for environment: {e}")
                shadow_allow_paid = False

    return {
        "gemini_key_set": bool(gemini_key and len(gemini_key) > 10),
        "binance_key_set": bool(binance_key and len(binance_key) > 10),
        "environment_mode": env_mode,
        "kill_switch_active": kill_switch_active,
        "dev_totp_login": dev_totp_login,
        "dev_totp_approvals": dev_totp_approvals,
        "secure_cookie": secure_cookie,
        "shadow_allow_paid": shadow_allow_paid,
        "warnings": health_warnings(
            environment=gate_env, dev_totp_login=dev_totp_login, dev_totp_approvals=dev_totp_approvals,
            secure_cookie=secure_cookie, kill_switch_active=kill_switch_active),
    }


async def _exit_monitor_heartbeat(request: Request) -> Any:
    """The exit monitor's heartbeat (the set it applies), or None when the API cannot read it."""
    redis = getattr(getattr(request.app.state, "stream_hub", None), "redis", None)
    if redis is None:
        return None
    try:
        return await redis.get(EXIT_MONITOR_HEARTBEAT)
    except Exception:
        return None


@router.get("/registry")
async def get_settings_registry(request: Request, auth: dict = Depends(require_operator)) -> dict[str, Any]:
    """The settings registry (ADR 0007): every setting with its tier and apply mode, the value stored for it, and for
    the exit thresholds the value the monitor reports it applies."""
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM system_settings WHERE key = ANY($1)", list(WRITE_PATHS))
        row = await conn.fetchrow(_OVERRIDE_SQL, "PAPER", "PAPER-ACCOUNT-001")
    values = current_values({r["key"]: r["value"] for r in rows}, row)
    applied = heartbeat_exit_values(await _exit_monitor_heartbeat(request))
    return {"settings": [entry_view(setting, values.get(setting.key), applied.get(setting.key)) for setting in REGISTRY]}


class RegistryWriteRequest(BaseModel):
    value: Decimal = Field(max_digits=12)
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
    """Write one registry setting (ADR 0007). Lowering a value is free and raising it needs the operator's TOTP (for
    every T/L key, lower is tighter). The exit thresholds must also pass their cross-field rules as a set.
    Refusals: 404 unknown key, 403 no write path or tier, 422 out of bounds, decimals or a rule, 401 TOTP missing or
    wrong. A refusal changes nothing."""
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
    new = body.value.quantize(quantum(setting))
    username = auth.get("username") or f"machine:{auth.get('role', 'operator')}"
    client_ip = request.client.host if request.client else None

    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            # One writer for the whole registry: the TOTP and the cross-field rules are decided against stored values
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", "system_settings:registry")
            rows = await conn.fetch("SELECT key, value FROM system_settings WHERE key = ANY($1)", list(WRITE_PATHS))
            stored = {r["key"]: r["value"] for r in rows}
            current = stored_decimal(stored.get(key), setting)
            if new == current:
                return {"success": True, "key": key, "changed": False, "previous": format(current, "f"),
                        "value": format(current, "f"), "totp_verified": False}
            if key in SETTING_KEYS:
                found = exit_rule_violations(stored, setting, new)
                if found:
                    raise HTTPException(status_code=422, detail=[
                        {"loc": ["body", "value"], "msg": msg, "type": "registry_rule"} for msg in found])
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


@router.get("/routes")
async def get_model_routes(request: Request, auth: dict = Depends(require_operator)) -> dict[str, Any]:
    """Every role's route (ADR 0007, Phase 2): the code default, what the gateway applies now, and what is stored."""
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM ai_model_routes")
    stored = {r["role"]: dict(r) for r in rows}
    return {"routes": [route_view(role, DEFAULT_ROUTES[role], stored.get(role)) for role in sorted(DEFAULT_ROUTES)]}


class RouteWriteRequest(BaseModel):
    """The editable fields of one route. A null field takes the code default (the gateway's NULL rule), so a PUT sets
    the whole editable set."""

    primary_model: str | None = Field(default=None, max_length=128)
    fallback_model: str | None = Field(default=None, max_length=128)
    fallback_policy: str | None = Field(default=None, max_length=16)
    daily_cap_usd: Decimal | None = Field(default=None, max_digits=12, decimal_places=4)
    max_output_tokens: int | None = None
    thinking_budget: int | None = None
    reason: str = Field(..., min_length=5, max_length=300, description="Audited reason for the change")
    totp_code: str | None = Field(default=None, description="Operator TOTP; every route change needs it")


@router.put("/routes/{role}")
async def update_model_route(
    role: str,
    body: RouteWriteRequest,
    request: Request,
    auth: dict = Depends(require_operator),
    auth_mgr: OperatorAuthManager = Depends(get_auth_manager),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    """Write one role's route (ADR 0007, Phase 2). Every change needs the operator's TOTP. Refusals: 404 unknown role,
    403 protected role (locked), 422 a model, policy or limit the gateway may not use, 401 TOTP missing or wrong. A
    refusal changes nothing; the gateway picks a change up within its route cache (30 s)."""
    verify_csrf(auth, x_csrf_token)
    if role not in DEFAULT_ROUTES:
        raise HTTPException(status_code=404, detail="Unknown role")
    if role in PROTECTED_ROLES:
        raise HTTPException(status_code=403, detail=f"{role} is locked: a protected trade role keeps its certified route")
    values = {field: getattr(body, field) for field in EDITABLE}
    problems = route_violations(role, values)
    if problems:
        raise HTTPException(status_code=422, detail=[{"loc": ["body"], "msg": f"{role}: {m}", "type": "route_rule"}
                                                     for m in problems])
    username = auth.get("username") or f"machine:{auth.get('role', 'operator')}"
    client_ip = request.client.host if request.client else None

    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", "ai_model_routes:write")
            row = await conn.fetchrow("SELECT * FROM ai_model_routes WHERE role = $1", role)
            before = {field: (row[field] if row is not None else None) for field in EDITABLE}
            if all(same_value(before[field], values[field]) for field in EDITABLE):
                return {"success": True, "role": role, "changed": False}
            try:
                await require_totp(request, auth, body.totp_code)
            except HTTPException as e:
                await auth_mgr.log_audit_event(
                    username=username, action="ROUTE_UPDATE_FAILED_AUTH", ip_address=client_ip,
                    details={"role": role, "status": e.status_code})
                if e.status_code == status.HTTP_401_UNAUTHORIZED:
                    raise HTTPException(status_code=401, detail="TOTP_REQUIRED: changing a model route needs your current TOTP code")
                raise
            await conn.execute(
                """INSERT INTO ai_model_routes (role, primary_model, fallback_model, fallback_policy, max_output_tokens,
                       thinking_budget, daily_cap_usd, updated_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, now())
                   ON CONFLICT (role) DO UPDATE SET
                       primary_model = EXCLUDED.primary_model, fallback_model = EXCLUDED.fallback_model,
                       fallback_policy = EXCLUDED.fallback_policy, max_output_tokens = EXCLUDED.max_output_tokens,
                       thinking_budget = EXCLUDED.thinking_budget, daily_cap_usd = EXCLUDED.daily_cap_usd,
                       updated_at = now()""",
                role, values["primary_model"], values["fallback_model"], values["fallback_policy"],
                values["max_output_tokens"], values["thinking_budget"], values["daily_cap_usd"])

    def text(d: dict[str, Any]) -> dict[str, Any]:
        return {field: (None if d[field] is None else str(d[field])) for field in EDITABLE}

    await auth_mgr.log_audit_event(
        username=username, action="ROUTE_UPDATE", ip_address=client_ip,
        details={"role": role, "before": text(before), "after": text(values), "reason": body.reason, "totp_verified": True})
    return {"success": True, "role": role, "changed": True, "applies_within_s": 30}


class ShadowPaidUpdateRequest(BaseModel):
    allow_paid: bool
    reason: str = Field(..., min_length=5, max_length=300, description="Audited reason for the change")


@router.get("/shadow-paid")
async def get_shadow_allow_paid(
    request: Request,
    auth: dict = Depends(require_operator),
) -> dict[str, Any]:
    """Check whether paid shadow candidate models are allowed."""
    allow_paid = False
    pool = getattr(getattr(request, "app", None), "state", None)
    pool = getattr(pool, "pool", None)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                raw = await conn.fetchval("SELECT value FROM system_settings WHERE key = 'AI_SHADOW_ALLOW_PAID'")
                allow_paid = _truthy_setting(raw)
        except Exception as e:
            logger.warning(f"Failed to fetch shadow-paid setting: {e}")
            allow_paid = False
    return {"key": "AI_SHADOW_ALLOW_PAID", "allow_paid": allow_paid, "default": False}


@router.put("/shadow-paid")
async def update_shadow_allow_paid(
    body: ShadowPaidUpdateRequest,
    request: Request,
    auth: dict = Depends(require_operator),
    csrf: dict = Depends(require_csrf),
    auth_mgr: OperatorAuthManager = Depends(get_auth_manager),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> dict[str, Any]:
    """Toggle whether paid shadow candidate models are allowed to run."""
    verify_csrf(auth, x_csrf_token)
    username = auth.get("username") or f"machine:{auth.get('role', 'operator')}"
    client_ip = request.client.host if request.client else None
    val_json = "true" if body.allow_paid else "false"

    async with request.app.state.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO system_settings (key, value, updated_at) VALUES ($1, $2::jsonb, now())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            "AI_SHADOW_ALLOW_PAID", val_json,
        )

    await auth_mgr.log_audit_event(
        username=username,
        action="SETTING_UPDATE",
        ip_address=client_ip,
        details={
            "key": "AI_SHADOW_ALLOW_PAID",
            "allow_paid": body.allow_paid,
            "reason": body.reason,
        },
    )
    return {"success": True, "key": "AI_SHADOW_ALLOW_PAID", "allow_paid": body.allow_paid}


@router.get("/audit")
async def get_settings_audit(
    request: Request,
    auth: dict = Depends(require_operator),
) -> dict[str, Any]:
    """The latest 50 settings-related audit events."""
    pool = getattr(getattr(request, "app", None), "state", None)
    pool = getattr(pool, "pool", None)
    if pool is None:
        return {"events": []}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT event_id, username, action, ip_address, details, occurred_at
               FROM auth_audit_events
               WHERE action IN ('SETTING_UPDATE', 'SETTING_UPDATE_FAILED_AUTH', 'ROUTE_UPDATE', 'ROUTE_UPDATE_FAILED_AUTH', 'agent_config_update')
               ORDER BY occurred_at DESC
               LIMIT 50"""
        )
    events = []
    for r in rows:
        d = dict(r)
        occ = d.get("occurred_at")
        occ_str = occ.isoformat() if hasattr(occ, "isoformat") else (str(occ) if occ is not None else None)
        events.append({
            "event_id": str(d["event_id"]) if d.get("event_id") is not None else None,
            "username": d.get("username"),
            "action": d.get("action"),
            "ip_address": d.get("ip_address"),
            "details": _parse_details(d.get("details")),
            "occurred_at": occ_str,
        })
    return {"events": events}


