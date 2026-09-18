import logging
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from cryptonite_agents.fleet import (
    get_research_agent,
    get_explainer_agent,
    get_strategy_generator_agent,
    get_opportunity_hunter_agent,
)

logger = logging.getLogger(__name__)


async def fetch_agent_config(pool, agent_name: str) -> dict:
    row = await pool.fetchrow("SELECT model_name, temperature, system_instructions FROM agent_configs WHERE agent_name = $1", agent_name)
    if not row:
        return {}
    return {
        "model_name": row["model_name"],
        "temperature": row["temperature"],
        "system_instructions": row["system_instructions"],
    }

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

class ChatRequest(BaseModel):
    message: str

class ExplainRequest(BaseModel):
    backtest_id: str
    message: str

class StrategistRequest(BaseModel):
    message: str
    instructions: str | None = None

@router.post("/chat")
async def agent_chat(req: ChatRequest, request: Request):
    config_override = await fetch_agent_config(request.app.state.pool, "research")
    agent = get_research_agent(config_override)
    try:
        # Check if agent has run_async, otherwise fallback to run
        if hasattr(agent, "run_async"):
            response = await agent.run_async(req.message)
        else:
            response = agent.run(req.message)
        return {"response": response.text}
    except Exception as e:
        logger.error(f"Agent chat failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/explain")
async def agent_explain(req: ExplainRequest, request: Request):
    config_override = await fetch_agent_config(request.app.state.pool, "explainer")
    agent = get_explainer_agent(config_override)
    try:
        prompt = f"Backtest ID: {req.backtest_id}\nQuery: {req.message}"
        if hasattr(agent, "run_async"):
            response = await agent.run_async(prompt)
        else:
            response = agent.run(prompt)
        return {"response": response.text}
    except Exception as e:
        logger.error(f"Agent explain failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/strategist")
async def agent_strategist(req: StrategistRequest, request: Request):
    msg_lower = req.message.lower()
    if "code" in msg_lower or "generate" in msg_lower or "create" in msg_lower or "strategy" in msg_lower:
        config_override = await fetch_agent_config(request.app.state.pool, "generator")
        agent = get_strategy_generator_agent(config_override)
    else:
        config_override = await fetch_agent_config(request.app.state.pool, "hunter")
        agent = get_opportunity_hunter_agent(config_override)

    try:
        prompt = req.message
        if req.instructions:
            prompt = f"Additional User Instructions: {req.instructions}\n\n{prompt}"
            
        if hasattr(agent, "run_async"):
            response = await agent.run_async(prompt)
        else:
            response = agent.run(prompt)
            
        return {"response": response.text}
    except Exception as e:
        logger.error(f"Agent strategist failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
