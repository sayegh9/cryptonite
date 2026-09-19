import logging
import os
from cryptonite_api.routes.auth import require_operator, require_csrf
from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, Field
from typing import Any, Dict
import uuid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

class AgentConfigUpdate(BaseModel):
    model_name: str | None = Field(default=None, max_length=100)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    system_instructions: str | None = Field(default=None, max_length=5000)

@router.get("/agents")
async def get_all_agent_configs(request: Request, auth: dict = Depends(require_operator)) -> Dict[str, Any]:
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
                # Need a valid user UUID for the audit record
                system_uuid = uuid.UUID("00000000-0000-0000-0000-000000000000")
                await conn.execute(
                    """
                    INSERT INTO auth_audit_events (event_id, username, action, ip_address, user_agent, details, occurred_at)
                    VALUES ($1, $2, $3, $4, $5, $6, now())
                    """,
                    uuid.uuid4(),
                    "system",
                    "agent_config_update",
                    "127.0.0.1",
                    "system",
                    str(update_data)
                )
            except Exception as e:
                logger.warning(f"Failed to write audit log: {e}")
                
    return {"status": "success"}

@router.get("/environment")
async def get_environment_health(auth: dict = Depends(require_operator)) -> Dict[str, Any]:
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
