"""Test Paper Trading API Gateway endpoints, command submissions, lease fencing, and role permissions."""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from cryptonite_api.main import create_app
from cryptonite_execution.engine import PaperTradingEngine
from cryptonite_executor.server import command_poller_loop
from fastapi.testclient import TestClient
from connections import DB_URL as ADMIN_DB_URL, role_db_url

DB_URL = role_db_url("cryptonite_api", "api_dev_pass")

from cryptonite_api.routes.auth import require_csrf, require_operator


@pytest.fixture
def test_client():
    app = create_app()
    mock_pool = MagicMock()
    mock_pool.fetch = AsyncMock(return_value=[])
    mock_pool.fetchrow = AsyncMock(return_value={"id": 1, "is_latched": False, "status": "PENDING", "command_id": uuid.uuid4(), "created_at": "2026-09-08T20:00:00Z"})
    app.state.pool = mock_pool
    app.dependency_overrides[require_operator] = lambda: {"type": "session", "role": "operator"}
    app.dependency_overrides[require_csrf] = lambda: {"type": "session", "role": "operator"}
    return TestClient(app), mock_pool


def test_paper_api_projections_read_endpoints(test_client):
    """Verify read-only endpoints return expected JSON schemas."""
    client, mock_pool = test_client

    # /accounts
    res = client.get("/api/v1/paper/accounts")
    assert res.status_code == 200

    # /positions
    res = client.get("/api/v1/paper/positions")
    assert res.status_code == 200

    # /orders
    res = client.get("/api/v1/paper/orders")
    assert res.status_code == 200

    # /trades
    res = client.get("/api/v1/paper/trades")
    assert res.status_code == 200

    # /strategies
    res = client.get("/api/v1/paper/strategies")
    assert res.status_code == 200

    # /kill-switch
    res = client.get("/api/v1/paper/kill-switch")
    assert res.status_code == 200
    assert "is_latched" in res.json()


def test_paper_api_command_endpoints_insert_only(test_client):
    """Verify that all write endpoints insert into execution_commands only."""
    client, mock_pool = test_client

    # Deploy
    res = client.post(
        "/api/v1/paper/strategies/deploy",
        json={
            "strategy_name": "EMACrossover",
            "strategy_version_id": str(uuid.uuid4()),
            "instrument_id": "BTCUSDT.BINANCE",
            "timeframe": "1m",
            "allocation_usd": 5000.0,
        },
    )
    assert res.status_code == 200
    assert mock_pool.fetchrow.call_count >= 1
    sql = mock_pool.fetchrow.call_args[0][0]
    assert "INSERT INTO execution_commands" in sql

    # Pause
    mock_pool.fetchrow.reset_mock()
    res = client.post("/api/v1/paper/strategies/INST-123/pause")
    assert res.status_code == 200
    sql = mock_pool.fetchrow.call_args[0][0]
    assert "INSERT INTO execution_commands" in sql

    # Resume
    mock_pool.fetchrow.reset_mock()
    res = client.post("/api/v1/paper/strategies/INST-123/resume")
    assert res.status_code == 200

    # Stop
    mock_pool.fetchrow.reset_mock()
    res = client.post("/api/v1/paper/strategies/INST-123/stop")
    assert res.status_code == 200

    # Close position
    mock_pool.fetchrow.reset_mock()
    res = client.post("/api/v1/paper/strategies/INST-123/close-position", json={"instrument_id": "BTCUSDT.BINANCE"})
    assert res.status_code == 200

    # Cancel order
    mock_pool.fetchrow.reset_mock()
    res = client.post("/api/v1/paper/orders/ORD-456/cancel")
    assert res.status_code == 200

    # Trip Kill Switch
    mock_pool.fetchrow.reset_mock()
    res = client.post("/api/v1/paper/kill-switch", json={"reason": "Test emergency", "operator": "admin"})
    assert res.status_code == 200

    # Reset Kill Switch
    mock_pool.fetchrow.reset_mock()
    res = client.post("/api/v1/paper/kill-switch/reset", json={"operator": "admin"})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_paper_database_role_permissions():
    """Verify PostgreSQL role isolation between cryptonite_api and cryptonite_executor."""
    admin_pool = await asyncpg.create_pool(ADMIN_DB_URL)
    try:
        # Check that cryptonite_api can select on paper_accounts but cannot delete
        api_pool = await asyncpg.create_pool(DB_URL)
        try:
            accounts = await api_pool.fetch("SELECT * FROM paper_accounts;")
            assert isinstance(accounts, list)

            # Insert command succeeds
            cmd_id = uuid.uuid4()
            await api_pool.execute(
                """
                INSERT INTO execution_commands (
                    command_id, idempotency_key, command_type, status, requester_id
                ) VALUES ($1, $2, 'TEST_CMD', 'PENDING', 'test');
                """,
                cmd_id,
                f"test:{cmd_id}",
            )

            # API attempting DELETE on paper_execution_events should be rejected by PostgreSQL IAM
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await api_pool.execute("DELETE FROM paper_execution_events;")
        finally:
            await api_pool.close()
    finally:
        await admin_pool.close()


def test_executor_service_health_and_readiness():
    """Verify cryptonite_executor FastAPI /health and /ready behavior."""
    from cryptonite_executor import server
    from fastapi.testclient import TestClient

    client = TestClient(server.app)

    # Health is always 200 OK
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

    # When engine is None, ready is 503
    server.engine = None
    res = client.get("/ready")
    assert res.status_code == 503
    assert res.json()["status"] == "not_ready"

    # When engine is mock and not ready, ready is 503
    mock_eng = MagicMock()
    mock_eng.is_ready = False
    mock_eng.is_gate_open = False
    mock_eng.kill_switch.is_latched = True
    mock_eng.bridge.integrity_failed_event.is_set.return_value = True
    server.engine = mock_eng
    res = client.get("/ready")
    assert res.status_code == 503
    assert res.json()["integrity_failed"] is True

    # When engine is ready, ready is 200
    mock_eng.is_ready = True
    mock_eng.account_id = "ACC-TEST"
    mock_eng.is_gate_open = True
    mock_eng.kill_switch.is_latched = False
    res = client.get("/ready")
    assert res.status_code == 200
    assert res.json()["status"] == "ready"
    assert res.json()["account_id"] == "ACC-TEST"

    # Reset engine
    server.engine = None

    # Test Demo pipeline ready with advisory lock held
    mock_pipe = MagicMock(_mock_unsafe=True)
    mock_pipe.environment = "DEMO"
    mock_pipe.account_id = "DEMO-ACCOUNT-001"
    mock_pipe.worker_id = "test-worker-1"
    mock_pipe.kill_switch.is_latched = False
    mock_pipe.assert_advisory_lock.return_value = None

    server.pipeline = mock_pipe
    server.service_ready = True

    res = client.get("/ready")
    assert res.status_code == 200
    assert res.json()["status"] == "ready"
    assert res.json()["advisory_lock_held"] is True

    # When advisory lock is lost, /ready returns 503
    from cryptonite_execution.demo_pipeline import AdvisoryLockLostError
    mock_pipe.assert_advisory_lock.side_effect = AdvisoryLockLostError("Advisory lock lost")
    res = client.get("/ready")
    assert res.status_code == 503
    assert res.json()["status"] == "not_ready"
    assert res.json()["advisory_lock_held"] is False

    # Cleanup
    server.pipeline = None
    server.service_ready = False

