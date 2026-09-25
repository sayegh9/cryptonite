import logging
import os
import hmac
import hashlib
import time
import asyncpg
import redis.asyncio as redis
from typing import Optional, Callable
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from cryptonite_agents.debate_fleet import CRO_CHAT, ONCHAIN_CHAT
from cryptonite_agents.fleet import EXPLAINER, OPPORTUNITY_HUNTER, RESEARCH, STRATEGY_GENERATOR
from cryptonite_agents.gateway import BudgetExceeded, BudgetGuard, Gateway, GatewayError
from cryptonite_agents.persona import Persona

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Cryptonite Agent Worker")

INTERNAL_HMAC_SECRET = os.getenv("INTERNAL_HMAC_SECRET", "default_internal_secret")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://cryptonite_api:api_dev_pass@127.0.0.1:5432/cryptonite")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

class RateLimiter:
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.refill_rate = refill_rate

    async def consume(self, redis_client, key: str, tokens: int = 1) -> bool:
        # Token bucket script
        script = """
        local key = KEYS[1]
        local capacity = tonumber(ARGV[1])
        local refill_rate = tonumber(ARGV[2])
        local requested = tonumber(ARGV[3])
        local now = tonumber(ARGV[4])
        
        local bucket = redis.call("HMGET", key, "tokens", "last_refill")
        local tokens = tonumber(bucket[1])
        local last_refill = tonumber(bucket[2])
        
        if not tokens then
            tokens = capacity
            last_refill = now
        else
            local delta = math.max(0, now - last_refill)
            tokens = math.min(capacity, tokens + (delta * refill_rate))
            last_refill = now
        end
        
        if tokens >= requested then
            tokens = tokens - requested
            redis.call("HMSET", key, "tokens", tokens, "last_refill", last_refill)
            redis.call("EXPIRE", key, 3600)
            return 1
        else
            redis.call("HMSET", key, "tokens", tokens, "last_refill", last_refill)
            redis.call("EXPIRE", key, 3600)
            return 0
        end
        """
        now = time.time()
        result = await redis_client.eval(script, 1, key, self.capacity, self.refill_rate, tokens, now)
        return result == 1

# Allow 100 requests per minute
llm_rate_limiter = RateLimiter(capacity=100, refill_rate=100/60.0)

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
    app.state.redis = redis.from_url(REDIS_URL, decode_responses=True)
    # The gateway writes telemetry as its own role; the model's SQL tools use the read-only agent role
    telemetry_url = os.getenv("AI_TELEMETRY_DATABASE_URL")
    app.state.telemetry_pool = await asyncpg.create_pool(telemetry_url, min_size=1, max_size=3) if telemetry_url else None
    if app.state.telemetry_pool is None:
        logger.error("AI_TELEMETRY_DATABASE_URL not set: LLM spend is counted in this process only")
    app.state.gateway = Gateway(pool=app.state.telemetry_pool, redis=app.state.redis,
                                budget=BudgetGuard(pool=app.state.telemetry_pool, redis=app.state.redis,
                                                   spend_unknown=app.state.telemetry_pool is None))

@app.on_event("shutdown")
async def shutdown_event():
    if app.state.pool:
        await app.state.pool.close()
    if getattr(app.state, "telemetry_pool", None):
        await app.state.telemetry_pool.close()
    if getattr(app.state, "redis", None):
        await app.state.redis.aclose()

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
        
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client:
        nonce_key = f"hmac_nonce:{nonce}"
        is_new = await redis_client.set(nonce_key, "1", nx=True, ex=300)
        if not is_new:
            logger.warning(f"Replay attack detected with nonce {nonce}")
            raise HTTPException(status_code=401, detail="Nonce already used")
            
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

async def ask(persona: Persona, prompt: str, config: dict | None = None) -> dict:
    """Run a persona through the AI Gateway and map gateway failures to HTTP errors."""
    try:
        result = await persona.run(app.state.gateway, prompt, config=config)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=f"LLM budget: {e.reason}")
    except GatewayError as e:
        logger.error(f"{persona.name} agent unavailable: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    return {"response": result.output, "trace_id": str(result.trace_id), "model": result.model}


class ChatRequest(BaseModel):
    message: str
    
class ExplainRequest(BaseModel):
    backtest_id: str
    message: str

class StrategistRequest(BaseModel):
    message: str
    instructions: Optional[str] = None

@app.post("/api/v1/agents/chat")
async def chat_with_researcher(request: Request, req: ChatRequest):
    if not await llm_rate_limiter.consume(request.app.state.redis, "rate_limit:llm:research"):
        raise HTTPException(status_code=429, detail="Too many requests")
    config_override = await config_cache.get_config(app.state.pool, "research")
    return await ask(RESEARCH, req.message, config_override)

@app.post("/api/v1/agents/strategist")
async def chat_with_strategist(request: Request, req: StrategistRequest):
    if not await llm_rate_limiter.consume(request.app.state.redis, "rate_limit:llm:strategist"):
        raise HTTPException(status_code=429, detail="Too many requests")
    msg_lower = req.message.lower()
    is_generation = (
        ("code" in msg_lower and any(w in msg_lower for w in ["generate", "create", "write", "build", "new"]))
        or any(phrase in msg_lower for phrase in ["generate strategy", "create strategy", "write a strategy", "write strategy", "build strategy", "code a strategy", "generate code"])
        or ("generate" in msg_lower and "strategy" in msg_lower)
        or ("create" in msg_lower and "strategy" in msg_lower)
        or ("write" in msg_lower and "strategy" in msg_lower)
    )
    persona = STRATEGY_GENERATOR if is_generation else OPPORTUNITY_HUNTER
    config_override = await config_cache.get_config(app.state.pool, persona.name)

    prompt = req.message
    if req.instructions:
        prompt = f"Additional User Instructions: {req.instructions}\n\n{prompt}"
    return await ask(persona, prompt, config_override)

@app.post("/api/v1/agents/explain")
async def explain_backtest(request: Request, req: ExplainRequest):
    if not await llm_rate_limiter.consume(request.app.state.redis, "rate_limit:llm:explain"):
        raise HTTPException(status_code=429, detail="Too many requests")
    config_override = await config_cache.get_config(app.state.pool, "explainer")
    return await ask(EXPLAINER, f"Backtest ID: {req.backtest_id}\nQuery: {req.message}", config_override)

class CRORequest(BaseModel):
    message: str

class OnChainRequest(BaseModel):
    message: str

@app.post("/api/v1/agents/cro")
async def chat_with_cro(request: Request, req: CRORequest):
    if not await llm_rate_limiter.consume(request.app.state.redis, "rate_limit:llm:cro"):
        raise HTTPException(status_code=429, detail="Too many requests")
    return await ask(CRO_CHAT, f"Operator Query: {req.message}")

@app.post("/api/v1/agents/onchain")
async def chat_with_onchain(request: Request, req: OnChainRequest):
    if not await llm_rate_limiter.consume(request.app.state.redis, "rate_limit:llm:onchain"):
        raise HTTPException(status_code=429, detail="Too many requests")
    return await ask(ONCHAIN_CHAT, f"Operator Query: {req.message}")

@app.get("/health")
def health():
    return {"status": "ok"}
