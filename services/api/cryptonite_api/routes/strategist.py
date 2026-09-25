import json
from typing import Any

from cryptonite_agents.strategist_engine import StrategistEngine
from cryptonite_schemas.strategist import MultiTimeframeSnapshot, StrategistSignal
from cryptonite_api.routes.auth import AUTHED_READ, OPERATOR_WRITE, require_operator, require_csrf
from fastapi import APIRouter, Body, HTTPException, Request

router = APIRouter(prefix="/api/v1/strategist", tags=["strategist"])

REDIS_INSTRUCTION_KEY = "cryptonite:agent:strategist:instructions"
REDIS_STREAM_KEY = "cryptonite:stream:strategist_signal"


@router.post("/analyze", response_model=StrategistSignal, dependencies=OPERATOR_WRITE)
async def analyze_snapshot(request: Request, snapshot: MultiTimeframeSnapshot = Body(...)) -> Any:
    """Run the deterministic strategy engine on a provided MultiTimeframeSnapshot."""
    try:
        engine = StrategistEngine()
        signal = engine.analyze(snapshot)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Analysis failed: {str(e)}")

    redis = getattr(request.app.state, "stream_hub", None)
    if redis and hasattr(redis, "redis") and redis.redis:
        try:
            # Publish to stream for outbox/executor
            payload = {"payload": signal.model_dump_json()}
            await redis.redis.xadd(REDIS_STREAM_KEY, payload)
        except Exception:
            # Swallow redis errors if stream fails, we still want to return the signal to the caller
            pass

    return signal


@router.get("/instructions", dependencies=AUTHED_READ)
async def get_instructions(request: Request) -> dict[str, str]:
    """Retrieve the custom agent instructions from Redis."""
    redis = getattr(request.app.state, "stream_hub", None)
    if not redis or not hasattr(redis, "redis") or not redis.redis:
        raise HTTPException(status_code=500, detail="Redis connection unavailable.")

    try:
        instructions = await redis.redis.get(REDIS_INSTRUCTION_KEY)
        return {"instructions": instructions or ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Redis error: {str(e)}")


@router.put("/instructions", dependencies=OPERATOR_WRITE)
async def update_instructions(request: Request, payload: dict[str, str] = Body(...)) -> dict[str, str]:
    """Update the custom agent instructions in Redis."""
    instructions = payload.get("instructions", "").strip()
    
    redis = getattr(request.app.state, "stream_hub", None)
    if not redis or not hasattr(redis, "redis") or not redis.redis:
        raise HTTPException(status_code=500, detail="Redis connection unavailable.")

    try:
        if instructions:
            await redis.redis.set(REDIS_INSTRUCTION_KEY, instructions)
        else:
            await redis.redis.delete(REDIS_INSTRUCTION_KEY)
        return {"status": "success", "instructions": instructions}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Redis error: {str(e)}")
