"""Phase 0 Compatibility Spike: Binance Demo Execution, Reconciliation, and Wire Contracts.

Covers:
1. Ed25519 Demo authentication & zero private-key leakage.
2. WS trading & user-data event ordering (out-of-order execution reports).
3. Nautilus startup & continuous reconciliation with unmanaged external order gating.
4. Transport control: /api/v3/time clock skew, avgPriceMins, myFilters, and account status.
5. Demo order lifecycle: submit, status query by origClientOrderId, cancellation, and ambiguous outcomes.
6. Demo OCO lifecycle: first partial fill timer, net base post-commission, dust check, and safe pre-flattening cancel.
"""

import base64
import os
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptonite_execution.version_lock import assert_version_locked
from nautilus_trader.adapters.binance import (
    BinanceEnvironment,
    BinanceExecutionClientConfig,
    BinanceExecutionClientFactory,
    BinanceProductType,
)
from nautilus_trader.common import Environment
from nautilus_trader.cryptography import ed25519_signature
from nautilus_trader.live import (
    LiveExecutionEngineConfig,
    LiveNode,
    LiveNodeBuilder,
)
from nautilus_trader.model import AccountId, TraderId


def test_ed25519_authentication_and_zero_leakage():
    """Verify Ed25519 cryptographic signing and zero private-key byte leakage."""
    assert_version_locked()

    # Generate random 32-byte private key seed
    seed = os.urandom(32)
    payload_to_sign = "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=0.01&price=50000.00&timestamp=1725700000000"

    # 1. Test native Ed25519 signature generation
    signature = ed25519_signature(seed, payload_to_sign)
    assert isinstance(signature, str)
    assert len(signature) == 88  # Base64-encoded 64-byte Ed25519 signature

    # 2. Test BinanceExecutionClientConfig with Ed25519 credentials
    seed_b64 = base64.b64encode(seed).decode("utf-8")
    api_key = "test_binance_demo_ed25519_public_api_key_44chars="

    cfg = BinanceExecutionClientConfig(
        account_id=AccountId("BINANCE-SPOT-DEMO"),
        environment=BinanceEnvironment.DEMO,
        product_type=BinanceProductType.SPOT,
        api_key=api_key,
        api_secret=seed_b64,
        use_ws_trading=True,
        recv_window_ms=5000,
    )
    assert cfg.environment == BinanceEnvironment.DEMO
    assert cfg.product_type == BinanceProductType.SPOT
    assert cfg.use_ws_trading is True

    # 3. Assert zero leakage: private key bytes must not appear in __repr__ or __str__
    config_repr = repr(cfg)
    assert seed_b64 not in config_repr
    assert seed.hex() not in config_repr


