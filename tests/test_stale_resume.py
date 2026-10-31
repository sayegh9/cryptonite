"""Operator decision 2026-10-31: a stale-data breaker pause lifts itself once the feed has been fresh for 5 minutes,
at most 3 times per UTC day; an operator pause and other breakers are never undone."""
import datetime as dt

import asyncpg
import pytest
from cryptonite_execution import profile_lifecycle
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_orchestrator import mandate_monitor as monitor
from cryptonite_schemas.mandate import MandateStatus
from migrated_db import migrated_database, seed_paper_account
from test_mandate_hard_stop import make_mandate

T0 = dt.datetime(2026, 10, 31, 9, 0, tzinfo=dt.UTC)
BTC = "BTCUSDT.BINANCE"


class Redis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key):
        self.values.pop(key, None)

    async def publish(self, *args):
        return 0


def tick(at: dt.datetime, age_s: float = 1) -> dict:
    return {BTC: {"arrival_ts_ns": int((at.timestamp() - age_s) * 1e9)}}


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_stale_resume") as url:
        yield url


async def status(conn, mid) -> MandateStatus:
    return (await load_mandate(conn, mid)).status


async def stale_pause(conn, redis, mid, at):
    await monitor.track_profile_breakers(conn, redis, await load_mandate(conn, mid), at, tick(at, 601))
    assert await status(conn, mid) is MandateStatus.PAUSED


async def test_stale_pause_resumes_after_five_fresh_minutes_three_times_a_day(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await seed_paper_account(conn)
            # the start's capital cap compares running budgets with account equity
            await conn.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
            await conn.execute("UPDATE paper_accounts SET total_equity = 1000000, cash_balance = 1000000")
        mid = (await make_mandate(pool, 1)).id
        redis = Redis()
        async with pool.acquire() as conn:
            for n in range(1, 4):
                at = T0 + dt.timedelta(hours=n)
                await stale_pause(conn, redis, mid, at)
                await monitor.auto_resume_stale_pause(conn, redis, await load_mandate(conn, mid), at, tick(at))
                # a stale tick inside the window restarts the 5 minutes
                mid_window = at + dt.timedelta(seconds=200)
                await monitor.auto_resume_stale_pause(conn, redis, await load_mandate(conn, mid), mid_window,
                                                      tick(mid_window, 200))
                late = at + dt.timedelta(seconds=301)
                await monitor.auto_resume_stale_pause(conn, redis, await load_mandate(conn, mid), late, tick(late))
                assert await status(conn, mid) is MandateStatus.PAUSED  # the timer restarted at `late`
                done = late + dt.timedelta(seconds=301)
                await monitor.auto_resume_stale_pause(conn, redis, await load_mandate(conn, mid), done, tick(done))
                assert await status(conn, mid) is MandateStatus.ACTIVE, n
            assert await conn.fetchval(
                "SELECT count(*) FROM notifications_outbox WHERE kind = 'PROFILE_BREAKER_RESUMED' "
                "AND payload->>'mandate_id' = $1", str(mid)) == 3
            # the fourth stale pause of the day stays for the operator
            at = T0 + dt.timedelta(hours=5)
            await stale_pause(conn, redis, mid, at)
            for s in (0, 301, 700):
                when = at + dt.timedelta(seconds=s)
                await monitor.auto_resume_stale_pause(conn, redis, await load_mandate(conn, mid), when, tick(when))
            assert await status(conn, mid) is MandateStatus.PAUSED
    finally:
        await pool.close()


async def test_an_operator_pause_after_the_breaker_is_never_undone(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await seed_paper_account(conn)
            # the start's capital cap compares running budgets with account equity
            await conn.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
            await conn.execute("UPDATE paper_accounts SET total_equity = 1000000, cash_balance = 1000000")
        mid = (await make_mandate(pool, 1)).id
        redis = Redis()
        async with pool.acquire() as conn:
            await stale_pause(conn, redis, mid, T0)
            # the operator resumes, then pauses by hand while the old stale reason is still on the row
            await profile_lifecycle.start(conn, mid, actor="admin", totp_ok=True, settings=None)
            await conn.execute("UPDATE ai_mandates SET paused_reason = 'BREAKER_STALE_DATA: old' WHERE id = $1", mid)
            await profile_lifecycle.pause(conn, mid, actor="admin", reason="BREAKER_STALE_DATA: operator copy")
            for s in (0, 301):
                when = T0 + dt.timedelta(seconds=s)
                await monitor.auto_resume_stale_pause(conn, redis, await load_mandate(conn, mid), when, tick(when))
            assert await status(conn, mid) is MandateStatus.PAUSED
    finally:
        await pool.close()
