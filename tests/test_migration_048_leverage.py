"""Migration 048 (ADR 0009, leverage plan 2.1): the 10x leverage ceiling, max_risk_per_trade_pct (default 1, at most
2), perps only at L1; the downgrade refuses while a mandate has max_leverage above 5 or perps below L3."""

import asyncio
import os
import subprocess
import sys
import uuid

import asyncpg
import pytest
from migrated_db import REPO_ROOT, migrated_database


def _alembic(url, *args, check=True):
    env = {**os.environ, "DATABASE_URL": url}
    return subprocess.run([sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", *args],
                          cwd=REPO_ROOT, env=env, check=check, capture_output=True, text=True)


async def _insert(url, **cols):
    base = {"name": f"m-{uuid.uuid4().hex[:6]}", "capital_budget_usd": 10000, "instruments": ["BTCUSDT-PERP.BINANCE"],
            "max_position_pct": 25, "max_drawdown_pct": 20, "allow_perps": False, "max_leverage": 1,
            "autonomy_level": 1, "created_by": "test"}
    base.update(cols)
    c = await asyncpg.connect(url)
    try:
        keys = list(base)
        await c.execute(f"INSERT INTO ai_mandates (id, {', '.join(keys)}) VALUES (gen_random_uuid(), "
                        f"{', '.join(f'${i + 1}' for i in range(len(keys)))})", *base.values())
    finally:
        await c.close()


async def _fetchval(url, sql):
    c = await asyncpg.connect(url)
    try:
        return await c.fetchval(sql)
    finally:
        await c.close()


def test_048_ceilings_and_refused_downgrade():
    with migrated_database("cryptonite_m048") as url:
        asyncio.run(_insert(url, allow_perps=True, max_leverage=10, max_risk_per_trade_pct=2))
        assert asyncio.run(_fetchval(url, "SELECT max_risk_per_trade_pct FROM ai_mandates")) == 2
        # ADR 0011 D4: migration 050 dropped chk_ai_mandates_perps_level, so perps at L2/L3 are no longer refused
        # (and are covered by test_migration_050); the other 048 ceilings stand
        for bad in ({"max_leverage": 11}, {"max_risk_per_trade_pct": 2.5}, {"max_risk_per_trade_pct": 0}):
            with pytest.raises(asyncpg.CheckViolationError):
                asyncio.run(_insert(url, **bad))
        asyncio.run(_insert(url, name="spot-default", instruments=["ETHUSDT.BINANCE"]))  # own coin: 050 locks one owner per coin
        assert asyncio.run(_fetchval(url, "SELECT max_risk_per_trade_pct FROM ai_mandates WHERE name = 'spot-default'")) == 1

        down = _alembic(url, "downgrade", "047", check=False)
        assert down.returncode != 0 and "max_leverage above 5" in (down.stderr + down.stdout)
        asyncio.run(_fetchval(url, "UPDATE ai_mandates SET max_leverage = 5 WHERE allow_perps RETURNING 1"))
        down = _alembic(url, "downgrade", "047", check=False)
        assert down.returncode != 0 and "perps enabled below L3" in (down.stderr + down.stdout)
        asyncio.run(_fetchval(url, "UPDATE ai_mandates SET allow_perps = false, markets = ARRAY['SPOT'], max_leverage = 1 RETURNING 1"))
        _alembic(url, "downgrade", "047")
        assert asyncio.run(_fetchval(url, "SELECT count(*) FROM information_schema.columns "
                                          "WHERE table_name = 'ai_mandates' AND column_name = 'max_risk_per_trade_pct'")) == 0
        with pytest.raises(asyncpg.CheckViolationError):  # the old 5x ceiling is back
            asyncio.run(_insert(url, max_leverage=6))
        _alembic(url, "upgrade", "head")
        asyncio.run(_insert(url, allow_perps=True, max_leverage=7))


def test_048_upgrade_refuses_perps_away_from_l1():
    with migrated_database("cryptonite_m048up") as url:
        _alembic(url, "downgrade", "047")
        asyncio.run(_insert(url, allow_perps=True, max_leverage=5, autonomy_level=3))  # legal before 048 (O1)
        up = _alembic(url, "upgrade", "head", check=False)
        assert up.returncode != 0 and "perps enabled away from L1" in (up.stderr + up.stdout)