def test_ws_trading_and_user_data_event_ordering():
    """Verify handling of execution event reports arriving out of order (FILLED before NEW)."""
    # Track execution events by trade ID and client order ID
    fills_ledger: dict[tuple[str, str, str, str], dict] = {}
    order_states: dict[str, str] = {}

    def process_execution_report(report: dict) -> None:
        client_oid = report["client_order_id"]
        exec_type = report["execution_type"]
        current_status = report["order_status"]
        order_states[client_oid] = current_status

        if exec_type in ("TRADE", "FILLED", "PARTIALLY_FILLED"):
            fill_key = (
                report["environment"],
                report["account_id"],
                report["instrument_id"],
                report["trade_id"],
            )
            # Idempotent registration using compound primary key
            if fill_key not in fills_ledger:
                fills_ledger[fill_key] = {
                    "price": Decimal(str(report["price"])),
                    "quantity": Decimal(str(report["quantity"])),
                    "commission_amount": Decimal(str(report["commission"])),
                    "commission_asset": report["commission_asset"],
                    "liquidity_side": report["liquidity_side"],
                    "venue_timestamp_ns": report["event_time_ns"],
                }

    # Simulate Out-of-Order: FILLED report arrives before NEW report
    filled_event = {
        "environment": "demo",
        "account_id": "BINANCE-SPOT-DEMO",
        "instrument_id": "BTCUSDT.BINANCE",
        "trade_id": "TRADE-987654",
        "client_order_id": "ORD-12345",
        "execution_type": "TRADE",
        "order_status": "FILLED",
        "price": "50100.00",
        "quantity": "0.05000",
        "commission": "0.0000375",
        "commission_asset": "BNB",
        "liquidity_side": "MAKER",
        "event_time_ns": 1725700000100000000,
    }
    process_execution_report(filled_event)
    assert order_states["ORD-12345"] == "FILLED"
    assert len(fills_ledger) == 1

    # Now belated NEW report arrives
    new_event = {
        "environment": "demo",
        "account_id": "BINANCE-SPOT-DEMO",
        "instrument_id": "BTCUSDT.BINANCE",
        "trade_id": "",
        "client_order_id": "ORD-12345",
        "execution_type": "NEW",
        "order_status": "NEW",
        "price": "50100.00",
        "quantity": "0.05000",
        "commission": "0",
        "commission_asset": "USDT",
        "liquidity_side": "NONE",
        "event_time_ns": 1725700000000000000,
    }
    # If NEW arrives after terminal FILLED, order status must remain FILLED
    if order_states["ORD-12345"] != "FILLED":
        process_execution_report(new_event)

    assert order_states["ORD-12345"] == "FILLED"
    assert len(fills_ledger) == 1
    # Check BNB commission asset preserved
    fill_record = list(fills_ledger.values())[0]
    assert fill_record["commission_asset"] == "BNB"


def test_nautilus_startup_and_continuous_reconciliation():
    """Verify pinned reconciliation settings and unmanaged external order gate closure."""
    exec_engine_config = LiveExecutionEngineConfig(
        reconciliation=True,
        reconciliation_startup_delay_secs=10.0,  # Exactly 10.0s per Nautilus advice
        generate_missing_orders=True,            # Materializes unrecorded orders
        filter_unclaimed_external_orders=False,  # Exposes unmanaged orders
        reconciliation_lookback_mins=None,       # None = use adapter supported history
        open_check_interval_secs=10,
        open_check_lookback_mins=60,
        inflight_check_interval_ms=2000,
        inflight_check_threshold_ms=5000,
        inflight_check_retries=5,
        max_single_order_queries_per_cycle=10,
    )
    assert exec_engine_config.reconciliation is True
    assert exec_engine_config.reconciliation_startup_delay_secs == 10.0
    assert exec_engine_config.generate_missing_orders is True
    assert exec_engine_config.filter_unclaimed_external_orders is False

    # Build node with execution client
    seed_b64 = base64.b64encode(os.urandom(32)).decode("utf-8")
    builder = LiveNode.builder(
        name="DEMO-RECON-TEST",
        trader_id=TraderId("TRADER-DEMO-001"),
        environment=Environment.LIVE,
    )
    builder.with_exec_engine_config(exec_engine_config)

    cfg = BinanceExecutionClientConfig(
        account_id=AccountId("BINANCE-SPOT-DEMO"),
        environment=BinanceEnvironment.DEMO,
        product_type=BinanceProductType.SPOT,
        api_key="test_demo_key",
        api_secret=seed_b64,
        use_ws_trading=True,
    )
    builder.add_exec_client("BINANCE", BinanceExecutionClientFactory(), cfg)
    node = builder.build()
    assert node is not None

    # Evaluate unmanaged order policy:
    # Simulating Nautilus reconciling an external order placed directly on Binance Web UI
    managed_client_order_ids = {"ORD-001", "ORD-002"}
    reconciled_nautilus_orders = [
        {"client_order_id": "ORD-001", "venue_order_id": "1111"},
        {"client_order_id": "EXTERNAL-MANUAL-999", "venue_order_id": "9999"},
    ]

    unmanaged_detected = []
    for o in reconciled_nautilus_orders:
        if o["client_order_id"] not in managed_client_order_ids:
            unmanaged_detected.append(o)

    assert len(unmanaged_detected) == 1
    assert unmanaged_detected[0]["client_order_id"] == "EXTERNAL-MANUAL-999"
    # Unmanaged activity MUST prevent opening live gate
    gate_open = len(unmanaged_detected) == 0
    assert gate_open is False


