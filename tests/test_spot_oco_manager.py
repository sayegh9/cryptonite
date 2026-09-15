"""Tests for SpotOcoManager."""

import asyncio
from decimal import Decimal
from unittest.mock import MagicMock
import pytest
from cryptonite_execution.oco import SpotOcoManager


@pytest.mark.asyncio
async def test_oco_first_fill_timer_and_counterpart_cancellation():
    """Verify that first fill starts 15s protection timer and triggers counterpart cancel."""
    cancelled_orders = []

    def mock_cancel(order_id: str):
        cancelled_orders.append(order_id)

    mgr = SpotOcoManager(cancel_order_fn=mock_cancel)
    # Set short timeout for fast test execution
    mgr.PROTECTION_TIMEOUT_SECS = 0.05

    # Register OCO
    order_list_id = 12345
    limit_order_id = "LIMIT-ORD-01"
    stop_order_id = "STOP-ORD-02"

    mgr.register_oco(
        order_list_id=order_list_id,
        list_client_order_id="LIST-01",
        instrument_id="BTCUSDT.BINANCE",
        limit_order_id=limit_order_id,
        stop_order_id=stop_order_id,
        quantity=Decimal("0.05"),
        limit_price=Decimal("60000.00"),
        stop_price=Decimal("48000.00"),
    )

    # 1. First partial fill on limit leg (0.02 BTC)
    res = mgr.on_fill(
        client_order_id=limit_order_id,
        fill_qty=Decimal("0.02"),
        price=Decimal("60000.00"),
        commission=Decimal("0.00002"),
        commission_asset="BTC",
        base_asset="BTC",
    )
    assert res["is_oco"] is True
    assert res["timer_started"] is True
    # Net base post-commission: 0.02 - 0.00002 = 0.01998 BTC
    assert res["net_base"] == Decimal("0.01998")

    # 2. Wait for protection timer to fire
    await asyncio.sleep(0.08)

    # 3. Counterpart order (stop_order_id) should have been cancelled
    assert stop_order_id in cancelled_orders


@pytest.mark.asyncio
async def test_oco_safe_pre_flattening_cancellation():
    """Verify safe cancellation of open OCO legs before market flattening."""
    cancelled = []

    def mock_cancel(order_id: str):
        cancelled.append(order_id)

    mgr = SpotOcoManager(cancel_order_fn=mock_cancel)
    mgr.register_oco(
        order_list_id=999,
        list_client_order_id="LIST-999",
        instrument_id="BTCUSDT.BINANCE",
        limit_order_id="LIMIT-999",
        stop_order_id="STOP-999",
        quantity=Decimal("0.10"),
        limit_price=Decimal("60000.00"),
        stop_price=Decimal("48000.00"),
    )

    # Pre-flattening cancellation
    await mgr.cancel_active_ocos_for_flatten()
    assert "LIMIT-999" in cancelled
    assert "STOP-999" in cancelled


def test_oco_dust_detection():
    """Verify dust checks on remaining balance."""
    mgr = SpotOcoManager(min_qty=Decimal("0.00001"), min_notional=Decimal("10.0"))
    # Quantity below minQty is dust
    assert mgr.is_dust(Decimal("0.000005"), Decimal("50000.00")) is True
    # Notional below minNotional is dust (0.0001 * 50,000 = $5 < $10)
    assert mgr.is_dust(Decimal("0.0001"), Decimal("50000.00")) is True
    # Valid tradeable size is not dust (0.001 * 50,000 = $50 > $10)
    assert mgr.is_dust(Decimal("0.001"), Decimal("50000.00")) is False


