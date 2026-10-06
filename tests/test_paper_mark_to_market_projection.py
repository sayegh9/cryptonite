"""Test CoalescedMarkToMarketProjector throttled updates and unrealized PnL calculation."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptonite_execution.bridge import CoalescedMarkToMarketProjector


@pytest.mark.asyncio
async def test_mark_to_market_projection_calculation():
    """Each open position is marked at its live price by one guarded UPDATE (the PnL is computed in SQL
    from the row as it is then); the touched account is revalued once; marks go to ``on_marked``."""
    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {"position_id": "POS-001", "account_id": "ACC", "instrument_id": "BTCUSDT.BINANCE"},
        {"position_id": "POS-002", "account_id": "ACC", "instrument_id": "ETHUSDT.BINANCE"},
        {"position_id": "POS-003", "account_id": "ACC", "instrument_id": "SOLUSDT.BINANCE"},  # no fresh quote
    ]
    mock_conn.fetchval.return_value = "ACC"
    mock_conn.fetchrow.return_value = {"pos_val": Decimal("1000")}
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    prices = {"BTCUSDT.BINANCE": (Decimal("52000.00"), 111), "ETHUSDT.BINANCE": Decimal("2900.00")}
    published = {}

    async def on_marked(marked):
        published.update(marked)

    projector = CoalescedMarkToMarketProjector(mock_pool, prices.get, interval_s=0.05, on_marked=on_marked)
    assert await projector.project_once() == 2

    updates = mock_conn.fetchval.call_args_list
    assert len(updates) == 2
    assert "status = 'OPEN'" in updates[0][0][0] and "IS DISTINCT FROM" in updates[0][0][0]
    assert updates[0][0][1:] == ("POS-001", Decimal("52000.00"))
    assert updates[1][0][1:] == ("POS-002", Decimal("2900.00"))
    assert mock_conn.execute.call_count == 1  # paper_accounts revalued once (total_equity, updated_at)
    assert "updated_at = now()" in mock_conn.execute.call_args[0][0]
    assert published["BTCUSDT.BINANCE"] == (Decimal("52000.00"), 111)
    assert "SOLUSDT.BINANCE" not in published


@pytest.mark.asyncio
async def test_mark_to_market_projector_loop_lifecycle():
    """Verify projector loop starts, runs periodically, and cancels cleanly."""
    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = []
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    projector = CoalescedMarkToMarketProjector(
        pg_pool=mock_pool,
        get_latest_price_fn=lambda x: Decimal("50000"),
        interval_s=0.05,
    )

    await projector.start()
    assert projector._running is True
    await asyncio.sleep(0.15)
    await projector.stop()
    assert projector._running is False
    assert mock_conn.fetch.call_count >= 2
