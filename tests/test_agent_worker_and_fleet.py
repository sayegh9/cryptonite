"""Tests for Agent Worker service, HMAC verification, ConfigCache fallback, and agent fleet dispatching."""

import hashlib
import hmac
import json
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

agent_worker_dir = str(Path(__file__).parent.parent / "services" / "agent_worker")
if agent_worker_dir not in sys.path:
    sys.path.insert(0, agent_worker_dir)

from decimal import Decimal  # noqa: E402

import server as worker_server  # noqa: E402, F401
from cryptonite_agents.gateway import BudgetGuard, Gateway  # noqa: E402
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model  # noqa: E402
from cryptonite_agents.gateway.routes import FLASH, PRO  # noqa: E402
from pydantic_ai.messages import ModelResponse, TextPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402
from server import INTERNAL_HMAC_SECRET, ConfigCache, app  # noqa: E402


def _generate_hmac_headers(method: str, path: str, body: bytes, secret: str = INTERNAL_HMAC_SECRET, ts: int | None = None, nonce: str | None = None):
    if ts is None:
        ts = int(time.time())
    if nonce is None:
        nonce = uuid.uuid4().hex  # server rejects reused nonces as replays
    body_digest = hashlib.sha256(body).hexdigest()
    msg = f"{method}:{path}:{body_digest}:{ts}:{nonce}".encode()
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Internal-Timestamp": str(ts),
        "X-Internal-Nonce": nonce,
        "X-Internal-Signature": sig,
    }


def test_agent_worker_health_bypasses_hmac():
    """Health check endpoint should always return 200 without HMAC headers."""
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_agent_worker_missing_hmac_headers():
    """Requests to protected agent routes without HMAC headers must be rejected with 401."""
    with TestClient(app) as client:
        resp = client.post("/api/v1/agents/cro", json={"message": "hello"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Missing HMAC headers"


def test_agent_worker_expired_hmac_timestamp():
    """Requests with timestamps older or newer than 300s must be rejected with 401."""
    with TestClient(app) as client:
        payload = json.dumps({"message": "test"}).encode("utf-8")
        # 301 seconds in the past
        expired_ts = int(time.time()) - 301
        headers = _generate_hmac_headers("POST", "/api/v1/agents/cro", payload, ts=expired_ts)
        resp = client.post("/api/v1/agents/cro", content=payload, headers=headers)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Timestamp expired"


def test_agent_worker_invalid_hmac_signature():
    """Requests with forged or incorrect signatures must be rejected with 401."""
    with TestClient(app) as client:
        payload = json.dumps({"message": "test"}).encode("utf-8")
        headers = _generate_hmac_headers("POST", "/api/v1/agents/cro", payload, secret="wrong_secret")
        resp = client.post("/api/v1/agents/cro", content=payload, headers=headers)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid HMAC signature"


@pytest.mark.asyncio
async def test_config_cache_fallback_on_db_failure():
    """ConfigCache returns cached overrides when DB connection fails."""
    cache = ConfigCache()
    cache.configs = {"research": {"model_name": "gemini-2.5-pro", "temperature": 0.2}}
    cache.last_fetched = None

    mock_pool = MagicMock()
    mock_pool.acquire.side_effect = Exception("DB Connection Refused")

    cfg = await cache.get_config(mock_pool, "research")
    assert cfg["model_name"] == "gemini-2.5-pro"
    assert cfg["temperature"] == 0.2


@pytest.mark.asyncio
async def test_config_cache_fetches_from_db_when_stale():
    """ConfigCache queries database and updates internal cache when stale."""
    cache = ConfigCache()
    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {"agent_name": "generator", "model_name": "gemini-2.5-flash", "temperature": 0.4, "system_instructions": "Custom prompt"}
    ]
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    cfg = await cache.get_config(mock_pool, "generator")
    assert cfg["model_name"] == "gemini-2.5-flash"
    assert cfg["temperature"] == 0.4
    assert cfg["system_instructions"] == "Custom prompt"


class CapturingModel:
    """A pydantic-ai FunctionModel that records the instructions and prompt it receives."""

    def __init__(self, reply: str):
        self.reply = reply
        self.instructions: list[str] = []
        self.prompts: list[str] = []

    def model(self) -> FunctionModel:
        def fn(messages, info: AgentInfo):
            request = messages[-1]
            self.instructions.append(getattr(request, "instructions", "") or "")
            self.prompts.append(" ".join(str(getattr(p, "content", "")) for p in request.parts))
            return ModelResponse(parts=[TextPart(self.reply)])
        return FunctionModel(fn)


def post(client, path: str, body: dict):
    payload = json.dumps(body).encode("utf-8")
    return client.post(path, content=payload, headers=_generate_hmac_headers("POST", path, payload))


def run_worker(path: str, body: dict, reply: str):
    """Call an agent_worker endpoint with every Gemini model served by a capturing test model."""
    capture = CapturingModel(reply)
    with override_model(FLASH, capture.model()), override_model(PRO, capture.model()), \
         patch("server.config_cache.get_config", AsyncMock(return_value={})):
        with TestClient(app) as client:
            app.state.gateway = Gateway()  # no DB: keep test calls out of the dev cost ledger
            resp = post(client, path, body)
    return resp, capture


def test_budget_exhaustion_returns_429_not_a_model_call():
    capture = CapturingModel("should not be called")
    with override_model(FLASH, capture.model()), patch("server.config_cache.get_config", AsyncMock(return_value={})):
        with TestClient(app) as client:
            budget = BudgetGuard(default_cap_usd=Decimal("1"))
            client.portal.call(budget.record, "chat.cro", Decimal("5"))
            app.state.gateway = Gateway(budget=budget)
            resp = post(client, "/api/v1/agents/cro", {"message": "hi"})
    assert resp.status_code == 429 and "DAILY_BUDGET" in resp.json()["detail"]
    assert capture.prompts == []
