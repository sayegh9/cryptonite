"""Binance Demo WebSocket API v3 User-Data Stream Client.

Pinnned exclusively to Binance Demo WebSocket API:
  wss://demo-ws-api.binance.com/ws-api/v3

Protocol Flow:
  1. Connect to demo-ws-api
  2. session.logon (Ed25519 signature)
  3. userDataStream.subscribe (verifying subscriptionId)
  4. Parse envelope: {"subscriptionId": <int>, "event": {...}}
  5. Dispatch executionReport, outboundAccountPosition, listStatus to pipeline
  6. Manage 24-hour connection renewal and graceful resubscription
  7. Handle serverShutdown, eventStreamTerminated, disconnects with exponential backoff
  8. Enforce mandatory authoritative reconciliation BEFORE reopening submission gate
"""

import asyncio
import base64
import json
import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Callable, Coroutine, Dict, Optional

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
import websockets

logger = logging.getLogger("cryptonite.execution.binance_user_stream")

DEMO_WS_API_URL = "wss://demo-ws-api.binance.com/ws-api/v3"


class BinanceDemoWebSocketApiClient:
    """Ed25519 authenticated WebSocket API client for Binance Demo user data streams."""

    def __init__(
        self,
        api_key: str,
        private_key_pem: bytes,
        ws_url: str = DEMO_WS_API_URL,
        on_execution_report: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, bool]]] = None,
        on_balance_update: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]] = None,
        on_order_list_update: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]] = None,
        reconcile_hook: Optional[Callable[[], Coroutine[Any, Any, bool]]] = None,
        gate_controller: Optional[Any] = None,
        renewal_interval_secs: int = 82800,  # 23 hours
    ):
        self.api_key = api_key
        self._private_key: ed25519.Ed25519PrivateKey = serialization.load_pem_private_key(
            private_key_pem, password=None
        )
        self.ws_url = ws_url
        self.on_execution_report = on_execution_report
        self.on_balance_update = on_balance_update
        self.on_order_list_update = on_order_list_update
        self.reconcile_hook = reconcile_hook
        self.gate_controller = gate_controller
        self.renewal_interval_secs = renewal_interval_secs

        self.ws: Optional[Any] = None
        self.subscription_id: Optional[int] = None
        self.is_connected = False
        self.is_subscribed = False
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None
        self._renewal_task: Optional[asyncio.Task] = None
        self._pending_requests: Dict[str, asyncio.Future] = {}

    def _sign_payload(self, query_string: str) -> str:
        """Signs query string with Ed25519 private key and returns base64 signature."""
        sig = self._private_key.sign(query_string.encode("utf-8"))
        return base64.b64encode(sig).decode("utf-8")

    async def start(self) -> None:
        """Start WebSocket connection and listener loop."""
        self._running = True
        self._loop_task = asyncio.create_task(self._run_connection_lifecycle())

    async def stop(self) -> None:
        """Gracefully disconnect and cancel tasks."""
        self._running = False
        if self._renewal_task:
            self._renewal_task.cancel()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            await self.ws.close()
        self.is_connected = False
        self.is_subscribed = False
        if self.gate_controller:
            self.gate_controller.is_gate_open = False

    async def _run_connection_lifecycle(self) -> None:
        """Supervises connection, auto-reconnect, and resubscription."""
        backoff = 1.0
        while self._running:
            try:
                if self.gate_controller:
                    self.gate_controller.is_gate_open = False
                self.is_connected = False
                self.is_subscribed = False

                logger.info("Connecting to Binance Demo WebSocket API: %s", self.ws_url)
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10) as ws:
                    self.ws = ws
                    self.is_connected = True
                    backoff = 1.0

                    reader_task = asyncio.create_task(self._reader_loop(ws))
                    try:
                        # 1. session.logon
                        await self._logon()

                        # 2. userDataStream.subscribe
                        sub_id = await self._subscribe_user_data()
                        self.subscription_id = sub_id
                        self.is_subscribed = True
                        logger.info("Subscribed to Demo user data stream. subscriptionId=%s", sub_id)

                        # 3. Reconnection Authoritative Reconciliation Check
                        if self.reconcile_hook:
                            logger.info("Triggering mandatory post-connect authoritative reconciliation...")
                            reconcile_ok = await self.reconcile_hook()
                            if not reconcile_ok:
                                logger.critical("Reconciliation check failed post-reconnect! Keeping gate closed.")
                                if self.gate_controller:
                                    self.gate_controller.is_gate_open = False
                            else:
                                logger.info("Reconciliation check passed. Reopening order submission gate.")
                                if self.gate_controller:
                                    self.gate_controller.is_gate_open = True
                        elif self.gate_controller:
                            self.gate_controller.is_gate_open = True

                        # 4. Schedule 24h renewal task
                        if self._renewal_task:
                            self._renewal_task.cancel()
                        self._renewal_task = asyncio.create_task(self._renewal_timer())

                        # 5. Await reader loop until connection closes
                        await reader_task
                    finally:
                        reader_task.cancel()
                        try:
                            await reader_task
                        except (asyncio.CancelledError, Exception):
                            pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("WebSocket connection dropped or failed: %s. Reconnecting in %.1fs...", e, backoff)
                if self.gate_controller:
                    self.gate_controller.is_gate_open = False
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)

    async def _reader_loop(self, ws) -> None:
        """Continuously reads messages from WebSocket and dispatches to handler."""
        async for raw_msg in ws:
            await self._handle_raw_message(raw_msg)

    async def _renew_connection(self) -> None:
        """Gracefully close current connection to trigger renewal reconnect."""
        logger.info("Triggering WebSocket connection renewal...")
        if self.ws:
            await self.ws.close()

    async def _renewal_timer(self) -> None:
        """Periodically triggers connection reconnect before 24-hour limit."""
        try:
            await asyncio.sleep(self.renewal_interval_secs)
            logger.info("24-hour connection renewal interval reached. Reconnecting WebSocket gracefully...")
            await self._renew_connection()
        except asyncio.CancelledError:
            pass

    async def _send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send a request over WebSocket API and await matching response."""
        req_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending_requests[req_id] = future

        msg = {"id": req_id, "method": method}
        if params:
            msg["params"] = params

        await self.ws.send(json.dumps(msg))
        try:
            resp = await asyncio.wait_for(future, timeout=10.0)
            return resp
        finally:
            self._pending_requests.pop(req_id, None)

    async def _logon(self) -> None:
        """Authenticate session using Ed25519 signature."""
        timestamp_ms = int(time.time() * 1000)
        query_str = f"apiKey={self.api_key}&timestamp={timestamp_ms}"
        signature = self._sign_payload(query_str)

        resp = await self._send_request(
            method="session.logon",
            params={
                "apiKey": self.api_key,
                "timestamp": timestamp_ms,
                "signature": signature,
            },
        )
        if resp.get("status") != 200:
            error = resp.get("error", {})
            raise RuntimeError(f"session.logon failed: status={resp.get('status')}, error={error}")
        logger.info("Binance Demo session.logon successful.")

    async def _subscribe_user_data(self) -> int:
        """Subscribes to user data stream and retrieves subscriptionId."""
        resp = await self._send_request(method="userDataStream.subscribe")
        if resp.get("status") != 200:
            error = resp.get("error", {})
            raise RuntimeError(f"userDataStream.subscribe failed: status={resp.get('status')}, error={error}")
        result = resp.get("result", {})
        subscription_id = result.get("subscriptionId", 0)
        return int(subscription_id)

    async def _handle_raw_message(self, raw_msg: str) -> None:
        """Parses envelope and dispatches responses or events."""
        try:
            data = json.loads(raw_msg)
        except Exception as e:
            logger.error("Failed decoding WebSocket message: %s", e)
            return

        # Handle request-response correlation
        req_id = data.get("id")
        if req_id and req_id in self._pending_requests:
            fut = self._pending_requests[req_id]
            if not fut.done():
                fut.set_result(data)
            return

        # Handle stream events with envelope: {"subscriptionId": <int>, "event": {...}}
        event = data.get("event")
        if not event:
            # Check for direct event payload or error
            event_type = data.get("e")
            if event_type:
                event = data
            else:
                return

        event_type = event.get("e")
        if not event_type:
            return

        # Check server shutdown / terminated events
        if event_type in ("serverShutdown", "eventStreamTerminated"):
            logger.warning("Received %s event from Binance Demo. Forcing reconnect...", event_type)
            if self.gate_controller:
                self.gate_controller.is_gate_open = False
            if self.ws:
                await self.ws.close()
            return

        if event_type == "executionReport":
            await self._dispatch_execution_report(event)
        elif event_type == "outboundAccountPosition":
            await self._dispatch_account_position(event)
        elif event_type == "listStatus":
            await self._dispatch_list_status(event)

    async def _dispatch_execution_report(self, event: Dict[str, Any]) -> None:
        """Transform Binance executionReport to Cryptonite pipeline report."""
        if not self.on_execution_report:
            return

        trade_id = event.get("t", -1)
        report = {
            "event_type": "ORDER_FILLED" if event.get("X") in ("FILLED", "PARTIALLY_FILLED") else "ORDER_ACCEPTED",
            "source_event_id": f"binance-exec-{event.get('E')}-{event.get('i')}-{trade_id}",
            "trade_id": trade_id if trade_id != -1 else None,
            "client_order_id": event.get("c"),
            "venue_order_id": str(event.get("i", "")),
            "instrument_id": f"{event.get('s')}.BINANCE",
            "status": self._normalize_status(event.get("X")),
            "raw_venue_status": event.get("X"),
            "side": event.get("S"),
            "price": Decimal(str(event.get("L", event.get("p", "0")))),
            "quantity": Decimal(str(event.get("q", "0"))),
            "quote_quantity": Decimal(str(event.get("Y", event.get("Z", "0")))),
            "filled_qty": Decimal(str(event.get("z", "0"))),
            "commission_amount": Decimal(str(event.get("n", "0"))),
            "commission_asset": event.get("N") or "USDT",
            "is_maker": event.get("m", False),
            "occurred_at_ns": int(event.get("E", time.time() * 1000)) * 1_000_000,
        }
        await self.on_execution_report(report)

    async def _dispatch_account_position(self, event: Dict[str, Any]) -> None:
        """Handle outboundAccountPosition balance updates."""
        if self.on_balance_update:
            await self.on_balance_update(event)

    async def _dispatch_list_status(self, event: Dict[str, Any]) -> None:
        """Handle listStatus OCO updates."""
        if self.on_order_list_update:
            await self.on_order_list_update(event)

    @staticmethod
    def _normalize_status(venue_status: Optional[str]) -> str:
        mapping = {
            "NEW": "ACCEPTED",
            "PARTIALLY_FILLED": "PARTIALLY_FILLED",
            "FILLED": "FILLED",
            "CANCELED": "CANCELED",
            "PENDING_CANCEL": "PENDING_CANCEL",
            "REJECTED": "REJECTED",
            "EXPIRED": "EXPIRED",
        }
        return mapping.get(venue_status, "SUBMITTED")
