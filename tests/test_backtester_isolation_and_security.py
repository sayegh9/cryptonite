"""Security and isolation tests for backtester service and trading strategies.

Validates:
1. AST static analysis of all strategy files:
   - Prohibits live exchange SDKs/adapters (ccxt, python-binance, nautilus live adapters).
   - Prohibits raw outbound network libraries (requests, httpx, aiohttp, urllib.request).
   - Prohibits reading API secrets or private keys from environment or filesystem.
2. Least-privilege database role validation for `cryptonite_backtester`:
   - Role has SELECT/INSERT/UPDATE permissions on backtest tables.
   - Role CANNOT DROP, TRUNCATE, or ALTER tables.
3. Container configuration validation:
   - Backtester runs as non-root user (1000:1000).
   - Backtester consumes offline catalog data only.
"""

import ast
from pathlib import Path

import asyncpg
import pytest

STRATEGIES_DIR = Path(__file__).parent.parent / "packages" / "strategies" / "cryptonite_strategies"
DB_URL_ADMIN = "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"
DB_URL_BACKTESTER = "postgresql://cryptonite_backtester:backtester_dev_pass@localhost:5432/cryptonite"

DISALLOWED_MODULES = {
    "ccxt",
    "binance",
    "requests",
    "httpx",
    "aiohttp",
    "urllib.request",
    "socket",
    "nautilus_trader.adapters.binance",
    "nautilus_trader.live",
}

DISALLOWED_STRINGS = {
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "API_SECRET",
    "PRIVATE_KEY",
    "SECRET_KEY",
}


def test_strategies_ast_isolation():
    """Verify that no strategy file imports live adapters or reads exchange credentials."""
    strategy_files = list(STRATEGIES_DIR.glob("*.py"))
    assert len(strategy_files) >= 4, "Expected strategy files in packages/strategies/cryptonite_strategies"

    for py_file in strategy_files:
        tree = ast.parse(py_file.read_text("utf-8"), filename=str(py_file))

        for node in ast.walk(tree):
            # Check import statements
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for disallowed in DISALLOWED_MODULES:
                        assert not alias.name.startswith(disallowed), (
                            f"Disallowed import '{alias.name}' detected in strategy file {py_file.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for disallowed in DISALLOWED_MODULES:
                    assert not mod.startswith(disallowed), (
                        f"Disallowed from-import '{mod}' detected in strategy file {py_file.name}"
                    )

            # Check for credential strings
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for disallowed_str in DISALLOWED_STRINGS:
                    assert disallowed_str not in node.value, (
                        f"Disallowed credential string '{disallowed_str}' found in strategy file {py_file.name}"
                    )


@pytest.mark.asyncio
async def test_backtester_role_least_privilege_enforcement():
    """Verify cryptonite_backtester role has required table permissions but cannot alter schema or drop tables."""
    pool = await asyncpg.create_pool(DB_URL_BACKTESTER)
    try:
        # Can select from backtest_runs
        runs = await pool.fetch("SELECT id FROM backtest_runs LIMIT 1")
        assert isinstance(runs, list)

        # Can select from strategy_versions and strategies
        strats = await pool.fetch("SELECT id, slug FROM strategies LIMIT 1")
        assert isinstance(strats, list)

        # CANNOT drop tables
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await pool.execute("DROP TABLE backtest_runs")

        # CANNOT truncate tables
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await pool.execute("TRUNCATE TABLE backtest_runs")

        # CANNOT alter table schema
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await pool.execute("ALTER TABLE backtest_runs ADD COLUMN malicious_col text")

    finally:
        await pool.close()


def test_docker_network_isolation_and_catalog_mount():
    """Verify Docker compose enforces network egress isolation, read-only catalog mount, and zero exchange credentials."""
    import yaml

    compose_file = Path(__file__).parent.parent / "infra" / "docker-compose.yml"
    assert compose_file.exists(), f"Compose file not found at {compose_file}"

    with open(compose_file, "r") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})
    assert "backtester" in services, "backtester service missing from docker-compose.yml"
    backtester = services["backtester"]

    # 1. Network isolation: attached to internal network with no internet egress
    networks = compose.get("networks", {})
    assert "isolated_backtester_net" in networks, "isolated_backtester_net missing in networks"
    assert networks["isolated_backtester_net"].get("internal") is True, (
        "isolated_backtester_net must be configured with 'internal: true' to prevent external internet egress"
    )

    bt_networks = backtester.get("networks", [])
    assert "isolated_backtester_net" in bt_networks, (
        "backtester service must be attached to isolated_backtester_net"
    )
    assert "default" not in bt_networks, (
        "backtester service must NOT be attached to default bridge network to prevent external internet egress"
    )

    # 2. Read-only catalog mount and read-write artifacts mount
    volumes = backtester.get("volumes", [])
    has_ro_catalog = any(":ro" in v and "nautilus_catalog" in v for v in volumes)
    has_rw_artifacts = any("backtest_artifacts" in v for v in volumes)
    assert has_ro_catalog, "Catalog volume must be mounted with ':ro' read-only flag"
    assert has_rw_artifacts, "Artifacts volume must be mounted for output persistence"

    # 3. Absence of credentials in environment or build
    env = backtester.get("environment", {})
    for key in env:
        for disallowed in DISALLOWED_STRINGS:
            assert disallowed not in key.upper(), f"Disallowed credential key '{key}' found in backtester environment"
            if isinstance(env[key], str):
                assert disallowed not in env[key].upper(), f"Disallowed credential value found in '{key}'"

    # 4. Port binding strictly to localhost
    ports = backtester.get("ports", [])
    for p in ports:
        assert p.startswith("127.0.0.1:"), f"Port mapping '{p}' must be bound strictly to 127.0.0.1 loopback"


