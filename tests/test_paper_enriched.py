"""Unit tests for enriched Paper Trading and Strategy API endpoints."""

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptonite_api.main import create_app
from cryptonite_api.routes.auth import require_auth, require_csrf, require_operator
from cryptonite_schemas.universe import IngestUniverse
from cryptonite_strategies.registry import STRATEGY_REGISTRY, get_strategy_meta, list_strategies
from fastapi.testclient import TestClient


@pytest.fixture
def test_app_and_pool():
    app = create_app()
    mock_pool = MagicMock()
    mock_pool.fetch = AsyncMock(return_value=[])
    mock_pool.fetchrow = AsyncMock(return_value=None)
    mock_pool.fetchval = AsyncMock(return_value=None)
    mock_pool.execute = AsyncMock(return_value=None)
    app.state.pool = mock_pool

    redis = MagicMock()
    redis.get = AsyncMock(return_value=IngestUniverse.build(["BTCUSDT", "ETHUSDT"], ["1m", "1h"]).model_dump_json())
    app.state.stream_hub = SimpleNamespace(redis=redis)

    app.dependency_overrides[require_auth] = lambda: {"type": "session", "role": "operator"}
    app.dependency_overrides[require_operator] = lambda: {"type": "session", "role": "operator"}
    app.dependency_overrides[require_csrf] = lambda: {"type": "session", "role": "operator"}

    return TestClient(app), mock_pool


def test_strategy_registry_rich_metadata():
    """Verify that STRATEGY_REGISTRY contains thesis, category, market_regime, and timeframe_default."""
    expected_slugs = ["ema-cross", "rsi-reversal", "supertrend"]
    for slug in expected_slugs:
        assert slug in STRATEGY_REGISTRY
        entry = STRATEGY_REGISTRY[slug]
        assert "thesis" in entry and len(entry["thesis"]) > 10
        assert "category" in entry and entry["category"] in ("trend_following", "mean_reversion", "breakout")
        assert "market_regime" in entry and len(entry["market_regime"]) > 0
        assert "timeframe_default" in entry and len(entry["timeframe_default"]) > 0

    ema_meta = get_strategy_meta("ema-cross")
    assert ema_meta["category"] == "trend_following"
    assert ema_meta["market_regime"] == "High Volatility Trends"
    assert ema_meta["timeframe_default"] == "15m"
    assert "thesis" in ema_meta

    rsi_meta = get_strategy_meta("rsi-reversal")
    assert rsi_meta["category"] == "mean_reversion"
    assert rsi_meta["market_regime"] == "Ranging / Consolidation"
    assert rsi_meta["timeframe_default"] == "15m"

    supertrend_meta = get_strategy_meta("supertrend")
    assert supertrend_meta["category"] == "breakout"
    assert supertrend_meta["market_regime"] == "High Volatility Trends"
    assert supertrend_meta["timeframe_default"] == "1h"

    all_strats = list_strategies()
    assert len(all_strats) >= 3
    for s in all_strats:
        assert "thesis" in s
        assert "category" in s
        assert "market_regime" in s
        assert "timeframe_default" in s


