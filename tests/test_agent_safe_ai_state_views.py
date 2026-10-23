"""agent_safe_* views of the AI's own state (migration 046), read by the Brain as cryptonite_agent_ro.

Runs against a throwaway DB at Alembic head. Each test's seed rows live in a transaction that is rolled back.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest
from migrated_db import REPO_ROOT, migrated_database

VIEWS = {
    "agent_safe_mandate_health": [
        "id", "name", "status", "autonomy_level", "level_since", "instruments", "capital_budget_usd",
        "max_position_pct", "max_drawdown_pct", "halted_reason", "halted_at",
        "equity", "hwm", "drawdown_pct", "equity_ts",
        # migration 049 (ADR 0010): the limits and today's daily session
        "min_conviction", "min_rr", "daily_loss_limit_pct", "daily_profit_target_pct", "profit_lock_pct",
        "session_date", "session_start_equity", "session_pnl", "session_loss_latched_at", "session_target_hit_at",
        "session_lock_fired_at",
        # migration 050 (ADR 0011): the profile columns
        "environment", "markets", "direction", "style", "max_open_positions", "dry_run", "paused_reason",
    ],
    "agent_safe_debate_verdicts": [
        "run_id", "instrument_id", "status", "trigger_source", "created_at", "completed_at", "failure_reason",
        "mandate_id", "decision_trace_id", "recommendation", "conviction_score", "conviction_level",
        "allocation_pct", "decision_price", "reasoning",
    ],
    "agent_safe_open_approvals": [
        "id", "mandate_id", "action_class", "command_type", "summary", "source", "level_at_creation",
        "timeout_policy", "expires_at", "created_at",
    ],
    "agent_safe_reflections": [
        "id", "run_id", "instrument_id", "timeframe", "volatility_regime", "outcome", "actual_outcome_pnl",
        "alpha_vs_benchmark", "lessons_learned", "created_at",
    ],
    "agent_safe_risk_latches": ["environment", "account_id", "is_latched", "latched_at", "reason"],
    "agent_safe_daily_loss": ["account_id", "utc_trading_day", "daily_realized_pnl", "daily_loss_latched"],
}

BASE_TABLES = (
    "ai_mandates", "ai_mandate_equity", "debate_runs", "portfolio_decisions", "ai_approvals", "reflections",
    "execution_kill_switch", "paper_accounts",
)

BTC = "BTCUSDT.BINANCE"
ETH = "ETHUSDT.BINANCE"
NOW = datetime.now(UTC)


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_viewai") as url:
        yield url


@pytest.fixture
async def conn(db_url):
    c = await asyncpg.connect(db_url)
    tx = c.transaction()
    await tx.start()
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


async def mandate(conn, status="ACTIVE", halted_reason=None):
    mid = uuid.uuid4()
    await conn.execute(
        """INSERT INTO ai_mandates (id, name, capital_budget_usd, instruments, max_position_pct, status,
                                   autonomy_level, halted_reason, halted_at)
           VALUES ($1, $2, 1000, ARRAY[$3], 10, $4, 2, $5, $6)""",
        mid, f"viewtest-{mid.hex[:8]}", BTC, status, halted_reason, NOW if halted_reason else None,
    )
    return mid


async def equity(conn, mandate_id, ts, value, hwm, dd):
    await conn.execute(
        "INSERT INTO ai_mandate_equity (mandate_id, ts, equity, hwm, drawdown_pct) VALUES ($1, $2, $3, $4, $5)",
        mandate_id, ts, value, hwm, dd,
    )


async def debate_run(conn, instrument, created_at, mandate_id=None, status="COMPLETED"):
    rid = uuid.uuid4()
    await conn.execute(
        """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at, mandate_id,
                                   decision_trace_id, failure_reason)
           VALUES ($1, $2, $3, 'SCANNER', $4, $5, $6, NULL)""",
        rid, instrument, status, created_at, mandate_id, uuid.uuid4(),
    )
    return rid


async def decision(conn, run_id, instrument, created_at, recommendation, reasoning):
    await conn.execute(
        """INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, conviction_level,
                                           allocation_pct, reasoning, decision_price, created_at)
           VALUES ($1, $2, $3, 0.8, 'HIGH', 5, $4, 42000, $5)""",
        run_id, instrument, recommendation, reasoning, created_at,
    )


async def approval(conn, mandate_id, status):
    await conn.execute(
        """INSERT INTO ai_approvals (mandate_id, action_class, command_type, payload, summary, source, source_ref,
                                    level_at_creation, timeout_policy, expires_at, status)
           VALUES ($1, 'ADD_RISK', 'SUBMIT_ORDER', '{}'::jsonb, 'buy 1 BTC', 'orchestrator', $2, 2, 'DENY',
                   now() + interval '1 hour', $3)""",
        mandate_id, f"viewtest-{uuid.uuid4().hex}", status,
    )


# --- contract: views exist, the agent role can read them and nothing else ----------------------------------------

@pytest.mark.parametrize("view", sorted(VIEWS))
async def test_view_exists_and_the_agent_role_can_select_it(conn, view):
    assert await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", view)
    assert await conn.fetchval("SELECT has_table_privilege('cryptonite_agent_ro', $1, 'SELECT')", view)


@pytest.mark.parametrize("view", sorted(VIEWS))
async def test_view_exposes_exactly_the_contract_columns(conn, view):
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = $1 "
        "ORDER BY ordinal_position", view,
    )
    assert [r["column_name"] for r in rows] == VIEWS[view]


@pytest.mark.parametrize("table", BASE_TABLES)
async def test_the_agent_role_has_no_select_on_the_base_table(conn, table):
    assert not await conn.fetchval("SELECT has_table_privilege('cryptonite_agent_ro', $1, 'SELECT')", table)
    # Column-level grants would also leak the table, so check every column too
    assert not await conn.fetchval("SELECT has_any_column_privilege('cryptonite_agent_ro', $1, 'SELECT')", table)


@pytest.mark.parametrize("view", sorted(VIEWS))
async def test_view_runs_with_security_barrier(conn, view):
    options = await conn.fetchval("SELECT reloptions FROM pg_class WHERE oid = to_regclass($1)", view)
    assert options is not None and "security_barrier=true" in options


async def test_views_run_as_the_agent_role(conn):
    m = await mandate(conn)
    await conn.execute("SET ROLE cryptonite_agent_ro")
    try:
        for view in VIEWS:
            await conn.fetch(f"SELECT * FROM {view}")
        assert m in {r["id"] for r in await conn.fetch("SELECT id FROM agent_safe_mandate_health")}
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.fetch("SELECT 1 FROM ai_mandates LIMIT 1")
    finally:
        await conn.execute("RESET ROLE")


# --- behaviour ---------------------------------------------------------------------------------------------------

async def test_mandate_health_shows_the_latest_equity_row_and_keeps_mandates_without_one(conn):
    m = await mandate(conn)
    bare = await mandate(conn, status="HALTED", halted_reason="DRAWDOWN")
    # Inserted newest-first: the latest row is the one with the greatest ts, not the last inserted
    await equity(conn, m, NOW - timedelta(hours=1), Decimal("90"), Decimal("110"), Decimal("18.18"))
    await equity(conn, m, NOW - timedelta(hours=2), Decimal("100"), Decimal("110"), Decimal("9.09"))

    rows = {r["id"]: r for r in await conn.fetch("SELECT * FROM agent_safe_mandate_health")}
    assert rows[m]["equity"] == Decimal("90")
    assert rows[m]["hwm"] == Decimal("110")
    assert rows[m]["drawdown_pct"] == Decimal("18.18")
    assert rows[m]["equity_ts"] == NOW - timedelta(hours=1)
    assert rows[bare]["status"] == "HALTED" and rows[bare]["halted_reason"] == "DRAWDOWN"
    assert rows[bare]["equity"] is None and rows[bare]["equity_ts"] is None


async def test_an_archived_mandate_is_absent_from_mandate_health(conn):
    archived = await mandate(conn, status="ARCHIVED")
    paused = await mandate(conn, status="PAUSED")
    ids = {r["id"] for r in await conn.fetch("SELECT id FROM agent_safe_mandate_health")}
    assert paused in ids
    assert archived not in ids


async def test_debate_verdict_joins_the_latest_decision_and_truncates_reasoning(conn):
    m = await mandate(conn)
    run = await debate_run(conn, BTC, NOW - timedelta(days=1), mandate_id=m)
    await decision(conn, run, BTC, NOW - timedelta(days=1, hours=2), "BUY", "older view")
    await decision(conn, run, BTC, NOW - timedelta(days=1, hours=1), "SELL", "R" * 3000)

    rows = [r for r in await conn.fetch("SELECT * FROM agent_safe_debate_verdicts") if r["run_id"] == run]
    assert len(rows) == 1
    row = rows[0]
    assert row["recommendation"] == "SELL"
    assert row["conviction_level"] == "HIGH"
    assert row["decision_price"] == Decimal("42000")
    assert row["mandate_id"] == m
    assert row["reasoning"] == "R" * 1500


async def test_a_debate_run_without_a_decision_is_still_listed(conn):
    run = await debate_run(conn, ETH, NOW - timedelta(hours=3), status="FAILED")
    row = next(r for r in await conn.fetch("SELECT * FROM agent_safe_debate_verdicts") if r["run_id"] == run)
    assert row["status"] == "FAILED" and row["recommendation"] is None


async def test_debate_runs_older_than_30_days_are_absent(conn):
    recent = await debate_run(conn, BTC, NOW - timedelta(days=29))
    old = await debate_run(conn, BTC, NOW - timedelta(days=31))
    run_ids = {r["run_id"] for r in await conn.fetch("SELECT run_id FROM agent_safe_debate_verdicts")}
    assert recent in run_ids
    assert old not in run_ids


async def test_only_pending_approvals_appear(conn):
    m = await mandate(conn)
    # Every value of the ai_approvals chk_ai_approvals_status CHECK constraint
    for status in ("PENDING", "APPROVED", "VETOED", "EXPIRED", "EXPIRED_PROCEEDED", "CANCELLED"):
        await approval(conn, m, status)
    rows = await conn.fetch("SELECT * FROM agent_safe_open_approvals WHERE mandate_id = $1", m)
    assert len(rows) == 1
    assert rows[0]["summary"] == "buy 1 BTC" and rows[0]["timeout_policy"] == "DENY"


async def test_reflection_shows_the_instrument_of_its_run(conn):
    run = await debate_run(conn, ETH, NOW - timedelta(days=2))
    refl = await conn.fetchval(
        """INSERT INTO reflections (run_id, actual_outcome_pnl, alpha_vs_benchmark, lessons_learned,
                                   volatility_regime, timeframe, outcome)
           VALUES ($1, 12.5, 1.2, 'size smaller in chop', 'HIGH_VOL', '1h', 'WIN') RETURNING id""",
        run,
    )
    row = await conn.fetchrow("SELECT * FROM agent_safe_reflections WHERE id = $1", refl)
    assert row["instrument_id"] == ETH
    assert row["timeframe"] == "1h" and row["outcome"] == "WIN"
    assert row["lessons_learned"] == "size smaller in chop"


async def test_risk_latch_and_daily_loss_expose_their_rows(conn):
    await conn.execute(
        """INSERT INTO execution_kill_switch (environment, account_id, is_latched, latched_at, reason)
           VALUES ('VIEWTEST', 'VIEWTEST-KS', TRUE, now(), 'drawdown')"""
    )
    await conn.execute(
        """INSERT INTO paper_accounts (account_id, starting_balance, cash_balance, total_equity, daily_realized_pnl,
                                      daily_loss_latched, utc_trading_day, updated_at)
           VALUES ('VIEWTEST-PA', 1000, 1000, 1000, -50, TRUE, CURRENT_DATE, now())"""
    )
    latch = await conn.fetchrow("SELECT * FROM agent_safe_risk_latches WHERE account_id = 'VIEWTEST-KS'")
    assert latch["environment"] == "VIEWTEST" and latch["is_latched"] is True and latch["reason"] == "drawdown"
    loss = await conn.fetchrow("SELECT * FROM agent_safe_daily_loss WHERE account_id = 'VIEWTEST-PA'")
    assert loss["daily_realized_pnl"] == Decimal("-50") and loss["daily_loss_latched"] is True


# --- migration: downgrade to 045 and back to head ----------------------------------------------------------------

def _alembic(url, *args):
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", *args],
                   cwd=REPO_ROOT, env=env, check=True, capture_output=True)


async def _view_names(url):
    c = await asyncpg.connect(url)
    try:
        return {r["table_name"] for r in await c.fetch(
            "SELECT table_name FROM information_schema.views WHERE table_name LIKE 'agent_safe_%'")}
    finally:
        await c.close()


def test_downgrade_to_045_drops_the_views_and_upgrade_restores_them():
    # Sync test: migrated_database() and _alembic() own their event loops (asyncio.run)
    with migrated_database("cryptonite_viewdown") as url:
        assert set(VIEWS) <= asyncio.run(_view_names(url))
        _alembic(url, "downgrade", "045")
        assert not set(VIEWS) & asyncio.run(_view_names(url))
        _alembic(url, "upgrade", "head")
        assert set(VIEWS) <= asyncio.run(_view_names(url))
