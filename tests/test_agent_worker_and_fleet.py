"""Tests for Agent Worker service, HMAC verification, ConfigCache fallback, and agent fleet dispatching."""

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import sys
from pathlib import Path

agent_worker_dir = str(Path(__file__).parent.parent / "services" / "agent_worker")
if agent_worker_dir not in sys.path:
    sys.path.insert(0, agent_worker_dir)

import server as worker_server
from server import ConfigCache, INTERNAL_HMAC_SECRET, app


def _generate_hmac_headers(method: str, path: str, body: bytes, secret: str = INTERNAL_HMAC_SECRET, ts: int | None = None, nonce: str = "test-nonce-123"):
    if ts is None:
        ts = int(time.time())
    body_digest = hashlib.sha256(body).hexdigest()
    msg = f"{method}:{path}:{body_digest}:{ts}:{nonce}".encode("utf-8")
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
        resp = client.post("/api/v1/agents/chat", json={"message": "hello"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Missing HMAC headers"


def test_agent_worker_expired_hmac_timestamp():
    """Requests with timestamps older or newer than 300s must be rejected with 401."""
    with TestClient(app) as client:
        payload = json.dumps({"message": "test"}).encode("utf-8")
        # 301 seconds in the past
        expired_ts = int(time.time()) - 301
        headers = _generate_hmac_headers("POST", "/api/v1/agents/chat", payload, ts=expired_ts)
        resp = client.post("/api/v1/agents/chat", content=payload, headers=headers)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Timestamp expired"


def test_agent_worker_invalid_hmac_signature():
    """Requests with forged or incorrect signatures must be rejected with 401."""
    with TestClient(app) as client:
        payload = json.dumps({"message": "test"}).encode("utf-8")
        headers = _generate_hmac_headers("POST", "/api/v1/agents/chat", payload, secret="wrong_secret")
        resp = client.post("/api/v1/agents/chat", content=payload, headers=headers)
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


def test_strategist_routes_to_generator_when_keywords_present():
    """Strategist endpoint routes to strategy generator when generation keywords are in message."""
    mock_agent = MagicMock()
    mock_response = MagicMock()
    mock_response.text = AsyncMock(return_value="Generated Strategy Code")
    mock_agent.chat = AsyncMock(return_value=mock_response)
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=None)

    with patch("server.get_strategy_generator_agent", return_value=mock_agent) as mock_get_gen, \
         patch("server.config_cache.get_config", AsyncMock(return_value={})):
        
        with TestClient(app) as client:
            payload = json.dumps({"message": "Please generate code for a new MACD strategy", "instructions": "Use 1h bars"}).encode("utf-8")
            headers = _generate_hmac_headers("POST", "/api/v1/agents/strategist", payload)
            
            resp = client.post("/api/v1/agents/strategist", content=payload, headers=headers)
            assert resp.status_code == 200
            assert resp.json() == {"response": "Generated Strategy Code"}
            
            mock_get_gen.assert_called_once()
            called_prompt = mock_agent.chat.call_args[0][0]
            assert "Additional User Instructions: Use 1h bars" in called_prompt
            assert "Please generate code for a new MACD strategy" in called_prompt


def test_strategist_routes_to_hunter_when_general_query():
    """Strategist endpoint routes to opportunity hunter when no code generation keywords are present."""
    mock_agent = MagicMock()
    mock_response = MagicMock()
    mock_response.text = AsyncMock(return_value="Market opportunities identified")
    mock_agent.chat = AsyncMock(return_value=mock_response)
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=None)

    with patch("server.get_opportunity_hunter_agent", return_value=mock_agent) as mock_get_hunter, \
         patch("server.config_cache.get_config", AsyncMock(return_value={})):
        
        with TestClient(app) as client:
            payload = json.dumps({"message": "What tokens have unusual volume today?"}).encode("utf-8")
            headers = _generate_hmac_headers("POST", "/api/v1/agents/strategist", payload)
            
            resp = client.post("/api/v1/agents/strategist", content=payload, headers=headers)
            assert resp.status_code == 200
            assert resp.json() == {"response": "Market opportunities identified"}
            mock_get_hunter.assert_called_once()


def test_explain_backtest_endpoint_formatting():
    """Explainer endpoint receives backtest_id and formats prompt correctly."""
    mock_agent = MagicMock()
    mock_response = MagicMock()
    mock_response.text = AsyncMock(return_value="Tearsheet explanation")
    mock_agent.chat = AsyncMock(return_value=mock_response)
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=None)

    with patch("server.get_explainer_agent", return_value=mock_agent) as mock_get_explainer, \
         patch("server.config_cache.get_config", AsyncMock(return_value={})):
        
        with TestClient(app) as client:
            payload = json.dumps({"backtest_id": "bt-999-xyz", "message": "Why was max drawdown 15%?"}).encode("utf-8")
            headers = _generate_hmac_headers("POST", "/api/v1/agents/explain", payload)
            
            resp = client.post("/api/v1/agents/explain", content=payload, headers=headers)
            assert resp.status_code == 200
            assert resp.json() == {"response": "Tearsheet explanation"}
            mock_get_explainer.assert_called_once()
            called_prompt = mock_agent.chat.call_args[0][0]
            assert "Backtest ID: bt-999-xyz" in called_prompt
            assert "Query: Why was max drawdown 15%?" in called_prompt
