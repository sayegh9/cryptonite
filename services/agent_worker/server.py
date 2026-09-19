import logging
import os
import hmac
import hashlib
import time
import asyncpg
from typing import Optional, Callable
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from cryptonite_agents.fleet import get_research_agent, get_explainer_agent, get_strategy_generator_agent, get_opportunity_hunter_agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Cryptonite Agent Worker")

INTERNAL_HMAC_SECRET = os.getenv("INTERNAL_HMAC_SECRET", "default_internal_secret")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://cryptonite_api:api_dev_pass@127.0.0.1:5432/cryptonite")

# Cache for agent configs
class ConfigCache:
    def __init__(self):
        self.configs = {}
        self.last_fetched = None

    async def get_config(self, pool, agent_name: str) -> dict:
        now = datetime.now()
        # 1 minute cache stale age
        if self.last_fetched is None or now - self.last_fetched > timedelta(minutes=1):
            try:
                # Emit telemetry about loading configuration
                logger.info("Loading agent configuration overrides from DB")
                async with pool.acquire(timeout=5.0) as conn:
                    rows = await conn.fetch("SELECT agent_name, model_name, temperature, system_instructions FROM agent_configs")
                    self.configs = {r["agent_name"]: dict(r) for r in rows}
                self.last_fetched = now
            except Exception as e:
                logger.warning(f"Failed to load agent configs, falling back to cache: {e}")
                
        return self.configs.get(agent_name, {})

config_cache = ConfigCache()

@app.on_event("startup")
async def startup_event():
    app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

@app.on_event("shutdown")
async def shutdown_event():
    if app.state.pool:
        await app.state.pool.close()

async def verify_hmac(request: Request):
    if request.url.path == "/health":
        return
        
    ts = request.headers.get("X-Internal-Timestamp")
    nonce = request.headers.get("X-Internal-Nonce")
    signature = request.headers.get("X-Internal-Signature")
    
    if not ts or not nonce or not signature:
        raise HTTPException(status_code=401, detail="Missing HMAC headers")
        
    try:
        ts_int = int(ts)
        if abs(time.time() - ts_int) > 300: # 5 min tolerance
            raise HTTPException(status_code=401, detail="Timestamp expired")
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid timestamp")
        
    body = await request.body()
    body_digest = hashlib.sha256(body).hexdigest()
    
    method = request.method
    path = request.url.path
    msg = f"{method}:{path}:{body_digest}:{ts}:{nonce}".encode("utf-8")
    expected_sig = hmac.new(INTERNAL_HMAC_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    
    if not hmac.compare_digest(expected_sig, signature):
        logger.warning(f"HMAC mismatch for {path}")
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

app.router.dependencies.append(Depends(verify_hmac))

class ChatRequest(BaseModel):
    message: str
    
class ExplainRequest(BaseModel):
    backtest_id: str
    message: str

class StrategistRequest(BaseModel):
    message: str
    instructions: Optional[str] = None

@app.post("/api/v1/agents/chat")
async def chat_with_researcher(req: ChatRequest):
    try:
        config_override = await config_cache.get_config(app.state.pool, "research")
        agent = get_research_agent(config_override)
        if hasattr(agent, "run_async"):
            response = await agent.run_async(req.message)
        else:
            response = agent.run(req.message)
        return {"response": response.text}
    except Exception as e:
        logger.error(f"Research agent error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/agents/strategist")
async def chat_with_strategist(req: StrategistRequest):
    try:
        msg_lower = req.message.lower()
        if "code" in msg_lower or "generate" in msg_lower or "create" in msg_lower or "strategy" in msg_lower:
            config_override = await config_cache.get_config(app.state.pool, "generator")
            agent = get_strategy_generator_agent(config_override)
        else:
            config_override = await config_cache.get_config(app.state.pool, "hunter")
            agent = get_opportunity_hunter_agent(config_override)

        prompt = req.message
        if req.instructions:
            prompt = f"Additional User Instructions: {req.instructions}\n\n{prompt}"
            
        if hasattr(agent, "run_async"):
            response = await agent.run_async(prompt)
        else:
            response = agent.run(prompt)
            
        return {"response": response.text}
    except Exception as e:
        logger.error(f"Strategist agent error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/agents/explain")
async def explain_backtest(req: ExplainRequest):
    try:
        config_override = await config_cache.get_config(app.state.pool, "explainer")
        agent = get_explainer_agent(config_override)
        prompt = f"Backtest ID: {req.backtest_id}\nQuery: {req.message}"
        if hasattr(agent, "run_async"):
            response = await agent.run_async(prompt)
        else:
            response = agent.run(prompt)
        return {"response": response.text}
    except Exception as e:
        logger.error(f"Explainer agent error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "ok"}
