"""Roundtrip downgrade and upgrade test for Alembic migrations."""

import subprocess
import asyncpg
import pytest

DB_URL = "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"


@pytest.mark.destructive
@pytest.mark.asyncio
async def test_alembic_roundtrip_downgrade_and_upgrade():
    """Verify that migrations can cleanly roll back to base and roll forward to head."""
    # 0. Clean up any extra account rows created by edge-case tests before testing full base downgrade
    pool_init = await asyncpg.create_pool(DB_URL)
    try:
        exists = await pool_init.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'execution_kill_switch');"
        )
        if exists:
            count = await pool_init.fetchval("SELECT count(*) FROM execution_kill_switch;")
            if count > 1:
                min_id = await pool_init.fetchval("SELECT min(id) FROM execution_kill_switch;")
                await pool_init.execute("DELETE FROM execution_kill_switch WHERE id != $1;", min_id)
    finally:
        await pool_init.close()

    # 1. Downgrade to base
    res_down = subprocess.run(
        ["uv", "run", "python", "-m", "alembic", "-c", "infra/migrations/alembic.ini", "downgrade", "base"],
        capture_output=True,
        text=True,
    )
    assert res_down.returncode == 0, f"Downgrade to base failed: {res_down.stderr}"

    # 2. Check that public tables (except alembic_version) were dropped
    pool = await asyncpg.create_pool(DB_URL)
    try:
        tables = await pool.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name != 'alembic_version';"
        )
        assert len(tables) == 0, f"Expected 0 tables after downgrade, found {[t['table_name'] for t in tables]}"
    finally:
        await pool.close()

    # 3. Upgrade to head
    res_up = subprocess.run(
        ["uv", "run", "python", "-m", "alembic", "-c", "infra/migrations/alembic.ini", "upgrade", "head"],
        capture_output=True,
        text=True,
    )
    assert res_up.returncode == 0, f"Upgrade to head failed: {res_up.stderr}"

    # 4. Verify all tables and head version exist
    pool = await asyncpg.create_pool(DB_URL)
    try:
        ver = await pool.fetchval("SELECT version_num FROM alembic_version;")
        assert ver == "013", f"Expected head version 013, got {ver}"

        expected_tables = {
            "strategies",
            "strategy_versions",
            "backtest_runs",
            "backtest_results",
            "instruments",
            "outbox_events",
            "execution_commands",
            "paper_execution_events",
            "integrity_incidents",
            "paper_accounts",
            "paper_strategy_instances",
            "paper_orders",
            "paper_positions",
            "paper_trades",
            "paper_risk_events",
            "execution_kill_switch",
            "operator_users",
            "auth_audit_events",
            "totp_replay_cache",
            "trading_balances",
            "trading_balance_snapshots",
            "trading_orders",
            "trading_order_lists",
            "trading_order_events",
            "trading_fills",
            "execution_reservations",
            "reconciliation_runs",
            "reconciliation_differences",
        }
        rows = await pool.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';"
        )
        table_names = {r["table_name"] for r in rows}
        for exp in expected_tables:
            assert exp in table_names, f"Expected table '{exp}' in schema after upgrade"

        # 5. Restore role grants and baseline instruments to leave dev DB in clean state
        from pathlib import Path
        init_roles_sql = (Path(__file__).parent.parent / "infra" / "docker" / "init-roles.sql").read_text()
        await pool.execute(init_roles_sql)
        await pool.execute(
            """
            INSERT INTO instruments (instrument_id, raw_symbol, venue, market_type, base_currency, quote_currency, price_precision, size_precision, price_increment, size_increment, min_notional, status, is_active)
            VALUES 
            ('BTCUSDT.BINANCE', 'BTCUSDT', 'BINANCE', 'SPOT', 'BTC', 'USDT', 2, 5, 0.01, 0.00001, 10.0, 'TRADING', true),
            ('ETHUSDT.BINANCE', 'ETHUSDT', 'BINANCE', 'SPOT', 'ETH', 'USDT', 2, 4, 0.01, 0.0001, 10.0, 'TRADING', true)
            ON CONFLICT (instrument_id) DO NOTHING;
            """
        )
    finally:
        await pool.close()


@pytest.mark.destructive
@pytest.mark.asyncio
async def test_migration_007_downgrade_refuses_when_multiple_accounts_exist():
    """Verify that migration 007 refuses downgrade when multiple account rows exist to protect history."""
    pool = await asyncpg.create_pool(DB_URL)
    try:
        # Insert second account into execution_kill_switch
        await pool.execute(
            """
            INSERT INTO execution_kill_switch (environment, account_id, is_latched, latch_generation)
            VALUES ('DEMO', 'TEST-ACC-PROTECTED', false, 0)
            ON CONFLICT (environment, account_id) DO NOTHING;
            """
        )

        # Attempt downgrade from 007 to 006
        res = subprocess.run(
            ["uv", "run", "python", "-m", "alembic", "-c", "infra/migrations/alembic.ini", "downgrade", "006"],
            capture_output=True,
            text=True,
        )
        assert res.returncode != 0, "Downgrade must fail when multiple account rows exist"
        assert "Cannot downgrade migration 007" in res.stderr
        assert "account-scoped rows exist" in res.stderr

        # Clean up the extraneous account
        await pool.execute("DELETE FROM execution_kill_switch WHERE account_id = 'TEST-ACC-PROTECTED';")
    finally:
        await pool.close()

