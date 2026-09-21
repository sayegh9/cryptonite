"""Binance Combined WebSocket Stream Client for klines with open-bar buffering."""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import redis.asyncio as aioredis
import websockets
from cryptonite_schemas.bar import RawBinanceKline
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("cryptonite.stream")

BINANCE_WS_STREAM_URL = "wss://stream.binance.com:9443/stream"
BINANCE_FUTURES_WS_STREAM_URL = "wss://fstream.binance.com/stream"


class BinanceStreamClient:
    """Manages real-time WebSocket connection to Binance combined streams."""

    def __init__(
        self,
        symbols: list[str],
        timeframes: list[str],
        on_closed_bar: Callable[[str, str, RawBinanceKline, int], Coroutine[Any, Any, None]] | None = None,
        base_ws_url: str = BINANCE_WS_STREAM_URL,
        redis: aioredis.Redis | None = None,
        enable_quotes: bool = True,
    ) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.timeframes = [t.lower() for t in timeframes]
        self.on_closed_bar = on_closed_bar
        self.base_ws_url = base_ws_url
        self.redis = redis
        self.enable_quotes = enable_quotes

        self._running = False
        self._ws: Any = None
        self.ingest_session_id = uuid.uuid4()
        # In-memory buffer for open-candle updates: symbol -> RawBinanceKline
        self.open_kline_buffer: dict[str, RawBinanceKline] = {}
        # Monotonic update sequence per (symbol, timeframe)
        self.update_sequences: dict[str, int] = {}
        # Monotonic quote sequence per symbol
        self.quote_sequences: dict[str, int] = {}
        # Buffer for closed bars that arrive while reconnecting or backfilling
        self.closed_bar_buffer: asyncio.Queue[tuple[str, str, RawBinanceKline, int]] = asyncio.Queue()
        self.reconnect_count = 0
        self.last_received_ts = 0

    def add_symbol(self, symbol: str) -> None:
        """Add a new symbol to the active subscriptions and force a reconnect."""
        sym = symbol.upper()
        if sym not in self.symbols:
            self.symbols.append(sym)
            logger.info("Dynamically added %s to live stream. Triggering reconnect...", sym)
            if self._ws:
                asyncio.create_task(self._ws.close())

    @property
    def stream_names(self) -> list[str]:
        streams = []
        for s in self.symbols:
            for tf in self.timeframes:
                streams.append(f"{s.lower()}@kline_{tf}")
            if self.enable_quotes:
                streams.append(f"{s.lower()}@bookTicker")
        return streams

    @property
    def stream_url(self) -> str:
        streams_query = "/".join(self.stream_names)
        return f"{self.base_ws_url}?streams={streams_query}"

    async def start(self) -> None:
        """Start the WebSocket listener loop with automatic reconnection."""
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                url = self.stream_url
                logger.info("Connecting to Binance WebSocket streams: %s", url)
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    backoff = 1.0  # Reset backoff on successful connect
                    logger.info("Connected to Binance WebSocket stream")
                    while self._running:
                        msg = await ws.recv()
                        arrival_ns = time.time_ns()
                        self.last_received_ts = arrival_ns
                        await self._handle_message(msg, arrival_ns)
            except (TimeoutError, ConnectionClosed, OSError) as e:
                if not self._running:
                    break
                self.reconnect_count += 1
                logger.warning(
                    "WebSocket disconnected (%s). Reconnecting in %.1fs (reconnects=%d)...",
                    e,
                    backoff,
                    self.reconnect_count,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception as e:
                logger.exception("Unexpected error in WebSocket loop: %s", e)
                await asyncio.sleep(5.0)

    async def _handle_book_ticker(self, payload: dict[str, Any], arrival_ns: int) -> None:
        try:
            symbol = payload["s"].upper()
            bid_p = str(payload["b"])
            bid_q = str(payload["B"])
            ask_p = str(payload["a"])
            ask_q = str(payload["A"])
            inst_str = f"{symbol}.BINANCE"
            seq = self.quote_sequences.get(symbol, 0) + 1
            self.quote_sequences[symbol] = seq

            if self.redis:
                from nautilus_trader.model import (
                    InstrumentId,
                    Price,
                    Quantity,
                    QuoteTick,
                )

                quote = QuoteTick(
                    instrument_id=InstrumentId.from_str(inst_str),
                    bid_price=Price.from_str(bid_p),
                    ask_price=Price.from_str(ask_p),
                    bid_size=Quantity.from_str(bid_q),
                    ask_size=Quantity.from_str(ask_q),
                    ts_event=arrival_ns,
                    ts_init=arrival_ns,
                )
                bus_record = {
                    "topic": f"data.quotes.BINANCE.{symbol}",
                    "type": "QuoteTick",
                    "payload": quote.to_json_bytes().decode("utf-8"),
                    "encoding": "JSON",
                }
                try:
                    health_record = {
                        "instrument_id": inst_str,
                        "ingest_session_id": str(self.ingest_session_id),
                        "publish_sequence": seq,
                        "event_ts_ns": arrival_ns,
                        "arrival_ts_ns": arrival_ns,
                    }
                    ui_record = {
                        "instrument_id": inst_str,
                        "bid_price": bid_p,
                        "ask_price": ask_p,
                        "bid_size": bid_q,
                        "ask_size": ask_q,
                        "arrival_ns": arrival_ns,
                    }
                    async with self.redis.pipeline(transaction=False) as pipe:
                        pipe.xadd("nautilus:stream:bus", bus_record, maxlen=5000, approximate=True)
                        pipe.xadd("cryptonite:stream:ingress_health", {"data": json.dumps(health_record)}, maxlen=5000, approximate=True)
                        pipe.xadd("cryptonite:stream:ui_quote_tick", {"data": json.dumps(ui_record)}, maxlen=1000, approximate=True)
                        await pipe.execute()
                except Exception as ex:
                    logger.debug("Failed to publish QuoteTick pipeline: %s", ex)
        except Exception as e:
            logger.error("Error processing bookTicker message: %s", e)

    async def _handle_message(self, raw_msg: str | bytes, arrival_ns: int) -> None:
        try:
            data = json.loads(raw_msg)
            payload = data.get("data", data)
            if not isinstance(payload, dict):
                return

            if payload.get("b") is not None and payload.get("a") is not None and payload.get("s") is not None and "k" not in payload:
                await self._handle_book_ticker(payload, arrival_ns)
                return

            if payload.get("e") != "kline" or "k" not in payload:
                return

            k = payload["k"]
            symbol = payload["s"]
            timeframe = k["i"]
            is_closed = bool(k["x"])

            raw_kline = RawBinanceKline(
                open_time_ms=int(k["t"]),
                open=str(k["o"]),
                high=str(k["h"]),
                low=str(k["l"]),
                close=str(k["c"]),
                volume=str(k["v"]),
                close_time_ms=int(k["T"]),
                quote_volume=str(k["q"]),
                trades_count=int(k["n"]),
                taker_buy_base_volume=str(k["V"]),
                taker_buy_quote_volume=str(k["Q"]),
                is_closed=is_closed,
            )

            # Store in open kline buffer
            buffer_key = f"{symbol}:{timeframe}"
            self.open_kline_buffer[buffer_key] = raw_kline

            # Increment monotonic sequence counter
            seq = self.update_sequences.get(buffer_key, 0) + 1
            self.update_sequences[buffer_key] = seq

            # Direct lossy publication to Redis open_kline stream
            if self.redis:
                duration_ms = raw_kline.close_time_ms - raw_kline.open_time_ms + 1
                bar_open_ns = raw_kline.open_time_ms * 1_000_000
                bar_close_inclusive_ns = raw_kline.close_time_ms * 1_000_000
                interval_end_exclusive_ns = (raw_kline.open_time_ms + duration_ms) * 1_000_000

                open_payload = {
                    "schema_version": 1,
                    "producer": "cryptonite-ingest",
                    "occurred_at_ns": arrival_ns,
                    "event_id": str(uuid.uuid4()),
                    "event_type": "open_kline",
                    "instrument_id": f"{symbol.upper()}.BINANCE",
                    "timeframe": timeframe,
                    "update_sequence": seq,
                    "bar_open_ns": bar_open_ns,
                    "bar_close_inclusive_ns": bar_close_inclusive_ns,
                    "interval_end_exclusive_ns": interval_end_exclusive_ns,
                    "open": raw_kline.open,
                    "high": raw_kline.high,
                    "low": raw_kline.low,
                    "close": raw_kline.close,
                    "volume": raw_kline.volume,
                    "quote_volume": raw_kline.quote_volume,
                    "trades_count": raw_kline.trades_count,
                    "is_closed": is_closed,
                }
                try:
                    await self.redis.xadd(
                        "cryptonite:stream:open_kline",
                        {"event_id": open_payload["event_id"], "data": json.dumps(open_payload)},
                        maxlen=1000,
                        approximate=True,
                    )
                except Exception as ex:
                    logger.debug("Failed to publish open tick to Redis: %s", ex)

            if is_closed:
                logger.debug("Received closed kline for %s (%s) close_time=%d", symbol, timeframe, raw_kline.close_time_ms)
                # Put in internal queue
                await self.closed_bar_buffer.put((symbol, timeframe, raw_kline, arrival_ns))
                if self.on_closed_bar:
                    await self.on_closed_bar(symbol, timeframe, raw_kline, arrival_ns)

        except Exception as e:
            logger.error("Error processing stream message: %s", e)

    async def stop(self) -> None:
        """Stop the WebSocket client."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("Stopped Binance WebSocket client")
