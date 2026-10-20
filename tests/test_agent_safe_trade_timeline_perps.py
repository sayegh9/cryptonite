"""Migration 048 (leverage plan 3.3): agent_safe_trade_timeline carries the entry's setup grade, the model's
confidence and the leverage, and reads a perp short both ways (a SELL entry, a reduce-only BUY close)."""

import json
import uuid
from datetime import timedelta
from decimal import Decimal

from test_agent_safe_trade_timeline import (  # noqa: F401 - conn and db_url are fixtures
    ACCOUNT,
    NOW,
    account,
    command,
    conn,
    db_url,
    instance,
    mandate,
    mandate_instance,
    order,
    row,
)

PERP = "XRPUSDT-PERP.BINANCE"


async def perp_position(conn, instance_id, opened_at, closed_at, *, realized, leverage):  # noqa: F811
    pid = f"tlp-{uuid.uuid4().hex[:24]}"
    await conn.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price, quantity,
               current_price, unrealized_pnl, realized_pnl, status, opened_at, closed_at, leverage, margin_mode,
               isolated_margin)
           VALUES ($1, $2, $3, $4, 'FLAT', 0.5, 0, 0.5, 0, $5, 'CLOSED', $6, $7, $8, 'ISOLATED', 0)""",
        pid, ACCOUNT, instance_id, PERP, Decimal(str(realized)), opened_at, closed_at, leverage)
    return pid


async def test_a_perp_short_with_its_grade_confidence_and_leverage(conn):  # noqa: F811
    await account(conn)
    mid = await mandate(conn, "perp")
    target = mandate_instance(mid)
    await instance(conn, target)
    opened = NOW - timedelta(days=1)
    entry = await command(conn, "SUBMIT_ORDER", target,
                          {"instrument_id": PERP, "side": "SELL", "setup_grade": "A", "confidence": 0.72,
                           "source": "brain_order_card"}, "approval:web:op", opened - timedelta(minutes=1), mid=mid)
    await order(conn, target, f"AI-{entry.hex[:12].upper()}", PERP, "SELL", opened, 10, 0.5)
    closed = opened + timedelta(hours=4)
    close = await command(conn, "SUBMIT_ORDER", target, {"instrument_id": PERP, "side": "BUY", "reduce_only": True},
                          "approval:web:op", closed - timedelta(seconds=5), mid=mid)
    await order(conn, target, f"AI-{close.hex[:12].upper()}", PERP, "BUY", closed - timedelta(seconds=4), 10, 0.45)
    pid = await perp_position(conn, target, opened, closed, realized="0.5", leverage=5)

    r = await row(conn, pid)
    assert r["side"] == "SHORT"  # from the SELL entry: a perp short
    assert r["entry_command_id"] == entry
    assert (r["entry_setup_grade"], r["entry_confidence"], r["entry_leverage"]) == ("A", Decimal("0.72"), 5)
    assert r["close_command_id"] == close and r["exit_price"] == Decimal("0.45")  # the BUY fill closes a short
    assert r["return_pct"] == Decimal("10")  # 0.5 / (0.5 x 10) x 100


async def test_a_spot_entry_without_a_grade_reads_null(conn):  # noqa: F811
    await account(conn)
    mid = await mandate(conn, "spot")
    target = mandate_instance(mid)
    await instance(conn, target)
    opened = NOW - timedelta(days=1)
    await command(conn, "SUBMIT_ORDER", target, {"instrument_id": "XRPUSDT.BINANCE", "side": "BUY",
                                                 "confidence": "not a number"}, "approval:web:op",
                  opened - timedelta(minutes=1), mid=mid)
    pid = f"tls-{uuid.uuid4().hex[:24]}"
    await conn.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price, quantity,
               current_price, unrealized_pnl, realized_pnl, status, opened_at)
           VALUES ($1, $2, $3, 'XRPUSDT.BINANCE', 'LONG', 0.5, 10, 0.5, 0, 0, 'OPEN', $4)""",
        pid, ACCOUNT, target, opened)
    r = await row(conn, pid)
    assert (r["side"], r["entry_setup_grade"], r["entry_confidence"], r["entry_leverage"]) == ("LONG", None, None, 1)
    assert json.dumps(dict(r), default=str)  # every column is plain data
