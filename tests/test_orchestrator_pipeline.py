"""Tests for live strategy orchestrator pipeline, strategy patching, and command execution."""

import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptonite_execution.engine import PaperTradingEngine
from cryptonite_execution.risk import OrderIntent
from cryptonite_orchestrator.server import _patch_strategy_for_paper_engine, command_poller
from nautilus_trader.model import Price, Quantity


class DummyNautilusOrder:
    def __init__(self, client_order_id: str, instrument_id: str, side_val: int, qty: float, price: float | None = None):
        self.client_order_id = MagicMock(value=client_order_id)
        self.instrument_id = instrument_id
        self.side = MagicMock(value=side_val)  # 1 for BUY, 2 for SELL
        self.order_type = MagicMock(value=1)   # 1 for MARKET
        self.quantity = MagicMock(as_double=lambda: qty)
        self.price = MagicMock(as_double=lambda: price) if price else None


class DummyStrategy:
    def __init__(self, instrument_id: str, trade_size: float = 0.05):
        self.instrument_id = instrument_id
        self.trade_size = trade_size

    def submit_order(self, order):
        pass

    def buy_market(self, qty=None):
        pass

    def sell_market(self, qty=None):
        pass


def test_patch_strategy_submit_order_routes_to_engine_dispatcher():
    """Verify monkey-patched strategy routes submit_order to engine.dispatcher.dispatch."""
    engine = MagicMock(spec=PaperTradingEngine)
    engine.dispatcher = MagicMock()
    strategy = DummyStrategy(instrument_id="BTCUSDT.BINANCE", trade_size=0.1)

    patched_strat = _patch_strategy_for_paper_engine(strategy, engine, instance_id="INST-001")
    naut_order = DummyNautilusOrder("ORD-123", "BTCUSDT.BINANCE", side_val=1, qty=0.05)

    patched_strat.submit_order(naut_order)

    engine.dispatcher.dispatch.assert_called_once()
    intent, on_submit = engine.dispatcher.dispatch.call_args[0][:2]
    assert isinstance(intent, OrderIntent)
    assert intent.client_order_id == "ORD-123"
    assert intent.instance_id == "INST-001"
    assert intent.instrument_id == "BTCUSDT.BINANCE"
    assert intent.side == "BUY"
    assert intent.order_type == "MARKET"
    assert intent.quantity == Decimal("0.05")


def test_patch_strategy_buy_and_sell_market_helpers():
    """Verify monkey-patched strategy handles buy_market and sell_market helpers without a live Nautilus node."""
    engine = MagicMock(spec=PaperTradingEngine)
    engine.dispatcher = MagicMock()
    strategy = DummyStrategy(instrument_id="ETHUSDT.BINANCE", trade_size=0.25)

    patched_strat = _patch_strategy_for_paper_engine(strategy, engine, instance_id="INST-002")

    # buy_market
    patched_strat.buy_market()
    assert engine.dispatcher.dispatch.call_count == 1
    intent = engine.dispatcher.dispatch.call_args[0][0]
    assert intent.side == "BUY"
    assert intent.instance_id == "INST-002"
    assert intent.instrument_id == "ETHUSDT.BINANCE"
    assert intent.quantity == Decimal("0.25")

    # sell_market with custom quantity
    qty_mock = MagicMock(as_double=lambda: 0.5)
    patched_strat.sell_market(qty=qty_mock)
    assert engine.dispatcher.dispatch.call_count == 2
    intent2 = engine.dispatcher.dispatch.call_args[0][0]
    assert intent2.side == "SELL"
    assert intent2.quantity == Decimal("0.5")


@pytest.mark.asyncio
async def test_command_poller_processes_deploy_command():
    """Command poller claims PENDING DEPLOY command and inserts into paper_strategy_instances."""
    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    cmd_id = uuid.uuid4()
    inst_id = "INST-DEPLOY-TEST"

    command_row = {
        "command_id": cmd_id,
        "command_type": "DEPLOY",
        "target_instance_id": inst_id,
        "payload": json.dumps({
            "account_id": "DEMO-ACCOUNT-001",
            "strategy_name": "EMACrossover",
            "strategy_version_id": str(uuid.uuid4()),
            "instrument_id": "BTCUSDT.BINANCE",
            "timeframe": "1m",
            "parameters": {"fast_period": 9, "slow_period": 21},
            "allocation_usd": 10000.0,
        }),
    }

    call_count = 0
    async def fake_fetchrow(query, *args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return command_row
        raise asyncio.CancelledError()

    mock_conn.fetchrow = fake_fetchrow
    mock_conn.execute = AsyncMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    engine = MagicMock(spec=PaperTradingEngine)

    import asyncio
    with pytest.raises(asyncio.CancelledError):
        await command_poller(mock_pool, engine)

    # Verify paper_strategy_instances insert was called
    executed_sqls = [call[0][0] for call in mock_conn.execute.call_args_list]
    assert any("INSERT INTO paper_strategy_instances" in sql for sql in executed_sqls)
    # Verify command marked COMPLETED
    assert any("UPDATE execution_commands SET status = 'COMPLETED'" in sql for sql in executed_sqls)


@pytest.mark.asyncio
async def test_command_poller_processes_manual_order():
    """Command poller dispatches MANUAL_ORDER directly to engine dispatcher."""
    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    cmd_id = uuid.uuid4()

    command_row = {
        "command_id": cmd_id,
        "command_type": "MANUAL_ORDER",
        "target_instance_id": "MANUAL_TRADE",
        "payload": json.dumps({
            "client_order_id": "MANUAL-TEST-001",
            "instrument_id": "BTCUSDT.BINANCE",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity": 0.025,
            "price": None,
        }),
    }

    call_count = 0
    async def fake_fetchrow(query, *args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return command_row
        raise asyncio.CancelledError()

    mock_conn.fetchrow = fake_fetchrow
    mock_conn.execute = AsyncMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    engine = MagicMock(spec=PaperTradingEngine)
    engine.dispatcher = MagicMock()

    import asyncio
    with pytest.raises(asyncio.CancelledError):
        await command_poller(mock_pool, engine)

    engine.dispatcher.dispatch.assert_called_once()
    intent = engine.dispatcher.dispatch.call_args[0][0]
    assert intent.client_order_id == "MANUAL-TEST-001"
    assert intent.instrument_id == "BTCUSDT.BINANCE"
    assert intent.side == "BUY"
    assert intent.quantity == Decimal("0.025")
