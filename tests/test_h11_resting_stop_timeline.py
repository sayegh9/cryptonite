"""H11 (migration 062): a position closed by its resting stop gets its exit price and close reason RESTING_STOP in
``agent_safe_trade_timeline``. Before, such a close had no closing command, so both were NULL (BTC perp short,
2026-11-03 13:29), and the learner's lesson complained about the missing exit."""

from datetime import timedelta
from decimal import Decimal

import test_agent_safe_trade_timeline as tl
from test_agent_safe_trade_timeline import (
    ACCOUNT,
    NOW,
    XRP,
    account,
    command,
    instance,
    mandate,
    mandate_instance,
    order,
    position,
    row,
)

db_url = tl.db_url  # the timeline suite's fixtures: one throwaway DB at head, a rolled-back transaction per test
conn = tl.conn


async def stop_order(conn, instance_id, client_order_id, side, created_at, qty, price, status="FILLED",
                     filled_at=None):
    """The resting stop's paper order: written when the stop is placed, filled (same row, ``updated_at``) when it
    triggers."""
    await conn.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
               quantity, filled_qty, avg_fill_price, status, created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, 'STOP_MARKET', $6, $7, $8, $9, $10, $11)""",
        client_order_id, instance_id, ACCOUNT, XRP, side, qty, qty if status == "FILLED" else 0,
        Decimal(str(price)) if status == "FILLED" else None, status, created_at, filled_at or created_at,
    )


async def stopped_trade(conn, *, stop_status="FILLED"):
    await account(conn)
    mid = await mandate(conn, "stop")
    target = mandate_instance(mid)
    await instance(conn, target)
    opened = NOW - timedelta(days=1)
    entry = await command(conn, "SUBMIT_ORDER", target, {"instrument_id": XRP, "side": "BUY"}, "operator:tester",
                          opened - timedelta(seconds=5), mid=mid)
    await order(conn, target, f"AI-{entry.hex[:12].upper()}", XRP, "BUY", opened - timedelta(seconds=4), 10, 0.5)
    # the stop row is written with the entry fill, a moment before the position row's opened_at
    closed = opened + timedelta(hours=15) if stop_status == "FILLED" else None
    await stop_order(conn, target, "STOP-H11TEST0001", "SELL", opened - timedelta(milliseconds=100), 10, 0.47,
                     status=stop_status, filled_at=closed)
    return await position(conn, target, opened, qty=10, entry=0.5, realized=-0.3, closed_at=closed)


async def test_a_resting_stop_close_has_its_exit_price_and_reason(conn):
    r = await row(conn, await stopped_trade(conn))
    assert r["exit_price"] == Decimal("0.47")
    assert r["close_reason"] == "RESTING_STOP" and r["close_command_id"] is None


async def test_an_open_position_with_a_resting_stop_has_no_exit(conn):
    r = await row(conn, await stopped_trade(conn, stop_status="ACCEPTED"))
    assert r["exit_price"] is None and r["close_reason"] is None


async def test_an_earlier_positions_stop_never_closes_the_next_one(conn):
    """Position 1 stopped out; position 2 opened within the minute: position 2 does not take position 1's stop."""
    await account(conn)
    mid = await mandate(conn, "two")
    target = mandate_instance(mid)
    await instance(conn, target)
    first_open = NOW - timedelta(hours=3)
    first_close = NOW - timedelta(hours=2, seconds=30)
    await stop_order(conn, target, "STOP-H11TEST0004", "SELL", first_open - timedelta(milliseconds=100), 10, 0.45,
                     filled_at=first_close)
    await position(conn, target, first_open, qty=10, entry=0.5, realized=-0.5, closed_at=first_close)
    second = await position(conn, target, first_close + timedelta(seconds=20), qty=10, entry=0.46, realized=0.2,
                            closed_at=NOW - timedelta(hours=1))
    r = await row(conn, second)
    assert r["close_reason"] is None and r["exit_price"] is None


async def test_a_command_close_still_wins_over_the_resting_stop(conn):
    await account(conn)
    mid = await mandate(conn, "cmd")
    target = mandate_instance(mid)
    await instance(conn, target)
    opened = NOW - timedelta(days=2)
    await stop_order(conn, target, "STOP-H11TEST0002", "SELL", opened - timedelta(milliseconds=100), 10, 0.4,
                     status="ACCEPTED")
    pid = await position(conn, target, opened, qty=10, entry=0.5, realized=1, closed_at=opened + timedelta(hours=5))
    await command(conn, "CLOSE_POSITION", target, {"instrument_id": XRP, "reason": "TRAILING_STOP"},
                  "risk:exit_monitor", opened + timedelta(hours=4), mid=mid)
    await order(conn, target, "CLOSE-H11T0003", XRP, "SELL", opened + timedelta(hours=4, seconds=1), 10, 0.6)
    r = await row(conn, pid)
    assert (r["close_reason"], r["exit_price"]) == ("TRAILING_STOP", Decimal("0.6"))


def test_062_downgrades_and_upgrades_the_view_cleanly():
    import asyncio
    import os
    import subprocess
    import sys

    import asyncpg
    from migrated_db import REPO_ROOT, migrated_database

    def alembic(url, *args):
        subprocess.run([sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", *args], cwd=REPO_ROOT,
                       env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True)

    async def viewdef(url):
        c = await asyncpg.connect(url)
        try:
            return await c.fetchval("SELECT pg_get_viewdef('agent_safe_trade_timeline'::regclass)")
        finally:
            await c.close()

    with migrated_database("cryptonite_h11mig") as url:
        assert "RESTING_STOP" in asyncio.run(viewdef(url))
        alembic(url, "downgrade", "061")
        assert "RESTING_STOP" not in (old := asyncio.run(viewdef(url))) and "order_type" not in old
        alembic(url, "upgrade", "head")
        assert "RESTING_STOP" in asyncio.run(viewdef(url))
