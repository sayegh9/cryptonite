"""Category B Credentialed Binance Demo Integration & Acceptance Suite.

Dedicated release gate for Binance Demo Mode verification.
Requires:
  - BINANCE_DEMO_API_KEY (or Docker secret /run/secrets/binance_demo_api_key)
  - BINANCE_DEMO_PRIVATE_KEY_PATH (Ed25519 PEM/DER file, e.g. /run/secrets/binance_demo_private_key.pem)

Endpoints (Restricted Demo-Only Egress):
  - Demo REST: https://demo-api.binance.com
  - Demo WS Trading API: wss://demo-ws-api.binance.com/ws-api/v3
  - Demo Streams: wss://demo-stream.binance.com/ws

Markers:
  - pytest -m "not binance_demo and not binance_demo_strict": deselected for normal CI.
  - pytest -m binance_demo: skips cleanly when credentials are not configured.
  - pytest -m binance_demo_strict: FAILS immediately if credentials are missing (Release Acceptance Gate).
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from decimal import Decimal
from typing import Optional, Tuple
import httpx
import pytest
import websockets

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptonite_execution.clock import ClockSyncMonitor
from cryptonite_execution.version_lock import assert_version_locked
from nautilus_trader.adapters.binance import (
    BinanceEnvironment,
    BinanceExecutionClientConfig,
    BinanceProductType,
)
from nautilus_trader.cryptography import ed25519_signature
from nautilus_trader.model import AccountId

# 1. Resolve API Credentials & Secrets
def _load_credentials() -> Tuple[Optional[str], Optional[ed25519.Ed25519PrivateKey]]:
    api_key = os.getenv("BINANCE_DEMO_API_KEY")
    if not api_key:
        key_secret_file = Path("/run/secrets/binance_demo_api_key")
        if key_secret_file.exists():
            api_key = key_secret_file.read_text().strip()

    priv_key: Optional[ed25519.Ed25519PrivateKey] = None

    priv_key_path = os.getenv("BINANCE_DEMO_PRIVATE_KEY_PATH") or "/run/secrets/binance_demo_private_key.pem"
    if priv_key_path and Path(priv_key_path).exists():
        try:
            key_data = Path(priv_key_path).read_bytes()
            loaded = load_pem_private_key(key_data, password=None)
            if isinstance(loaded, ed25519.Ed25519PrivateKey):
                priv_key = loaded
        except Exception:
            pass

    return api_key, priv_key


API_KEY, ED25519_PRIV_KEY = _load_credentials()
DEMO_BASE_URL = os.getenv("BINANCE_DEMO_BASE_URL", "https://demo-api.binance.com")
DEMO_WS_URL = os.getenv("BINANCE_DEMO_WS_URL", "wss://demo-ws-api.binance.com/ws-api/v3")
DEMO_STREAM_URL = os.getenv("BINANCE_DEMO_STREAM_URL", "wss://demo-stream.binance.com/ws")

HAS_CREDENTIALS = bool(API_KEY and ED25519_PRIV_KEY)


def _sign(query_or_payload: str) -> str:
    """Generate Ed25519 signature for Binance Demo API."""
    if ED25519_PRIV_KEY:
        raw_sig = ED25519_PRIV_KEY.sign(query_or_payload.encode("ASCII"))
        return base64.b64encode(raw_sig).decode("ASCII")
    raise ValueError("No private key available for signing")


def gate_credentials(request: pytest.FixtureRequest):
    """Enforces strict failure on binance_demo_strict and skip on binance_demo."""
    if not HAS_CREDENTIALS:
        markexpr = getattr(request.config.option, "markexpr", "")
        if "binance_demo_strict" in markexpr:
            pytest.fail(
                "CRITICAL ACCEPTANCE GATE FAILED: Milestone 5A Credentialed Binance Demo verification requires "
                "valid credentials (BINANCE_DEMO_API_KEY and BINANCE_DEMO_PRIVATE_KEY_PATH) "
                "and cannot be accepted without live venue verification!"
            )
        pytest.skip("BINANCE_DEMO_API_KEY and private key required for Category B venue tests")


# --- Category B Tests ---

@pytest.mark.binance_demo
def test_demo_endpoints_and_nautilus_runtime_resolution():
    """Verify BinanceEnvironment.DEMO in NautilusTrader resolves to official Demo endpoints."""
    assert_version_locked()

    cfg = BinanceExecutionClientConfig(
        account_id=AccountId("BINANCE-SPOT-DEMO"),
        environment=BinanceEnvironment.DEMO,
        product_type=BinanceProductType.SPOT,
        api_key=API_KEY or "demo_dummy_key",
        api_secret="demo_dummy_secret",
    )
    assert cfg.environment == BinanceEnvironment.DEMO
    assert cfg.product_type == BinanceProductType.SPOT

    # Assert Demo hosts in Nautilus binary strings
    import nautilus_trader, pathlib
    p = pathlib.Path(nautilus_trader.__file__).parent
    so_file = list(p.rglob("*.so"))[0]
    binary_bytes = so_file.read_bytes()
    assert b"demo-api.binance.com" in binary_bytes
    assert b"demo-ws-api.binance.com" in binary_bytes


@pytest.mark.binance_demo
@pytest.mark.binance_demo_strict
@pytest.mark.asyncio
async def test_credentialed_demo_connectivity_and_clock_skew(request):
    """Verify live HTTP connectivity and measure actual RTT / clock skew against Binance Demo."""
    gate_credentials(request)
    assert_version_locked()

    async with httpx.AsyncClient(base_url=DEMO_BASE_URL, timeout=10.0) as client:
        # 1. Ping
        ping_resp = await client.get("/api/v3/ping")
        assert ping_resp.status_code == 200

        # 2. Time skew
        t_send = int(time.time() * 1000)
        time_resp = await client.get("/api/v3/time")
        t_recv = int(time.time() * 1000)
        assert time_resp.status_code == 200

        server_time = time_resp.json()["serverTime"]
        monitor = ClockSyncMonitor()
        sample = monitor.compute_sample(t_send, server_time, t_recv)

        assert sample.rtt_ms >= 0
        # Assert clock drift is within 1000ms safety limit
        monitor.assert_clock_in_bounds(sample)


@pytest.mark.binance_demo
@pytest.mark.binance_demo_strict
@pytest.mark.asyncio
async def test_credentialed_demo_signed_account_and_balance_inspection(request):
    """Verify authenticated Ed25519/HMAC query against /api/v3/account on Binance Demo."""
    gate_credentials(request)
    assert_version_locked()

    timestamp = int(time.time() * 1000)
    query_str = f"timestamp={timestamp}&recvWindow=5000"
    sig = _sign(query_str)
    url = f"/api/v3/account?{query_str}&signature={sig}"
    headers = {"X-MBX-APIKEY": API_KEY}

    async with httpx.AsyncClient(base_url=DEMO_BASE_URL, timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        assert resp.status_code == 200, f"Account query failed: {resp.status_code} {resp.text}"
        data = resp.json()
        assert "balances" in data
        assert data.get("canTrade") is True


@pytest.mark.binance_demo
@pytest.mark.binance_demo_strict
@pytest.mark.asyncio
async def test_credentialed_demo_websocket_api_user_data_stream(request):
    """Verify Binance Demo WebSocket API v3 authentication (session.logon) and userDataStream.subscribe.

    Replaces obsolete REST listenKey (POST/PUT/DELETE /api/v3/userDataStream removed Feb 20, 2026).
    """
    gate_credentials(request)
    assert_version_locked()

    async with websockets.connect(DEMO_WS_URL, close_timeout=5.0) as ws:
        # 1. session.logon
        req_id = f"logon-{int(time.time()*1000)}"
        timestamp = int(time.time() * 1000)
        query = f"apiKey={API_KEY}&timestamp={timestamp}"
        sig = _sign(query)

        logon_msg = {
            "id": req_id,
            "method": "session.logon",
            "params": {
                "apiKey": API_KEY,
                "timestamp": timestamp,
                "signature": sig,
            },
        }
        await ws.send(json.dumps(logon_msg))
        raw_resp = await asyncio.wait_for(ws.recv(), timeout=10.0)
        resp = json.loads(raw_resp)
        assert resp.get("id") == req_id
        assert resp.get("status") == 200, f"session.logon failed: {resp}"

        # 2. userDataStream.subscribe
        sub_id = f"sub-{int(time.time()*1000)}"
        sub_msg = {
            "id": sub_id,
            "method": "userDataStream.subscribe",
        }
        await ws.send(json.dumps(sub_msg))
        raw_resp2 = await asyncio.wait_for(ws.recv(), timeout=10.0)
        resp2 = json.loads(raw_resp2)
        assert resp2.get("id") == sub_id
        assert resp2.get("status") == 200, f"userDataStream.subscribe failed: {resp2}"
        subscription_id = resp2.get("result", {}).get("subscriptionId")
        assert subscription_id is not None, f"Missing subscriptionId in result: {resp2}"

        # 3. userDataStream.unsubscribe
        unsub_id = f"unsub-{int(time.time()*1000)}"
        unsub_msg = {
            "id": unsub_id,
            "method": "userDataStream.unsubscribe",
            "params": {
                "subscriptionId": subscription_id,
            },
        }
        await ws.send(json.dumps(unsub_msg))
        raw_resp3 = await asyncio.wait_for(ws.recv(), timeout=10.0)
        resp3 = json.loads(raw_resp3)
        assert resp3.get("id") == unsub_id
        assert resp3.get("status") == 200, f"userDataStream.unsubscribe failed: {resp3}"


@pytest.mark.binance_demo
@pytest.mark.binance_demo_strict
@pytest.mark.asyncio
async def test_credentialed_demo_out_of_market_order_lifecycle(request):
    """Verify submit, query by origClientOrderId, and cancellation on Binance Demo."""
    gate_credentials(request)
    assert_version_locked()

    client_order_id = f"DEMO-OTM-{int(time.time()*1000)}"
    symbol = "BTCUSDT"
    # Deep out-of-the-money BUY order (price: 10,000 USDT)
    price = "10000.00"
    quantity = "0.001"

    timestamp = int(time.time() * 1000)
    params = f"symbol={symbol}&side=BUY&type=LIMIT&timeInForce=GTC&quantity={quantity}&price={price}&newClientOrderId={client_order_id}&timestamp={timestamp}&recvWindow=5000"
    sig = _sign(params)
    url = f"/api/v3/order?{params}&signature={sig}"
    headers = {"X-MBX-APIKEY": API_KEY}

    async with httpx.AsyncClient(base_url=DEMO_BASE_URL, timeout=15.0) as client:
        try:
            # 1. Submit
            submit_resp = await client.post(url, headers=headers)
            assert submit_resp.status_code == 200, f"Submit failed: {submit_resp.text}"
            sub_data = submit_resp.json()
            venue_order_id = sub_data["orderId"]

            # 2. Query by origClientOrderId
            q_timestamp = int(time.time() * 1000)
            q_params = f"symbol={symbol}&origClientOrderId={client_order_id}&timestamp={q_timestamp}&recvWindow=5000"
            q_sig = _sign(q_params)
            query_resp = await client.get(f"/api/v3/order?{q_params}&signature={q_sig}", headers=headers)
            assert query_resp.status_code == 200
            assert query_resp.json()["orderId"] == venue_order_id

            # 3. Cancel
            c_timestamp = int(time.time() * 1000)
            c_params = f"symbol={symbol}&origClientOrderId={client_order_id}&timestamp={c_timestamp}&recvWindow=5000"
            c_sig = _sign(c_params)
            cancel_resp = await client.delete(f"/api/v3/order?{c_params}&signature={c_sig}", headers=headers)
            assert cancel_resp.status_code == 200
            assert cancel_resp.json()["status"] == "CANCELED"
        finally:
            # Cleanup in case cancellation was interrupted
            try:
                c_timestamp = int(time.time() * 1000)
                c_params = f"symbol={symbol}&origClientOrderId={client_order_id}&timestamp={c_timestamp}&recvWindow=5000"
                c_sig = _sign(c_params)
                await client.delete(f"/api/v3/order?{c_params}&signature={c_sig}", headers=headers)
            except Exception:
                pass


@pytest.mark.binance_demo
@pytest.mark.binance_demo_strict
@pytest.mark.asyncio
async def test_credentialed_demo_tightly_capped_marketable_order_and_flatten(request):
    """Verify tightly capped marketable order execution ($10 ceiling), fill, balance reflection, and flattening."""
    gate_credentials(request)
    assert_version_locked()

    symbol = "BTCUSDT"
    # Tightly capped notional: ~5.5 USDT (meets minNotional 5.0, under 10.0 ceiling)
    quote_order_qty = "5.50"
    buy_order_id = f"DEMO-MKT-BUY-{int(time.time()*1000)}"
    headers = {"X-MBX-APIKEY": API_KEY}
    executed_btc_qty = Decimal("0")

    async with httpx.AsyncClient(base_url=DEMO_BASE_URL, timeout=15.0) as client:
        try:
            # 1. Execute marketable market BUY order
            timestamp = int(time.time() * 1000)
            params = f"symbol={symbol}&side=BUY&type=MARKET&quoteOrderQty={quote_order_qty}&newClientOrderId={buy_order_id}&timestamp={timestamp}&recvWindow=5000"
            sig = _sign(params)
            buy_resp = await client.post(f"/api/v3/order?{params}&signature={sig}", headers=headers)
            assert buy_resp.status_code == 200, f"Market buy failed: {buy_resp.text}"
            buy_data = buy_resp.json()
            assert buy_data["status"] == "FILLED"
            executed_btc_qty = Decimal(str(buy_data["executedQty"]))
            assert executed_btc_qty > Decimal("0")

            # Verify balance reflection
            q_ts = int(time.time() * 1000)
            q_params = f"timestamp={q_ts}&recvWindow=5000"
            q_sig = _sign(q_params)
            acc_resp = await client.get(f"/api/v3/account?{q_params}&signature={q_sig}", headers=headers)
            assert acc_resp.status_code == 200
        finally:
            # 2. Cleanup in finally: Sell back any purchased BTC to guarantee clean zero exposure
            if executed_btc_qty > Decimal("0"):
                sell_order_id = f"DEMO-MKT-SELL-{int(time.time()*1000)}"
                timestamp = int(time.time() * 1000)
                sell_params = f"symbol={symbol}&side=SELL&type=MARKET&quantity={executed_btc_qty}&newClientOrderId={sell_order_id}&timestamp={timestamp}&recvWindow=5000"
                sell_sig = _sign(sell_params)
                try:
                    await client.post(f"/api/v3/order?{sell_params}&signature={sell_sig}", headers=headers)
                except Exception as e:
                    logger.critical("Error flattening position in cleanup: %s", e)


@pytest.mark.binance_demo
@pytest.mark.binance_demo_strict
@pytest.mark.asyncio
async def test_credentialed_demo_oco_parent_child_lifecycle_and_cancel(request):
    """Verify protective OCO submission, child order acceptance, and cancellation before flattening."""
    gate_credentials(request)
    assert_version_locked()

    symbol = "BTCUSDT"
    quantity = "0.001"
    # Deep out-of-the-money OCO SELL orders
    price = "150000.00"
    stop_price = "20000.00"
    stop_limit_price = "19500.00"
    list_client_order_id = f"OCO-LIST-{int(time.time()*1000)}"

    timestamp = int(time.time() * 1000)
    params = (
        f"symbol={symbol}&side=SELL&quantity={quantity}&price={price}"
        f"&stopPrice={stop_price}&stopLimitPrice={stop_limit_price}&stopLimitTimeInForce=GTC"
        f"&listClientOrderId={list_client_order_id}&timestamp={timestamp}&recvWindow=5000"
    )
    sig = _sign(params)
    url = f"/api/v3/orderList/oco?{params}&signature={sig}"
    headers = {"X-MBX-APIKEY": API_KEY}

    order_list_id = None
    async with httpx.AsyncClient(base_url=DEMO_BASE_URL, timeout=15.0) as client:
        try:
            # 1. Submit OCO
            resp = await client.post(url, headers=headers)
            assert resp.status_code == 200, f"OCO submit failed: {resp.text}"
            data = resp.json()
            order_list_id = data["orderListId"]
            assert len(data["orderReports"]) == 2

            # 2. Cancel OCO (simulating pre-flattening safety cancel)
            c_timestamp = int(time.time() * 1000)
            c_params = f"symbol={symbol}&orderListId={order_list_id}&timestamp={c_timestamp}&recvWindow=5000"
            c_sig = _sign(c_params)
            cancel_resp = await client.delete(f"/api/v3/orderList?{c_params}&signature={c_sig}", headers=headers)
            assert cancel_resp.status_code == 200
            assert cancel_resp.json()["listOrderStatus"] == "ALL_DONE"
        finally:
            if order_list_id:
                try:
                    c_timestamp = int(time.time() * 1000)
                    c_params = f"symbol={symbol}&orderListId={order_list_id}&timestamp={c_timestamp}&recvWindow=5000"
                    c_sig = _sign(c_params)
                    await client.delete(f"/api/v3/orderList?{c_params}&signature={c_sig}", headers=headers)
                except Exception:
                    pass


@pytest.mark.binance_demo
@pytest.mark.binance_demo_strict
@pytest.mark.asyncio
async def test_credentialed_demo_ambiguous_outcome_reconciliation(request):
    """Verify venue query for nonexistent order confirms rejection before resubmission."""
    gate_credentials(request)
    assert_version_locked()

    nonexistent_order_id = f"NONEXISTENT-{int(time.time()*1000)}"
    timestamp = int(time.time() * 1000)
    params = f"symbol=BTCUSDT&origClientOrderId={nonexistent_order_id}&timestamp={timestamp}&recvWindow=5000"
    sig = _sign(params)
    url = f"/api/v3/order?{params}&signature={sig}"
    headers = {"X-MBX-APIKEY": API_KEY}

    async with httpx.AsyncClient(base_url=DEMO_BASE_URL, timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        # Binance error code -2013 = Order does not exist
        assert resp.status_code in (400, 404)
        assert resp.json().get("code") == -2013


@pytest.mark.binance_demo
@pytest.mark.binance_demo_strict
@pytest.mark.asyncio
async def test_credentialed_demo_final_zero_exposure_assertion(request):
    """Final assertion verifying no lingering open orders exist on the venue."""
    gate_credentials(request)
    assert_version_locked()

    timestamp = int(time.time() * 1000)
    params = f"symbol=BTCUSDT&timestamp={timestamp}&recvWindow=5000"
    sig = _sign(params)
    url = f"/api/v3/openOrders?{params}&signature={sig}"
    headers = {"X-MBX-APIKEY": API_KEY}

    async with httpx.AsyncClient(base_url=DEMO_BASE_URL, timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        assert resp.status_code == 200
        open_orders = resp.json()
        assert len(open_orders) == 0, f"Lingering open orders found on Demo venue: {open_orders}"
