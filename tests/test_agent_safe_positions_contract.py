"""agent_safe_positions view contract (migration 021) for debate_worker and exit_monitor.

Runs against a throwaway DB at Alembic head.
"""

import uuid
from datetime import UTC, datetime

import asyncpg
import pytest
from migrated_db import migrated_database

ACCOUNT = "PAPER-CONTRACT-001"
INSTANCE = "INST-CONTRACT-001"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_viewtest") as url:
        yield url


@pytest.fixture
async def conn(db_url):
    conn = await asyncpg.connect(db_url)
    await conn.execute("TRUNCATE paper_positions, paper_strategy_instances, paper_accounts CASCADE")
    strat_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO strategies (id, slug, name, description, author) VALUES ($1, $2, $2, 'test', 'test')",
        strat_id, f"view-{strat_id.hex[:8]}",
    )
    await conn.execute(
        """INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema)
           VALUES ($1, $1, 1, 'h', 'c', '{}'::jsonb, '{}'::jsonb)""",
        strat_id,
    )
    await conn.execute(
        """INSERT INTO paper_accounts (account_id, venue, currency, starting_balance, cash_balance, total_equity,
                                       realized_pnl, daily_realized_pnl, daily_loss_latched, updated_at)
           VALUES ($1, 'BINANCE', 'USDT', 10000, 10000, 10000, 0, 0, FALSE, now())""",
        ACCOUNT,
    )
    await conn.execute(
        """INSERT INTO paper_strategy_instances (instance_id, account_id, strategy_version_id, strategy_source_hash,
               risk_policy_hash, strategy_name, instrument_id, timeframe, resolved_parameters, allocation_usd, status)
           VALUES ($1, $2, $3, 'h', 'h', 'view-test', 'BTCUSDT.BINANCE', '1m', '{}'::jsonb, 1000, 'RUNNING')""",
        INSTANCE, ACCOUNT, strat_id,
    )
    rows = [
        # position_id, instrument, side, entry, qty, current, upnl, status, leverage, margin_mode
        ("P-LONG", "BTCUSDT.BINANCE", "LONG", 100, 10, 110, 100, "OPEN", 1, None),
        ("P-SHORT", "ETHUSDT-PERP.BINANCE", "SHORT", 50, 4, 45, 20, "OPEN", 5, "CROSS"),
        ("P-CLOSED", "SOLUSDT.BINANCE", "FLAT", 20, 0, 20, 0, "CLOSED", 1, None),
    ]
    for pid, inst, side, entry, qty, cur, upnl, status, lev, mode in rows:
        await conn.execute(
            """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
                   quantity, current_price, unrealized_pnl, status, leverage, margin_mode, closed_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)""",
            pid, ACCOUNT, INSTANCE, inst, side, entry, qty, cur, upnl, status, lev, mode,
            datetime.now(UTC) if status == "CLOSED" else None,
        )
    yield conn
    await conn.execute("TRUNCATE paper_positions, paper_strategy_instances, paper_accounts CASCADE")
    await conn.close()


async def test_view_exposes_only_open_positions(conn):
    rows = await conn.fetch("SELECT instrument_id FROM agent_safe_positions ORDER BY instrument_id")
    assert [r["instrument_id"] for r in rows] == ["BTCUSDT.BINANCE", "ETHUSDT-PERP.BINANCE"]


async def test_size_is_signed_and_percentages_are_0_to_100(conn):
    rows = {r["instrument_id"]: r for r in await conn.fetch("SELECT * FROM agent_safe_positions")}
    long, short = rows["BTCUSDT.BINANCE"], rows["ETHUSDT-PERP.BINANCE"]

    assert long["size"] == 10 and short["size"] == -4
    # uPnL / (entry * qty): 100 / 1000 = 10%, 20 / 200 = 10%
    assert float(long["unrealized_pnl_pct"]) == pytest.approx(10.0)
    assert float(short["unrealized_pnl_pct"]) == pytest.approx(10.0)
    # qty * current / equity: 10 * 110 / 10000 = 11%, 4 * 45 / 10000 = 1.8%
    assert float(long["weight_pct"]) == pytest.approx(11.0)
    assert float(short["weight_pct"]) == pytest.approx(1.8)
    assert short["leverage"] == 5 and short["margin_mode"] == "CROSS"


async def test_columns_read_by_agent_consumers_exist(conn):
    # debate_worker: SELECT * ... WHERE size != 0 ; exit_monitor: instrument_id, unrealized_pnl_pct, created_at
    await conn.fetch("SELECT * FROM agent_safe_positions WHERE size != 0")
    await conn.fetch("SELECT instrument_id, unrealized_pnl_pct, created_at FROM agent_safe_positions")


async def test_agent_role_can_read_view(conn):
    assert await conn.fetchval("SELECT has_table_privilege('cryptonite_agent_ro', 'agent_safe_positions', 'SELECT')")