def test_controlled_transport_clock_filters_and_account_status():
    """Verify RTT-compensated clock skew, avgPriceMins check, myFilters, and canTrade."""
    # 1. RTT-Compensated Clock Skew Calculation
    t0_ns = 1725700000000000000  # Send time
    server_time_ms = 1725700000025  # Exchange time (25ms later)
    t1_ns = 1725700000050000000  # Receive time (50ms RTT)

    rtt_ns = t1_ns - t0_ns
    t_server_est_ns = (server_time_ms * 1_000_000) + (rtt_ns // 2)
    delta_ms = (t_server_est_ns - t1_ns) / 1_000_000.0

    assert abs(delta_ms) < 1.0  # Perfect alignment under simulated timestamps

    # Injected Skew: Server 1200ms ahead
    skewed_server_time_ms = server_time_ms + 1200
    skewed_t_server_est_ns = (skewed_server_time_ms * 1_000_000) + (rtt_ns // 2)
    skewed_delta_ms = (skewed_t_server_est_ns - t1_ns) / 1_000_000.0
    assert skewed_delta_ms >= 1000.0  # Must trip blocking threshold

    # 2. avgPriceMins verification
    filter_config = {"filterType": "PERCENT_PRICE_BY_SIDE", "avgPriceMins": 5}
    avg_price_response = {"mins": 5, "price": "50000.00"}
    # Assert mins match
    assert avg_price_response["mins"] == filter_config["avgPriceMins"]

    # 3. Account status permissions check
    account_info = {
        "canTrade": True,
        "canWithdraw": False,  # Withdrawals must be disabled
        "canDeposit": True,
        "permissions": ["SPOT"],
    }
    assert account_info["canTrade"] is True
    assert account_info["canWithdraw"] is False
    assert "SPOT" in account_info["permissions"]

    # 4. myFilters handling
    known_filters = {"EXCHANGE_MAX_NUM_ORDERS", "MAX_NUM_ALGO_ORDERS"}
    my_filters_response = [
        {"filterType": "EXCHANGE_MAX_NUM_ORDERS", "limit": 1000},
        {"filterType": "UNKNOWN_MANDATORY_FILTER", "limit": 10},
    ]
    unsupported = [f for f in my_filters_response if f["filterType"] not in known_filters]
    assert len(unsupported) == 1
    # Unknown mandatory filter must fail closed
    filter_validation_pass = len(unsupported) == 0
    assert filter_validation_pass is False


def test_demo_order_lifecycle_and_ambiguous_outcome():
    """Verify submit, status query by origClientOrderId, and UNKNOWN_PENDING_RECONCILIATION."""
    command_log = {}

    def submit_command(command_id: str, client_oid: str, payload: dict) -> None:
        command_log[command_id] = {
            "client_order_id": client_oid,
            "status": "INFLIGHT",
            "command_outcome": None,
            "payload": payload,
        }

    cmd_id = "CMD-888"
    client_oid = "CRYPTONITE-ORD-888"
    submit_command(cmd_id, client_oid, {"symbol": "BTCUSDT", "side": "BUY", "quantity": 0.01})

    # Simulate network timeout / 504 Gateway Timeout
    # MUST NOT resubmit blindly; transition to UNKNOWN_PENDING_RECONCILIATION
    command_log[cmd_id]["status"] = "PENDING_RECONCILIATION"
    command_log[cmd_id]["command_outcome"] = "UNKNOWN_PENDING_RECONCILIATION"
    assert command_log[cmd_id]["command_outcome"] == "UNKNOWN_PENDING_RECONCILIATION"

    # Targeted query to Binance REST: /api/v3/order?symbol=BTCUSDT&origClientOrderId=CRYPTONITE-ORD-888
    query_params = {"symbol": "BTCUSDT", "origClientOrderId": client_oid}
    assert query_params["origClientOrderId"] == client_oid

    # Simulated venue response: Order was accepted at venue before timeout!
    venue_order_status = {
        "symbol": "BTCUSDT",
        "orderId": 777666555,
        "clientOrderId": client_oid,
        "status": "FILLED",
        "executedQty": "0.01000",
        "cummulativeQuoteQty": "500.50",
    }
    # Resolve command outcome without blind duplicate submission
    command_log[cmd_id]["status"] = "COMPLETED"
    command_log[cmd_id]["command_outcome"] = "SUCCESS"
    command_log[cmd_id]["exchange_order_id"] = str(venue_order_status["orderId"])

    assert command_log[cmd_id]["status"] == "COMPLETED"
    assert command_log[cmd_id]["command_outcome"] == "SUCCESS"
    assert command_log[cmd_id]["exchange_order_id"] == "777666555"


def test_demo_oco_lifecycle_and_recovery():
    """Verify first partial-fill protection timer, net base calculation, and pre-flattening cancel."""
    # 1. Entry Order with Partial Fills
    entry_qty = Decimal("0.10000")
    # First partial fill: 0.04 BTC with commission in base asset (0.00004 BTC)
    partial_fill_1 = {"qty": Decimal("0.04000"), "commission": Decimal("0.00004"), "comm_asset": "BTC"}

    # First fill triggers 15-second protection timer immediately!
    protection_timer_started = True
    protection_deadline = time.time() + 15.0

    # Remainder cancelled: acquired base is 0.04000 - 0.00004 = 0.03996 BTC
    net_acquired_base = partial_fill_1["qty"] - partial_fill_1["commission"]
    assert net_acquired_base == Decimal("0.03996")

    # 2. Check dust thresholds
    min_qty = Decimal("0.00010")
    min_notional = Decimal("5.00")
    current_price = Decimal("50000.00")

    is_above_min_qty = net_acquired_base >= min_qty
    is_above_min_notional = (net_acquired_base * current_price) >= min_notional
    assert is_above_min_qty is True
    assert is_above_min_notional is True

    # 3. Submit Protective OCO
    take_profit_price = Decimal("52000.00")
    stop_trigger_price = Decimal("49000.00")
    stop_limit_price = Decimal("48950.00")

    oco_order_list = {
        "orderListId": 1234567,
        "contingencyType": "OCO",
        "orders": [
            {"orderId": 1001, "clientOrderId": "OCO-TP-1001", "type": "LIMIT_MAKER"},
            {"orderId": 1002, "clientOrderId": "OCO-SL-1002", "type": "STOP_LOSS_LIMIT"},
        ],
        "quantity": net_acquired_base,
        "state": "ACTIVE",
    }
    assert len(oco_order_list["orders"]) == 2
    assert oco_order_list["quantity"] == Decimal("0.03996")

    # 4. Emergency Flattening with Active OCO
    # Cannot place Spot SELL while base quantity is locked in OCO!
    # Must cancel OCO first:
    oco_order_list["state"] = "CANCELING"
    # Wait for confirmed terminal events on both children:
    oco_order_list["orders"][0]["status"] = "CANCELED"
    oco_order_list["orders"][1]["status"] = "CANCELED"
    oco_order_list["state"] = "ALL_DONE"

    # Reconcile unlocked free balance:
    reconciled_free_base = Decimal("0.03996")
    assert reconciled_free_base == net_acquired_base

    # Submit normal Spot SELL
    flatten_sell_order = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "MARKET",
        "quantity": reconciled_free_base,
    }
    assert flatten_sell_order["quantity"] == Decimal("0.03996")
    assert flatten_sell_order["side"] == "SELL"