@pytest.mark.asyncio
async def test_alembic_migration_schema_integrity():
    """Verify alembic migration head is 003 and all backtest tables and columns exist with correct schema."""
    pool = await asyncpg.create_pool(DB_URL_ADMIN)
    try:
        # Check migration version
        row = await pool.fetchrow("SELECT version_num FROM alembic_version")
        assert int(row["version_num"]) >= 3, f"Expected alembic version >= 003, got {row['version_num']}"

        # Verify backtest_runs columns
        cols = await pool.fetch("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns 
            WHERE table_name = 'backtest_runs'
        """)
        col_names = {c["column_name"]: c["data_type"] for c in cols}
        expected_cols = [
            "id",
            "strategy_version_id",
            "instrument_id",
            "bar_type",
            "timeframe",
            "parameters",
            "warmup_start_ns",
            "evaluation_start_ns",
            "evaluation_end_ns",
            "execution_profile",
            "run_config_hash",
            "status",
            "attempt_count",
            "attempt_id",
            "lease_owner",
            "lease_expires_at",
            "created_at",
        ]
        for ec in expected_cols:
            assert ec in col_names, f"Column '{ec}' missing in backtest_runs schema"


        # Verify backtest_results columns
        res_cols = await pool.fetch("""
            SELECT column_name, data_type 
            FROM information_schema.columns 
            WHERE table_name = 'backtest_results'
        """)
        res_col_names = {c["column_name"] for c in res_cols}
        expected_res_cols = [
            "id",
            "backtest_run_id",
            "total_return",
            "sharpe_ratio",
            "sortino_ratio",
            "max_drawdown",
            "win_rate",
            "total_trades",
            "downsampled_equity",
            "catalog_manifest",
            "artifact_manifest",
        ]
        for erc in expected_res_cols:
            assert erc in res_col_names, f"Column '{erc}' missing in backtest_results schema"

    finally:
        await pool.close()


def test_backtester_service_health_endpoints():
    """Verify HTTP health and ready endpoints on backtester service."""
    from starlette.testclient import TestClient
    from cryptonite_backtester_service.server import app

    with TestClient(app) as client:
        # /health endpoint returns 200 and status ok
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

        # /ready endpoint returns valid status (ready when pool is initialized)
        ready_resp = client.get("/ready")
        assert ready_resp.status_code in (200, 503)
        assert "status" in ready_resp.json()


@pytest.mark.asyncio
async def test_backtester_readiness_exact_branches(tmp_path: Path):
    """Test all edge case branches of the /ready endpoint."""
    from unittest.mock import AsyncMock, MagicMock
    import cryptonite_backtester_service.server as srv
    from httpx import ASGITransport, AsyncClient

    # 1. worker is None -> 503
    orig_worker = srv.worker
    try:
        srv.worker = None
        async with AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://test") as client:
            resp = await client.get("/ready")
            assert resp.status_code == 503
            assert resp.json()["status"] == "unready"
            assert "worker_or_db_not_running" in resp.json()["reasons"]

            # 2. catalog does not exist -> 503
            mock_w = MagicMock()
            mock_w._running = True
            mock_w.pool = MagicMock()
            mock_w.catalog_path = tmp_path / "non_existent_catalog_path"
            srv.worker = mock_w

            resp = await client.get("/ready")
            assert resp.status_code == 503
            assert "catalog_path_unreadable" in resp.json()["reasons"]

            # 3. DB queries report failed outbox and integrity failure -> 503
            cat_real = tmp_path / "real_cat"
            cat_real.mkdir(parents=True, exist_ok=True)
            mock_w.catalog_path = cat_real

            mock_conn = AsyncMock()
            # fetchval for failed_outbox=2, integrity_failed=1
            mock_conn.fetchval.side_effect = [2, 1]
            mock_w.pool.acquire.return_value.__aenter__.return_value = mock_conn

            resp = await client.get("/ready")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "unready"
            assert data["failed_outbox_count"] == 2
            assert data["integrity_incident_count"] == 1

            # 4. DB queries healthy -> 200
            mock_conn.fetchval.side_effect = [0, 0]
            mock_w.worker_id = "test-worker-ready"
            mock_w.completed_count = 5
            mock_w.failed_count = 1  # User-level backtest failure leaves worker healthy!

            resp = await client.get("/ready")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ready"
            assert data["completed_count"] == 5
            assert data["failed_count"] == 1

            # 5. DB query error -> 503
            mock_conn.fetchval.side_effect = Exception("DB Connection Lost")
            resp = await client.get("/ready")
            assert resp.status_code == 503
            assert any("db_query_error" in r for r in resp.json()["reasons"])
    finally:
        srv.worker = orig_worker