def test_get_strategies_api_merges_metadata(test_app_and_pool):
    """Verify that GET /api/v1/strategies merges thesis, category, and market_regime."""
    client, mock_pool = test_app_and_pool

    strat_rows = [
        {
            "id": uuid.uuid4(),
            "name": "EMA Crossover Strategy",
            "slug": "ema-cross",
            "description": "Trend-following EMA strategy",
            "author": "Cryptonite Core",
            "active_version_id": uuid.uuid4(),
            "version_num": 1,
            "code_hash": "abc123hash",
            "default_parameters": json.dumps({"fast_period": 9, "slow_period": 21}),
            "parameter_schema": json.dumps({}),
        },
        {
            "id": uuid.uuid4(),
            "name": "RSI Oversold Reversal",
            "slug": "rsi-reversal",
            "description": "Mean-reversion RSI strategy",
            "author": "Cryptonite Core",
            "active_version_id": uuid.uuid4(),
            "version_num": 1,
            "code_hash": "def456hash",
            "default_parameters": json.dumps({"rsi_period": 14}),
            "parameter_schema": json.dumps({}),
        },
    ]

    mock_pool.fetchrow = AsyncMock(return_value={"id": uuid.uuid4()})
    mock_pool.fetch = AsyncMock(return_value=strat_rows)

    res = client.get("/api/v1/strategies")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2

    by_slug = {s["slug"]: s for s in data}
    assert "ema-cross" in by_slug
    assert by_slug["ema-cross"]["category"] == "trend_following"
    assert by_slug["ema-cross"]["market_regime"] == "High Volatility Trends"
    assert by_slug["ema-cross"]["timeframe_default"] == "15m"
    assert len(by_slug["ema-cross"]["thesis"]) > 10

    assert "rsi-reversal" in by_slug
    assert by_slug["rsi-reversal"]["category"] == "mean_reversion"
    assert by_slug["rsi-reversal"]["market_regime"] == "Ranging / Consolidation"
    assert by_slug["rsi-reversal"]["timeframe_default"] == "15m"
    assert len(by_slug["rsi-reversal"]["thesis"]) > 10


def test_get_strategy_by_slug_merges_metadata(test_app_and_pool):
    """Verify that GET /api/v1/strategies/{slug} returns enriched metadata."""
    client, mock_pool = test_app_and_pool

    mock_pool.fetchrow = AsyncMock(return_value={
        "id": uuid.uuid4(),
        "name": "Supertrend Breakout",
        "slug": "supertrend",
        "description": "Volatility breakout strategy",
        "author": "Cryptonite Core",
        "active_version_id": uuid.uuid4(),
        "version_num": 1,
        "code_hash": "ghi789hash",
        "python_code": None,
        "default_parameters": json.dumps({"atr_period": 10, "multiplier": 3.0}),
        "parameter_schema": json.dumps({}),
    })

    res = client.get("/api/v1/strategies/supertrend")
    assert res.status_code == 200
    data = res.json()
    assert data["slug"] == "supertrend"
    assert data["category"] == "breakout"
    assert data["market_regime"] == "High Volatility Trends"
    assert data["timeframe_default"] == "1h"
    assert len(data["thesis"]) > 10


