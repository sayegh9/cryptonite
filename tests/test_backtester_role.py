"""LE-5: the backtest worker's own database role has exactly what it uses and nothing else
(it used to connect as the database owner)."""

from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from migrated_db import migrated_database

ROLE, PASSWORD = "cryptonite_backtester", "backtester_dev_pass"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_bkroletest") as url:
        yield url


def as_role(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"{ROLE}:{PASSWORD}@{parts.netloc.rsplit('@', 1)[-1]}"))


@pytest.mark.parametrize("table, privilege, allowed", [
    ("backtest_runs", "SELECT", True), ("backtest_runs", "UPDATE", True), ("backtest_runs", "DELETE", False),
    ("backtest_results", "INSERT", True), ("outbox_events", "INSERT", True), ("strategy_versions", "SELECT", True),
    ("strategies", "UPDATE", False), ("paper_orders", "SELECT", False), ("execution_commands", "INSERT", False),
    ("ai_mandates", "SELECT", False), ("operator_users", "SELECT", False), ("machine_api_tokens", "SELECT", False),
])
async def test_role_privileges(db_url, table, privilege, allowed):
    conn = await asyncpg.connect(db_url)
    try:
        assert await conn.fetchval("SELECT has_table_privilege($1, $2, $3)", ROLE, table, privilege) is allowed
    finally:
        await conn.close()


async def test_the_role_can_claim_a_run_but_not_read_trading_data(db_url):
    owner = await asyncpg.connect(db_url)
    worker = await asyncpg.connect(as_role(db_url))
    try:
        strategy = await owner.fetchval("INSERT INTO strategies (name, slug, description, author) "
                                        "VALUES ('r', 'role-test', 'd', 'a') RETURNING id")
        version = await owner.fetchval(
            "INSERT INTO strategy_versions (strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema) "
            "VALUES ($1, 1, 'h', 'c', '{}'::jsonb, '{}'::jsonb) RETURNING id", strategy)
        run = await owner.fetchval(
            "INSERT INTO backtest_runs (strategy_version_id, instrument_id, bar_type, timeframe, parameters, "
            "warmup_start_ns, evaluation_start_ns, evaluation_end_ns, execution_profile, run_config_hash, status) "
            "VALUES ($1, 'BTCUSDT.BINANCE', 'b', '1m', '{}'::jsonb, 0, 1, 2, 'optimistic', 'h', 'PENDING') RETURNING id", version)
        claimed = await worker.fetchval(
            "UPDATE backtest_runs SET status = 'RUNNING' WHERE id = $1 AND status = 'PENDING' RETURNING id", run)
        assert claimed == run
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await worker.fetch("SELECT * FROM paper_orders")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await worker.execute("DELETE FROM backtest_runs WHERE id = $1", run)
    finally:
        await worker.close()
        await owner.close()
