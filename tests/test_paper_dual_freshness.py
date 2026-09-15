"""Test dual freshness contract: ingress health continuity AND LiveNode cache quote currency."""

import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from cryptonite_execution.ingress import IngressHealthMonitor
from cryptonite_execution.kill_switch import LatchedKillSwitch
from cryptonite_execution.risk import GuardedOrderDispatcher, OrderIntent
from nautilus_trader.model import InstrumentId, Price, Quantity, QuoteTick


def make_test_quote(symbol: str, bid: str, ask: str, ts_init_ns: int) -> QuoteTick:
    inst = InstrumentId.from_str(f"{symbol}.BINANCE")
    return QuoteTick(
        instrument_id=inst,
        bid_price=Price.from_str(bid),
        ask_price=Price.from_str(ask),
        bid_size=Quantity.from_str("1.0"),
        ask_size=Quantity.from_str("1.0"),
        ts_event=ts_init_ns,
        ts_init=ts_init_ns,
    )


def test_dual_freshness_matrix():
    """Verify all branches of the dual freshness contract."""
    monitor = IngressHealthMonitor(max_staleness_s=5.0)
    symbol = "BTCUSDT"
    inst_str = f"{symbol}.BINANCE"
    now_ns = 1_700_000_000_000_000_000
    session_id = "SESSION-001"

    # Case 0: No telemetry recorded yet -> not fresh
    mock_cache = MagicMock()
    assert monitor.is_fresh(inst_str, mock_cache, now_ns=now_ns) is False

    # Case 1: Telemetry fresh, but node cache has no quote -> not fresh
    monitor.update_health(
        instrument_id=inst_str,
        session_id=session_id,
        seq=1,
        event_ts_ns=now_ns - 100_000_000,
        arrival_ts_ns=now_ns - 50_000_000,
    )
    mock_cache.quote.return_value = None
    assert monitor.is_fresh(inst_str, mock_cache, now_ns=now_ns) is False

    # Case 2: Telemetry fresh, node cache has fresh quote aligned with health -> FRESH!
    fresh_quote = make_test_quote(symbol, "50000.00", "50010.00", now_ns - 50_000_000)
    mock_cache.quote.return_value = fresh_quote
    assert monitor.is_fresh(inst_str, mock_cache, now_ns=now_ns) is True

    # Case 3: Telemetry sequence gap detected -> not fresh
    monitor.update_health(
        instrument_id=inst_str,
        session_id=session_id,
        seq=3,  # gap! expected 2
        event_ts_ns=now_ns - 40_000_000,
        arrival_ts_ns=now_ns - 30_000_000,
    )
    assert monitor.is_fresh(inst_str, mock_cache, now_ns=now_ns) is False

    # Reset with new continuous session
    session_2 = "SESSION-002"
    monitor.update_health(
        instrument_id=inst_str,
        session_id=session_2,
        seq=1,
        event_ts_ns=now_ns - 20_000_000,
        arrival_ts_ns=now_ns - 10_000_000,
    )
    assert monitor.is_fresh(inst_str, mock_cache, now_ns=now_ns) is True

    # Case 4: Telemetry arrival is stale (> 5 seconds old) -> not fresh
    future_now_ns = now_ns + 6_000_000_000  # 6 seconds later
    assert monitor.is_fresh(inst_str, mock_cache, now_ns=future_now_ns) is False

    # Case 5: Telemetry is fresh, but quote in cache is lagging verified health arrival by > 1s -> not fresh
    lagging_quote = make_test_quote(symbol, "50000.00", "50010.00", (now_ns - 20_000_000) - 2_000_000_000)
    mock_cache.quote.return_value = lagging_quote
    assert monitor.is_fresh(inst_str, mock_cache, now_ns=now_ns) is False


def test_guarded_dispatcher_rejects_order_when_dual_freshness_fails():
    """Verify that GuardedOrderDispatcher rejects orders immediately if dual freshness is violated."""
    kill_switch = LatchedKillSwitch()
    monitor = IngressHealthMonitor(max_staleness_s=5.0)
    mock_cache = MagicMock()
    symbol = "BTCUSDT"
    inst_str = f"{symbol}.BINANCE"

    dispatcher = GuardedOrderDispatcher(
        account_id="PAPER-ACCOUNT-001",
        starting_cash=Decimal("10000.0"),
        kill_switch=kill_switch,
        ingress_health=monitor,
        node_cache=mock_cache,
    )

    intent = OrderIntent(
        client_order_id="ORD-INTENT-001",
        instance_id="INST-001",
        instrument_id=inst_str,
        side="BUY",
        order_type="LIMIT",
        price=Decimal("50000.00"),
        quantity=Decimal("0.1"),
    )

    # Cache has no quote and monitor has no health
    approved, reason, _ = dispatcher.evaluate_intent(intent)
    assert approved is False
    assert reason == "DATA_STALE_OR_DISCONTINUOUS"