def test_get_positions_enriched_fields(test_app_and_pool):
    """Verify that get_positions returns opened_at, strategy_name, duration_s, stop_loss_price, and take_profit_price."""
    client, mock_pool = test_app_and_pool

    now_iso = datetime.now(timezone.utc).isoformat()
    past_iso = "2026-10-18T12:00:00+00:00"

    positions = [
        {
            "position_id": "POS-001",
            "account_id": "PAPER-ACCOUNT-001",
            "instance_id": "INST-EMA-1",
            "instrument_id": "BTCUSDT.BINANCE",
            "side": "LONG",
            "entry_price": 50000.0,
            "quantity": 0.5,
            "current_price": 51000.0,
            "unrealized_pnl": 500.0,
            "realized_pnl": 0.0,
            "status": "OPEN",
            "opened_at": past_iso,
            "closed_at": None,
            "strategy_name": "EMA Crossover Strategy",
        },
        {
            "position_id": "POS-002",
            "account_id": "PAPER-ACCOUNT-001",
            "instance_id": "MANUAL_TRADE",
            "instrument_id": "ETHUSDT.BINANCE",
            "side": "SHORT",
            "entry_price": 3000.0,
            "quantity": 2.0,
            "current_price": 2900.0,
            "unrealized_pnl": 200.0,
            "realized_pnl": 0.0,
            "status": "OPEN",
            "opened_at": now_iso,
            "closed_at": None,
            "strategy_name": None,  # joined row had no match
        },
        {
            "position_id": "POS-003",
            "account_id": "PAPER-ACCOUNT-001",
            "instance_id": "AI_DEBATE_WORKER",
            "instrument_id": "SOLUSDT.BINANCE",
            "side": "LONG",
            "entry_price": 100.0,
            "quantity": 10.0,
            "current_price": 105.0,
            "unrealized_pnl": 50.0,
            "realized_pnl": 0.0,
            "status": "OPEN",
            "opened_at": None,
            "closed_at": None,
            "strategy_name": "AIDebateTrading",  # raw seed name should normalize
        },
    ]

    async def mock_fetch(sql, *args):
        if "system_settings" in sql:
            return []  # use defaults: stop_loss_pct=2.5, take_profit_pct=4.0
        return positions

    mock_pool.fetch = AsyncMock(side_effect=mock_fetch)

    res = client.get("/api/v1/paper/positions")
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 3

    # Position 1: LONG with joined strategy_name
    p1 = rows[0]
    assert p1["position_id"] == "POS-001"
    assert p1["account_id"] == "PAPER-ACCOUNT-001"
    assert p1["strategy_name"] == "EMA Crossover Strategy"
    assert p1["opened_at"] == past_iso
    assert p1["duration_s"] > 0
    # Stop loss for LONG at 2.5%: 50000 * (1 - 0.025) = 48750.0
    assert p1["stop_loss_price"] == 48750.0
    # Take profit for LONG at 4.0%: 50000 * (1 + 0.04) = 52000.0
    assert p1["take_profit_price"] == 52000.0
    assert p1["side"] == "LONG"
    assert p1["entry_price"] == 50000.0
    assert p1["quantity"] == 0.5
    assert p1["current_price"] == 51000.0
    assert p1["status"] == "OPEN"

    # Position 2: SHORT with MANUAL_TRADE fallback
    p2 = rows[1]
    assert p2["position_id"] == "POS-002"
    assert p2["strategy_name"] == "Manual Trade"
    assert p2["opened_at"] == now_iso
    assert p2["duration_s"] >= 0
    # Stop loss for SHORT at 2.5%: 3000 * (1 + 0.025) = 3075.0
    assert p2["stop_loss_price"] == 3075.0
    # Take profit for SHORT at 4.0%: 3000 * (1 - 0.04) = 2880.0
    assert p2["take_profit_price"] == 2880.0

    # Position 3: AI_DEBATE_WORKER normalization and missing opened_at fallback
    p3 = rows[2]
    assert p3["position_id"] == "POS-003"
    assert p3["strategy_name"] == "AI Debate Worker"
    assert p3["opened_at"] is not None
    assert p3["duration_s"] >= 0
    # Stop loss for LONG at 2.5%: 100 * (1 - 0.025) = 97.5
    assert p3["stop_loss_price"] == 97.5
    # Take profit for LONG at 4.0%: 100 * (1 + 0.04) = 104.0
    assert p3["take_profit_price"] == 104.0


def test_get_positions_custom_exit_rules(test_app_and_pool):
    """Verify that system_settings exit rules are dynamically picked up by get_positions."""
    client, mock_pool = test_app_and_pool

    positions = [
        {
            "position_id": "POS-100",
            "account_id": "PAPER-ACCOUNT-001",
            "instance_id": "INST-CUSTOM",
            "instrument_id": "BTCUSDT.BINANCE",
            "side": "LONG",
            "entry_price": 10000.0,
            "quantity": 1.0,
            "current_price": 10000.0,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "status": "OPEN",
            "opened_at": "2026-10-18T10:00:00Z",
            "closed_at": None,
            "strategy_name": "Custom Trend",
        }
    ]

    async def mock_fetch(sql, *args):
        if "system_settings" in sql:
            # Custom 3.0% stop loss and 5.0% take profit
            return [
                {"key": "exit.stop_loss_pct", "value": "3.0"},
                {"key": "exit.trail_activate_pct", "value": "5.0"},
            ]
        return positions

    mock_pool.fetch = AsyncMock(side_effect=mock_fetch)

    res = client.get("/api/v1/paper/positions")
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 1
    p = rows[0]
    # 10000 * (1 - 0.03) = 9700.0
    assert p["stop_loss_price"] == 9700.0
    # 10000 * (1 + 0.05) = 10500.0
    assert p["take_profit_price"] == 10500.0


