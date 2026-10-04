"""Throwaway Postgres database migrated to Alembic head, for tests needing the latest schema.

The shared dev DB (DATABASE_URL) is not guaranteed to be at head, so these tests create their
own database, run `alembic upgrade head` against it, and drop it afterwards.
"""

import asyncio
import contextlib
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from connections import DB_URL

REPO_ROOT = Path(__file__).resolve().parents[1]


async def _admin(sql: str) -> None:
    conn = await asyncpg.connect(DB_URL)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@contextlib.contextmanager
def migrated_database(prefix: str = "cryptonite_test") -> Iterator[str]:
    """Yield the URL of a fresh database at Alembic head; drop it on exit."""
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    url = urlunsplit(urlsplit(DB_URL)._replace(path=f"/{name}"))
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", "upgrade", "head"],
            cwd=REPO_ROOT, env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True,
        )
        yield url
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


async def seed_paper_account(conn, account_id: str = "PAPER-ACCOUNT-001", cash: int = 100000) -> None:
    """A paper account with the system instances (MANUAL_TRADE, AI_DEBATE_WORKER) on a fresh database."""
    strat_id = uuid.UUID(int=0xA15)
    await conn.execute(
        "INSERT INTO strategies (id, slug, name, description, author) VALUES ($1, 'system-baseline', 'SystemBaseline', 'system', 'system') "
        "ON CONFLICT DO NOTHING", strat_id)
    await conn.execute(
        """INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema)
           VALUES ($1, $1, 1, 'h', 'c', '{}'::jsonb, '{}'::jsonb) ON CONFLICT DO NOTHING""", strat_id)
    await conn.execute(
        """INSERT INTO paper_accounts (account_id, venue, currency, starting_balance, cash_balance, total_equity,
                                       realized_pnl, daily_realized_pnl, daily_loss_latched, updated_at)
           VALUES ($1, 'BINANCE', 'USDT', $2, $2, $2, 0, 0, FALSE, now()) ON CONFLICT DO NOTHING""", account_id, cash)
    for iid in ("MANUAL_TRADE", "AI_DEBATE_WORKER"):
        await conn.execute(
            """INSERT INTO paper_strategy_instances (instance_id, account_id, strategy_version_id, strategy_source_hash,
                   risk_policy_hash, strategy_name, instrument_id, timeframe, resolved_parameters, allocation_usd, status)
               VALUES ($1::varchar, $2, $3, 'system', 'system', $1::varchar, 'SYSTEM.' || $1::varchar, '1m', '{}'::jsonb, 0, 'SYSTEM')
               ON CONFLICT DO NOTHING""", iid, account_id, strat_id)
