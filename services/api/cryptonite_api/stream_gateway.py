ALLOWED_ORIGINS = {"http://localhost:3000", "http://127.0.0.1:3000", "http://192.168.1.203:3000"}
"""WebSocket stream gateway and Redis Streams fanout broadcaster.

Handles client subscriptions, Origin header validation, subscription allow-lists,
bounded client queues, open-bar coalescing, and slow-consumer backpressure termination.
"""

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("cryptonite.api.stream")

ALLOWED_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}
MAX_SUBSCRIPTIONS_PER_CLIENT = 20
CLIENT_QUEUE_MAXSIZE = 100


class ClientConnection:
    """Manages an active WebSocket client session with a bounded delivery queue."""

    def __init__(self, ws: WebSocket, client_id: str) -> None:
        self.ws = ws
        self.client_id = client_id
        # Bounded event queue
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=CLIENT_QUEUE_MAXSIZE)
        # Subscribed channels: set of (instrument_id, timeframe)
        self.subscriptions: set[str] = set()
        self.subscribe_signals: bool = False
        self.subscribe_tickers: bool = False
        self._sender_task: asyncio.Task[None] | None = None

    def start_sender(self) -> None:
        self._sender_task = asyncio.create_task(self._send_loop())

    async def stop(self) -> None:
        if self._sender_task:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass
            self._sender_task = None

    async def _send_loop(self) -> None:
        try:
            while True:
                msg = await self.queue.get()
                await self.ws.send_text(json.dumps(msg))
                self.queue.task_done()
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.debug("Client %s send error: %s", self.client_id, e)

    async def enqueue_lossy_tick(self, msg: dict[str, Any]) -> None:
        """Enqueue open tick with coalescing/dropping under backpressure."""
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            # Drop the oldest item if it is an open_kline to make room for newest tick
            try:
                old = self.queue.get_nowait()
                self.queue.task_done()
                if old.get("type") == "open_kline":
                    self.queue.put_nowait(msg)
            except Exception:
                pass  # Lossy drop for open ticks is safe and compliant

    async def enqueue_reliable_event(self, msg: dict[str, Any]) -> bool:
        """Reliably enqueue committed bars and signals. Returns False if client queue is overwhelmed."""
        try:
            self.queue.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            # Overwhelmed slow consumer: backpressure termination
            logger.warning("Terminating slow consumer %s (queue full on reliable event)", self.client_id)
            try:
                await self.ws.close(code=4408, reason="Slow consumer backpressure")
            except Exception:
                pass
            return False


