import logging
import hmac
import hashlib
import time
import json
import secrets
import httpx
import os
from cryptonite_api.routes.auth import require_operator, require_csrf
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

# Ensure HMAC secret exists
INTERNAL_HMAC_SECRET = os.getenv("INTERNAL_HMAC_SECRET", "default_internal_secret")

class ChatRequest(BaseModel):
    message: str

class ExplainRequest(BaseModel):
    backtest_id: str
    message: str

class StrategistRequest(BaseModel):
    message: str
    instructions: str | None = None

def sign_request(method: str, path: str, body: bytes) -> dict:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(8)
    body_digest = hashlib.sha256(body).hexdigest()
    
    msg = f"{method}:{path}:{body_digest}:{timestamp}:{nonce}".encode("utf-8")
    signature = hmac.new(INTERNAL_HMAC_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    
    return {
        "X-Internal-Timestamp": timestamp,
        "X-Internal-Nonce": nonce,
        "X-Internal-Signature": signature
    }

async def forward_request(path: str, req_body: BaseModel):
    method = "POST"
    body_bytes = req_body.model_dump_json().encode("utf-8")
    headers = sign_request(method, path, body_bytes)
    headers["Content-Type"] = "application/json"
    
    worker_url = f"http://cryptonite_agent_worker:8085{path}"
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(worker_url, content=body_bytes, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Failed to forward agent request to {worker_url}: {e}")
        raise HTTPException(status_code=500, detail="Internal agent worker error")

@router.post("/chat")
async def agent_chat(req: ChatRequest, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)):
    return await forward_request("/api/v1/agents/chat", req)

@router.post("/explain")
async def agent_explain(req: ExplainRequest, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)):
    return await forward_request("/api/v1/agents/explain", req)

@router.post("/strategist")
async def agent_strategist(req: StrategistRequest, request: Request, auth: dict = Depends(require_operator), csrf: dict = Depends(require_csrf)):
    return await forward_request("/api/v1/agents/strategist", req)
