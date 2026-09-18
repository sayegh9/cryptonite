import logging
import os
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Any, Dict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

class AgentConfigUpdate(BaseModel):
    model_name: str | None = None
    temperature: float | None = None
    system_instructions: str | None = None

@router.get("/agents")
async def get_all_agent_configs(request: Request) -> Dict[str, Any]:
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

@router.put("/agents/{agent_name}")
async def update_agent_config(agent_name: str, body: AgentConfigUpdate, request: Request):
    valid_agents = ["research", "explainer", "generator", "hunter", "strategist"]
    if agent_name not in valid_agents:
        raise HTTPException(status_code=400, detail="Invalid agent name")
    
    pool = request.app.state.pool
    
    await pool.execute(
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
        body.model_name,
        body.temperature,
        body.system_instructions
    )
    return {"status": "success"}

@router.get("/environment")
async def get_environment_health() -> Dict[str, Any]:
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
