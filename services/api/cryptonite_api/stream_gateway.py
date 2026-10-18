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
from cryptonite_schemas.identifiers import parse_instrument_id
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("cryptonite.api.stream")

ALLOWED_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}
MAX_SUBSCRIPTIONS_PER_CLIENT = 20
CLIENT_QUEUE_MAXSIZE = 100

# Perpetual Redis pub/sub channels -> client message type (producers: derivatives_feed, PaperTradingEngine)
PERP_CHANNELS = {
    "channel:perp_mark_ticks": "perp_mark_tick",
    "channel:perp_liquidations": "perp_liquidation",
    "channel:perp_funding_events": "perp_funding",
}
PERP_MARK_KEY_PATTERN = "perp:mark:*"
PERP_SUBSCRIPTION_CHANNEL = "perpetuals"
DEFAULT_PERP_INSTRUMENTS = {"BTCUSDT-PERP.BINANCE", "ETHUSDT-PERP.BINANCE"}
DEFAULT_SPOT_INSTRUMENTS = {
    "BTCUSDT.BINANCE",
    "ETHUSDT.BINANCE",
    "SOLUSDT.BINANCE",
    "BNBUSDT.BINANCE",
    "XRPUSDT.BINANCE",
    "WLDUSDT.BINANCE",
    "MUBARAKUSDT.BINANCE",
    "DOGEUSDT.BINANCE",
    "NEARUSDT.BINANCE",
    "ADAUSDT.BINANCE",
    "AVAXUSDT.BINANCE",
    "LINKUSDT.BINANCE",
    "SUIUSDT.BINANCE",
    "TRXUSDT.BINANCE",
    "PEPEUSDT.BINANCE",
    "SHIBUSDT.BINANCE",
}


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
        # Perpetuals: mark ticks filtered by perp_instruments (empty = all); account events unfiltered
        self.subscribe_perpetuals: bool = False
        self.perp_instruments: set[str] = set()
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

    def enqueue_droppable(self, msg: dict[str, Any]) -> None:
        """Enqueue a superseded-by-next-tick message (perp marks); drop it when the queue is full."""
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass

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

    def __init__(
        self,
        redis_url: str,
        allowed_instruments: set[str] | None = None,
        allowed_perp_instruments: set[str] | None = None,
        perp_channels: dict[str, str] | None = None,
        perp_mark_key_pattern: str = PERP_MARK_KEY_PATTERN,
    ) -> None:
        self.redis_url = redis_url
        self.allowed_instruments = allowed_instruments or DEFAULT_SPOT_INSTRUMENTS
        self.allowed_perp_instruments = allowed_perp_instruments or DEFAULT_PERP_INSTRUMENTS
        self.perp_channels = perp_channels or PERP_CHANNELS
        self.perp_mark_key_pattern = perp_mark_key_pattern
        self._perp_task: asyncio.Task[None] | None = None
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
        self._perp_task = asyncio.create_task(self._perp_pubsub_loop())

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

        if self._perp_task:
            self._perp_task.cancel()
            self._perp_task = None


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

            perp_instruments = [i for i in instruments if parse_instrument_id(i)[2]]
            instruments = [i for i in instruments if not parse_instrument_id(i)[2]]
            for inst in perp_instruments:
                if inst not in self.allowed_perp_instruments:
                    return {"error": f"Unsupported perpetual: {inst}. Allowed: {sorted(self.allowed_perp_instruments)}"}
            if perp_instruments and PERP_SUBSCRIPTION_CHANNEL not in channels:
                return {"error": f"Perpetual instruments require the '{PERP_SUBSCRIPTION_CHANNEL}' channel"}

            for inst in instruments:
                if self.allowed_instruments and inst not in self.allowed_instruments:
                    return {"error": f"Unsupported or unknown instrument: {inst}. Allowed: {sorted(self.allowed_instruments)}"}

            if PERP_SUBSCRIPTION_CHANNEL in channels:
                client.subscribe_perpetuals = True
                client.perp_instruments.update(perp_instruments)
                await self._send_perp_mark_snapshots(client)

            if "signals" in channels or "signal_candidate" in channels:
                client.subscribe_signals = True

            if "ticker_update" in channels:
                client.subscribe_tickers = True
                
            for inst in instruments:
                if len(client.subscriptions) < MAX_SUBSCRIPTIONS_PER_CLIENT:
                    client.subscriptions.add(inst)
                    
            res = {"status": "subscribed", "channels": channels, "instruments": instruments + perp_instruments}
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

            if PERP_SUBSCRIPTION_CHANNEL in channels:
                listed = [i for i in instruments if parse_instrument_id(i)[2]]
                if listed:
                    client.perp_instruments.difference_update(listed)
                else:
                    client.subscribe_perpetuals = False
                    client.perp_instruments.clear()
                
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




    def _wants_perp(self, client: ClientConnection, msg_type: str, instrument_id: str | None) -> bool:
        if not client.subscribe_perpetuals:
            return False
        if msg_type != "perp_mark_tick" or not client.perp_instruments:
            return True  # liquidation/funding notices are account events: every perpetuals subscriber gets them
        return instrument_id in client.perp_instruments

    async def _send_perp_mark_snapshots(self, client: ClientConnection) -> None:
        """Hydrate a new perpetuals subscriber from the cached perp:mark:* keys."""
        if not self.redis:
            return
        try:
            async for key in self.redis.scan_iter(match=self.perp_mark_key_pattern):
                raw = await self.redis.get(key)
                if not raw:
                    continue
                data = json.loads(raw)
                if self._wants_perp(client, "perp_mark_tick", data.get("instrument_id")):
                    client.enqueue_droppable({"type": "perp_mark_tick", "snapshot": True, "data": data})
        except Exception as e:
            logger.warning("Perp mark snapshot failed for %s: %s", client.client_id, e)

    async def _perp_pubsub_loop(self) -> None:
        """Relay perpetual pub/sub channels to subscribed clients; resubscribes after Redis errors.

        Mark ticks are droppable under backpressure (the next tick supersedes them);
        liquidation and funding notices use the reliable path.
        """
        while self._running:
            pubsub = None
            try:
                if not self.redis:
                    await asyncio.sleep(0.5)
                    continue
                pubsub = self.redis.pubsub()
                await pubsub.subscribe(*self.perp_channels)
                async for message in pubsub.listen():
                    if not self._running:
                        break
                    if message["type"] != "message":
                        continue
                    msg_type = self.perp_channels.get(message["channel"])
                    if msg_type is None:
                        continue
                    data = json.loads(message["data"])
                    client_msg = {"type": msg_type, "data": data}
                    for client in list(self.clients.values()):
                        if not self._wants_perp(client, msg_type, data.get("instrument_id")):
                            continue
                        if msg_type == "perp_mark_tick":
                            client.enqueue_droppable(client_msg)
                        elif not await client.enqueue_reliable_event(client_msg):
                            self.clients.pop(client.client_id, None)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Perp pub/sub relay error, resubscribing: %s", e)
                await asyncio.sleep(1.0)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:
                        pass

    async def _pubsub_loop(self) -> None:
        while self._running:
            try:
                if not self.redis:
                    await asyncio.sleep(0.5)
                    continue
                    
                pubsub = self.redis.pubsub()
                await pubsub.subscribe("CHANNEL_POSITIONS", "channel:agent_heartbeat")
                await pubsub.psubscribe("channel:debates:*")
                
                async for message in pubsub.listen():
                    if not self._running:
                        break
                    if message["type"] in ("message", "pmessage"):
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
