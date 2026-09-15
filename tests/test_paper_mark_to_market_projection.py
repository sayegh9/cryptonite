"""Test CoalescedMarkToMarketProjector throttled updates and unrealized PnL calculation."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptonite_execution.bridge import CoalescedMarkToMarketProjector


@pytest.mark.asyncio
async def test_mark_to_market_projection_calculation():
    """Verify mark-to-market pricing calculates unrealized PnL accurately."""
    mock_pool = MagicMock()
    mock_conn = AsyncMock()

    # Simulate 2 open positions
    # Pos 1: Entry 50,000, Qty 0.5 BTC
    # Pos 2: Entry 3,000, Qty 2.0 ETH
    mock_positions = [
        {"position_id": "POS-001", "instrument_id": "BTCUSDT.BINANCE", "entry_price": Decimal("50000.00"), "quantity": Decimal("0.5")},
        {"position_id": "POS-002", "instrument_id": "ETHUSDT.BINANCE", "entry_price": Decimal("3000.00"), "quantity": Decimal("2.0")},
    ]
    mock_conn.fetch.return_value = mock_positions
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    # Price provider: BTC is now 52,000 (+2,000 -> PnL +1,000), ETH is now 2,900 (-100 -> PnL -200)
    prices = {
        "BTCUSDT.BINANCE": Decimal("52000.00"),
        "ETHUSDT.BINANCE": Decimal("2900.00"),
    }
    get_price_fn = lambda inst: prices.get(inst)

    projector = CoalescedMarkToMarketProjector(
        pg_pool=mock_pool,
        get_latest_price_fn=get_price_fn,
        interval_s=0.05,
    )

    await projector.project_once()

    # Verify 2 updates executed
    assert mock_conn.execute.call_count == 2
    calls = mock_conn.execute.call_args_list

    # Call 1 (BTC): current_price = 52000, unrealized_pnl = (52000 - 50000) * 0.5 = 1000
    call_1_args = calls[0][0]
    assert call_1_args[1] == "POS-001"
    assert call_1_args[2] == Decimal("52000.00")
    assert call_1_args[3] == Decimal("1000.000")

    # Call 2 (ETH): current_price = 2900, unrealized_pnl = (2900 - 3000) * 2.0 = -200
    call_2_args = calls[1][0]
    assert call_2_args[1] == "POS-002"
    assert call_2_args[2] == Decimal("2900.00")
    assert call_2_args[3] == Decimal("-200.00")


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