@pytest.mark.asyncio
async def test_entry_order_first_fill_timer_and_oco_installation_on_expiry():
    """Verify institutional OCO contract:
    1. First fill on entry order starts 15s protection timer.
    2. On timer expiration, cancels unfilled entry remainder.
    3. Reconciles exact attributed net base quantity post-commission.
    4. Installs protective OCO for exact net base quantity.
    """
    cancelled_orders = []
    submitted_ocos = []

    def mock_cancel(order_id: str):
        cancelled_orders.append(order_id)

    def mock_submit_oco(entry_order_id, instrument_id, quantity, take_profit_price, stop_price, stop_limit_price):
        submitted_ocos.append({
            "entry_order_id": entry_order_id,
            "instrument_id": instrument_id,
            "quantity": quantity,
            "take_profit_price": take_profit_price,
            "stop_price": stop_price,
            "stop_limit_price": stop_limit_price,
        })
        return 778899

    mgr = SpotOcoManager(
        cancel_order_fn=mock_cancel,
        submit_oco_fn=mock_submit_oco,
        min_qty=Decimal("0.00010"),
        min_notional=Decimal("5.00"),
    )
    mgr.PROTECTION_TIMEOUT_SECS = 0.05

    entry_id = "ENTRY-ORD-001"
    mgr.register_entry_order(
        client_order_id=entry_id,
        instrument_id="BTCUSDT.BINANCE",
        quantity=Decimal("0.10000"),
        take_profit_price=Decimal("65000.00"),
        stop_price=Decimal("55000.00"),
        stop_limit_price=Decimal("54900.00"),
    )

    # 1. First partial fill of 0.04 BTC, with 0.00004 BTC commission in base asset
    res1 = mgr.on_entry_fill(
        client_order_id=entry_id,
        fill_qty=Decimal("0.04000"),
        price=Decimal("60000.00"),
        commission=Decimal("0.00004"),
        commission_asset="BTC",
        base_asset="BTC",
    )
    assert res1["is_entry"] is True
    assert res1["timer_started"] is True
    assert res1["net_base"] == Decimal("0.03996")
    assert res1["total_net_base"] == Decimal("0.03996")
    assert res1["fully_filled"] is False

    # 2. Second partial fill before timer expires (0.02 BTC, commission in USDT)
    res2 = mgr.on_entry_fill(
        client_order_id=entry_id,
        fill_qty=Decimal("0.02000"),
        price=Decimal("60000.00"),
        commission=Decimal("1.20"),
        commission_asset="USDT",
        base_asset="BTC",
    )
    assert res2["is_entry"] is True
    assert res2["timer_started"] is False
    # Total net base: 0.03996 + 0.02000 = 0.05996 BTC
    assert res2["total_net_base"] == Decimal("0.05996")
    assert res2["fully_filled"] is False

    # 3. Wait for 15s protection timeout to fire
    await asyncio.sleep(0.08)

    # 4. Entry remainder must be cancelled
    assert entry_id in cancelled_orders

    # 5. Protective OCO must be installed for exact attributed net base
    assert len(submitted_ocos) == 1
    oco = submitted_ocos[0]
    assert oco["entry_order_id"] == entry_id
    assert oco["instrument_id"] == "BTCUSDT.BINANCE"
    assert oco["quantity"] == Decimal("0.05996")
    assert oco["take_profit_price"] == Decimal("65000.00")
    assert oco["stop_price"] == Decimal("55000.00")
    assert oco["stop_limit_price"] == Decimal("54900.00")


@pytest.mark.asyncio
async def test_entry_order_full_fill_immediate_oco_installation():
    """Verify that full fill triggers immediate OCO installation and cancels timer."""
    submitted_ocos = []

    def mock_submit_oco(entry_order_id, instrument_id, quantity, take_profit_price, stop_price, stop_limit_price):
        submitted_ocos.append({"quantity": quantity})
        return 999111

    mgr = SpotOcoManager(
        submit_oco_fn=mock_submit_oco,
    )
    mgr.PROTECTION_TIMEOUT_SECS = 5.0  # long timer to prove immediate installation

    entry_id = "ENTRY-ORD-FULL"
    mgr.register_entry_order(
        client_order_id=entry_id,
        instrument_id="BTCUSDT.BINANCE",
        quantity=Decimal("0.05"),
        take_profit_price=Decimal("70000.00"),
        stop_price=Decimal("50000.00"),
    )

    # Fill full 0.05 BTC in one fill
    res = mgr.on_fill(
        client_order_id=entry_id,
        fill_qty=Decimal("0.05"),
        price=Decimal("60000.00"),
        commission=Decimal("0.00005"),
        commission_asset="BTC",
        base_asset="BTC",
    )
    assert res["is_entry"] is True
    assert res["fully_filled"] is True

    # Allow create_task to run
    await asyncio.sleep(0.01)

    assert len(submitted_ocos) == 1
    assert submitted_ocos[0]["quantity"] == Decimal("0.04995")


@pytest.mark.asyncio
async def test_entry_order_dust_fill_skips_oco():
    """Verify that if net base acquired is below dust threshold, OCO is not installed."""
    submitted_ocos = []
    cancelled = []

    mgr = SpotOcoManager(
        cancel_order_fn=lambda oid: cancelled.append(oid),
        submit_oco_fn=lambda **kwargs: submitted_ocos.append(kwargs),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("100.0"),
    )
    mgr.PROTECTION_TIMEOUT_SECS = 0.02

    entry_id = "ENTRY-ORD-DUST"
    mgr.register_entry_order(
        client_order_id=entry_id,
        instrument_id="BTCUSDT.BINANCE",
        quantity=Decimal("0.10"),
        take_profit_price=Decimal("70000.00"),
        stop_price=Decimal("50000.00"),
    )

    # Tiny fill below minQty (0.0001 BTC < 0.001 BTC)
    mgr.on_fill(
        client_order_id=entry_id,
        fill_qty=Decimal("0.0001"),
        price=Decimal("60000.00"),
        commission=Decimal("0"),
        commission_asset="USDT",
        base_asset="BTC",
    )

    await asyncio.sleep(0.05)
    assert entry_id in cancelled
    assert len(submitted_ocos) == 0, "Dust fill must not trigger OCO submission"