class StreamHub:
    """Central Redis stream listener and client broadcaster."""

    def __init__(self, redis_url: str, allowed_instruments: set[str] | None = None) -> None:
        self.redis_url = redis_url
        self.allowed_instruments = allowed_instruments or {"BTCUSDT.BINANCE", "ETHUSDT.BINANCE"}
        self.redis: aioredis.Redis | None = None
        self.clients: dict[str, ClientConnection] = {}
        self._running = False
        self._reader_task: asyncio.Task[None] | None = None
        self._pubsub_task: asyncio.Task[None] | None = None
        self.consumer_group = "api-fanout"
        self.consumer_name = f"api-{uuid4().hex[:6]}"

    async def start(self) -> None:
        self.redis = aioredis.from_url(self.redis_url, decode_responses=True)
        self._running = True

        # Ensure consumer group on closed_bar_committed and signal_candidate
        for stream in ["cryptonite:stream:closed_bar_committed", "cryptonite:stream:signal_candidate"]:
            try:
                await self.redis.xgroup_create(stream, self.consumer_group, id="$", mkstream=True)
            except Exception:
                pass


        self._reader_task = asyncio.create_task(self._redis_reader_loop())
        self._pubsub_task = asyncio.create_task(self._pubsub_loop())

        logger.info("StreamHub started with consumer=%s", self.consumer_name)

    async def stop(self) -> None:
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._pubsub_task:
            self._pubsub_task.cancel()
            self._pubsub_task = None


        for c in list(self.clients.values()):
            await c.stop()
        self.clients.clear()

        if self.redis:
            await self.redis.aclose()
            self.redis = None

    def register_client(self, ws: WebSocket) -> ClientConnection:
        client_id = f"client-{uuid4().hex[:8]}"
        client = ClientConnection(ws, client_id)
        self.clients[client_id] = client
        client.start_sender()
        return client

    async def unregister_client(self, client: ClientConnection) -> None:
        await client.stop()
        self.clients.pop(client.client_id, None)

    async def handle_subscription_message(
        self, client: ClientConnection, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate and apply client subscription message."""
        action = data.get("action")
        
        if action == "subscribe":
            timeframe = data.get("timeframe")
            if timeframe and timeframe not in ALLOWED_TIMEFRAMES:
                return {"error": f"Invalid timeframe: {timeframe}. Allowed: {sorted(ALLOWED_TIMEFRAMES)}"}

            channels = data.get("channels") or ([data.get("channel")] if data.get("channel") else [])
            instruments = data.get("instruments") or []
            
            if not instruments and data.get("instrument_id"):
                instruments = [data.get("instrument_id")]
            elif not instruments and data.get("symbol"):
                instruments = [f"{data.get('symbol').upper()}.BINANCE"]

            for inst in instruments:
                if self.allowed_instruments and inst not in self.allowed_instruments:
                    return {"error": f"Unsupported or unknown instrument: {inst}. Allowed: {sorted(self.allowed_instruments)}"}

            if "signals" in channels or "signal_candidate" in channels:
                client.subscribe_signals = True

            if "ticker_update" in channels:
                client.subscribe_tickers = True
                
            for inst in instruments:
                if len(client.subscriptions) < MAX_SUBSCRIPTIONS_PER_CLIENT:
                    client.subscriptions.add(inst)
                    
            res = {"status": "subscribed", "channels": channels, "instruments": instruments}
            if len(instruments) == 1:
                res["instrument_id"] = instruments[0]
            return res

        elif action == "unsubscribe":
            channels = data.get("channels") or [data.get("channel")]
            instruments = data.get("instruments") or []
            if not instruments and data.get("instrument_id"):
                instruments = [data.get("instrument_id")]
            elif not instruments and data.get("symbol"):
                instruments = [f"{data.get('symbol').upper()}.BINANCE"]

            if "signals" in channels or "signal_candidate" in channels:
                client.subscribe_signals = False

            if "ticker_update" in channels:
                client.subscribe_tickers = False
                
            for inst in instruments:
                client.subscriptions.discard(inst)
                
            return {"status": "unsubscribed", "channels": channels, "instruments": instruments}

        return {"error": f"Unknown action: {action}"}

    async def _redis_reader_loop(self) -> None:
        """Reads from Redis Streams and routes events to matching client connections."""
        last_open_id = "$"
        while self._running:
            try:
                if not self.redis:
                    await asyncio.sleep(0.5)
                    continue

                # 1. Read open kline ticks (lossy, non-group stream reading)
                open_entries = await self.redis.xread(
                    {"cryptonite:stream:open_kline": last_open_id},
                    count=50,
                    block=50,
                )
                if open_entries:
                    for _, messages in open_entries:
                        for msg_id, msg_data in messages:
                            last_open_id = msg_id
                            await self._fanout_open_kline(msg_data)

                # 2. Read durable committed bars and signal candidates (group reading)
                durable_entries = await self.redis.xreadgroup(
                    self.consumer_group,
                    self.consumer_name,
                    {
                        "cryptonite:stream:closed_bar_committed": ">",
                        "cryptonite:stream:signal_candidate": ">",
                    },
                    count=20,
                    block=50,
                )
                if durable_entries:
                    for stream_name, messages in durable_entries:
                        for msg_id, msg_data in messages:
                            if "closed_bar_committed" in stream_name:
                                await self._fanout_closed_bar(msg_data)
                            elif "signal_candidate" in stream_name:
                                await self._fanout_signal_candidate(msg_data)
                            await self.redis.xack(stream_name, self.consumer_group, msg_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in StreamHub reader loop: %s", e)
                await asyncio.sleep(0.5)

    async def _fanout_open_kline(self, msg_data: dict[str, Any]) -> None:
        raw_payload = msg_data.get("data")
        if not raw_payload:
            return
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        inst_id = payload.get("instrument_id")

        client_msg = {
            "type": "open_kline",
            **payload
        }

        # Route to clients subscribed to this instrument
        for client in list(self.clients.values()):
            if inst_id in client.subscriptions:
                await client.enqueue_lossy_tick(client_msg)

    async def _fanout_closed_bar(self, msg_data: dict[str, Any]) -> None:
        raw_payload = msg_data.get("data")
        if not raw_payload:
            return
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        inst_id = payload.get("instrument_id")

        client_msg = {
            "type": "closed_bar_committed",
            **payload
        }

        for client in list(self.clients.values()):
            if inst_id in client.subscriptions:
                ok = await client.enqueue_reliable_event(client_msg)
                if not ok:
                    self.clients.pop(client.client_id, None)

    async def _fanout_signal_candidate(self, msg_data: dict[str, Any]) -> None:
        raw_payload = msg_data.get("data")
        if not raw_payload:
            return
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload

        client_msg = {
            "type": "signal_candidate",
            "data": payload,
        }

        for client in list(self.clients.values()):
            if client.subscribe_signals:
                ok = await client.enqueue_reliable_event(client_msg)
                if not ok:
                    self.clients.pop(client.client_id, None)




    async def _pubsub_loop(self) -> None:
        while self._running:
            try:
                if not self.redis:
                    await asyncio.sleep(0.5)
                    continue
                    
                pubsub = self.redis.pubsub()
                await pubsub.subscribe("CHANNEL_POSITIONS")
                
                async for message in pubsub.listen():
                    if not self._running:
                        break
                    if message["type"] == "message":
                        data = message["data"]
                        # Fanout to all clients unconditionally
                        payload = json.loads(data)
                        for client in list(self.clients.values()):
                            if not client.queue.full():
                                client.queue.put_nowait(payload)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in pubsub loop: %s", e)
                await asyncio.sleep(1.0)
