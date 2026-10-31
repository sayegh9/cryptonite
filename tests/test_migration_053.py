"""Migration 053 exposes only safe profile views and restores 052 on downgrade."""

import asyncio
import datetime as dt
import os
import subprocess
import sys
import uuid

import asyncpg
import pytest
from cryptonite_schemas.mandate import mandate_instance_id
from migrated_db import REPO_ROOT, migrated_database, seed_paper_account


def _alembic(url: str, action: str, revision: str) -> None:
    """Move only the throwaway database to the requested Alembic revision."""
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", action, revision],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
        text=True,
    )


async def _columns(url: str) -> list[str]:
    """Return the health view's public column contract in ordinal order."""
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'agent_safe_mandate_health'
               ORDER BY ordinal_position"""
        )
        return [row["column_name"] for row in rows]
    finally:
        await conn.close()


async def _exercise(url: str) -> None:
    """Check one open position, the retention window, and the agent role's effective privileges."""
    owner = await asyncpg.connect(url)
    try:
        await seed_paper_account(owner)
        mid = await owner.fetchval(
            """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, markets, status)
               VALUES ($1, 1000, ARRAY['BTCUSDT-MARGIN.BINANCE'], 10, ARRAY['SPOT_MARGIN'], 'ACTIVE') RETURNING id""",
            f"m053-{uuid.uuid4().hex[:8]}",
        )
        await owner.execute(
            """INSERT INTO paper_strategy_instances
                   (instance_id, account_id, strategy_version_id, strategy_source_hash,
                    risk_policy_hash, strategy_name, instrument_id, timeframe,
                    resolved_parameters, allocation_usd, status)
               VALUES ($1, 'PAPER-ACCOUNT-001', $2, 'system', 'system', 'migration-053',
                       'BTCUSDT-MARGIN.BINANCE', '1h', '{}'::jsonb, 1000, 'SYSTEM')""",
            mandate_instance_id(mid), uuid.UUID(int=0xA15),
        )
        await owner.execute(
            """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side,
                   entry_price, quantity, current_price, unrealized_pnl, status, opened_at, leverage, margin_mode)
               VALUES ($1, 'PAPER-ACCOUNT-001', $2, 'BTCUSDT-MARGIN.BINANCE', 'LONG',
                       100, 1, 100, 0, 'OPEN', now(), 1, 'ISOLATED')""",
            str(uuid.uuid4()), mandate_instance_id(mid),
        )
        await owner.execute(
            """INSERT INTO ai_profile_decisions (mandate_id, ts, stage, outcome, reason)
               VALUES ($1, $2, 'ROUTE', 'NO_TRADE', 'NO_STRUCTURE'),
                      ($1, $3, 'SCAN', 'CANDIDATE', 'old row')""",
            mid, dt.datetime.now(dt.UTC), dt.datetime.now(dt.UTC) - dt.timedelta(days=91),
        )
        for view in ("agent_safe_mandate_health", "agent_safe_profile_decisions"):
            assert await owner.fetchval("SELECT has_table_privilege('cryptonite_agent_ro', $1, 'SELECT')", view)
        for table in ("ai_mandates", "ai_profile_decisions", "paper_positions"):
            assert not await owner.fetchval("SELECT has_table_privilege('cryptonite_agent_ro', $1, 'SELECT')", table)
        for view in ("agent_safe_mandate_health", "agent_safe_profile_decisions"):
            assert await owner.fetchval("SELECT reloptions::text FROM pg_class WHERE relname = $1", view) == "{security_barrier=true}"
    finally:
        await owner.close()

    agent = await asyncpg.connect(url)
    try:
        await agent.execute("SET ROLE cryptonite_agent_ro")
        row = await agent.fetchrow(
            """SELECT name, max_leverage, max_risk_per_trade_pct, max_debates_per_day,
                      open_positions FROM agent_safe_mandate_health WHERE id = $1""",
            mid,
        )
        assert row["name"].startswith("m053-") and row["open_positions"] == 1
        assert row["max_leverage"] == 1 and row["max_debates_per_day"] == 6
        decisions = await agent.fetch(
            "SELECT stage, outcome, reason_text, detail_json FROM agent_safe_profile_decisions WHERE mandate_id = $1",
            mid,
        )
        assert [(r["stage"], r["outcome"], r["reason_text"]) for r in decisions] == [
            ("ROUTE", "NO_TRADE", "NO_STRUCTURE"),
        ]
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await agent.fetchval("SELECT count(*) FROM ai_mandates")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await agent.fetchval("SELECT count(*) FROM ai_profile_decisions")
    finally:
        await agent.close()


def test_migration_053_round_trip_and_agent_isolation():
    """An up/down/up rehearsal preserves 052 readers and never grants a base table."""
    with migrated_database("cryptonite_m053") as url:
        _alembic(url, "downgrade", "052")
        before = asyncio.run(_columns(url))
        _alembic(url, "upgrade", "053")
        after = asyncio.run(_columns(url))
        assert after[: len(before)] == before
        assert after[len(before) :] == [
            "max_leverage", "max_risk_per_trade_pct", "veto_window_s", "max_debates_per_day",
            "exit_tp_r", "exit_tp1_scale_pct", "exit_trail_activate_pct", "exit_trail_pct",
            "exit_max_hold_hours", "notify_level", "breaker_max_loss_streak",
            "breaker_max_reject_streak", "created_at", "open_positions",
        ]
        asyncio.run(_exercise(url))
        _alembic(url, "downgrade", "052")
        assert asyncio.run(_columns(url)) == before

        async def no_decisions_view() -> bool:
            conn = await asyncpg.connect(url)
            try:
                return await conn.fetchval("SELECT to_regclass('public.agent_safe_profile_decisions') IS NULL")
            finally:
                await conn.close()

        assert asyncio.run(no_decisions_view())
        _alembic(url, "upgrade", "053")
        assert asyncio.run(_columns(url)) == after
        asyncio.run(_exercise(url))