def test_get_orders_enriched_strategy_name(test_app_and_pool):
    """Verify that get_orders returns strategy_name from joined instance or fallback."""
    client, mock_pool = test_app_and_pool

    orders = [
        {
            "client_order_id": "ORD-001",
            "instance_id": "INST-EMA-1",
            "account_id": "PAPER-ACCOUNT-001",
            "instrument_id": "BTCUSDT.BINANCE",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 0.5,
            "price": 50000.0,
            "filled_qty": 0.5,
            "avg_fill_price": 50000.0,
            "status": "FILLED",
            "created_at": "2026-10-18T14:00:00Z",
            "strategy_name": "EMA Crossover Strategy",
        },
        {
            "client_order_id": "ORD-002",
            "instance_id": "MANUAL_TRADE",
            "account_id": "PAPER-ACCOUNT-001",
            "instrument_id": "ETHUSDT.BINANCE",
            "side": "SELL",
            "order_type": "MARKET",
            "quantity": 1.0,
            "price": None,
            "filled_qty": 1.0,
            "avg_fill_price": 3000.0,
            "status": "FILLED",
            "created_at": "2026-10-18T14:05:00Z",
            "strategy_name": None,
        },
    ]

    mock_pool.fetch = AsyncMock(return_value=orders)

    res = client.get("/api/v1/paper/orders")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2
    assert data[0]["strategy_name"] == "EMA Crossover Strategy"
    assert data[1]["strategy_name"] == "Manual Trade"


def test_get_trades_enriched_and_slippage(test_app_and_pool):
    """Verify that get_trades includes strategy_name and slippage_bps against order price."""
    client, mock_pool = test_app_and_pool

    trades = [
        {
            "trade_id": "TRD-001",
            "account_id": "PAPER-ACCOUNT-001",
            "client_order_id": "ORD-001",
            "instance_id": "INST-EMA-1",
            "instrument_id": "BTCUSDT.BINANCE",
            "side": "BUY",
            "price": 50100.0,
            "quantity": 0.5,
            "commission": 2.5,
            "commission_currency": "USDT",
            "executed_at": "2026-10-18T14:01:00Z",
            "strategy_name": "EMA Crossover Strategy",
            "order_price": 50000.0,
        },
        {
            "trade_id": "TRD-002",
            "account_id": "PAPER-ACCOUNT-001",
            "client_order_id": "ORD-002",
            "instance_id": "MANUAL_TRADE",
            "instrument_id": "BTCUSDT.BINANCE",
            "side": "SELL",
            "price": 49900.0,
            "quantity": 0.5,
            "commission": 2.5,
            "commission_currency": "USDT",
            "executed_at": "2026-10-18T14:02:00Z",
            "strategy_name": None,
            "order_price": 50000.0,
        },
        {
            "trade_id": "TRD-003",
            "account_id": "PAPER-ACCOUNT-001",
            "client_order_id": "ORD-003",
            "instance_id": "AI_DEBATE_WORKER",
            "instrument_id": "BTCUSDT.BINANCE",
            "side": "BUY",
            "price": 50000.0,
            "quantity": 0.5,
            "commission": 2.5,
            "commission_currency": "USDT",
            "executed_at": "2026-10-18T14:03:00Z",
            "strategy_name": None,
            "order_price": None,  # Market order without limit price
        },
    ]

    mock_pool.fetch = AsyncMock(return_value=trades)

    res = client.get("/api/v1/paper/trades")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 3

    # Trade 1: BUY at 50100 vs order 50000 -> ((50100 - 50000) / 50000) * 10000 = +20.0 bps
    t1 = data[0]
    assert t1["strategy_name"] == "EMA Crossover Strategy"
    assert t1["slippage_bps"] == 20.0
    assert "order_price" not in t1  # cleaned up from output

    # Trade 2: SELL at 49900 vs order 50000 -> adverse to seller: +20.0 bps
    t2 = data[1]
    assert t2["strategy_name"] == "Manual Trade"
    assert t2["slippage_bps"] == 20.0

    # Trade 3: Market order -> slippage_bps is None
    t3 = data[2]
    assert t3["strategy_name"] == "AI Debate Worker"
    assert t3["slippage_bps"] is None
