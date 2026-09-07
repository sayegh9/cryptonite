"""Binance Combined WebSocket Stream Client for klines with open-bar buffering."""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

import websockets
from cryptonite_schemas.bar import RawBinanceKline
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("cryptonite.stream")

BINANCE_WS_STREAM_URL = "wss://stream.binance.com:9443/stream"


class BinanceStreamClient:
    """Manages real-time WebSocket connection to Binance combined streams."""

    def __init__(
        self,
        symbols: list[str],
        timeframes: list[str],
        on_closed_bar: Callable[[str, str, RawBinanceKline, int], Coroutine[Any, Any, None]] | None = None,
        base_ws_url: str = BINANCE_WS_STREAM_URL,
    ) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.timeframes = [t.lower() for t in timeframes]
        self.on_closed_bar = on_closed_bar
        self.base_ws_url = base_ws_url

        self._running = False
        self._ws: Any = None
        # In-memory buffer for open-candle updates: symbol -> RawBinanceKline
        self.open_kline_buffer: dict[str, RawBinanceKline] = {}
        # Buffer for closed bars that arrive while reconnecting or backfilling
        self.closed_bar_buffer: asyncio.Queue[tuple[str, str, RawBinanceKline, int]] = asyncio.Queue()
        self.reconnect_count = 0
        self.last_received_ts = 0

    @property
    def stream_names(self) -> list[str]:
        streams = []
        for s in self.symbols:
            for tf in self.timeframes:
                streams.append(f"{s.lower()}@kline_{tf}")
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

    async def _handle_message(self, raw_msg: str | bytes, arrival_ns: int) -> None:
        try:
            data = json.loads(raw_msg)
            if "data" not in data or data.get("data", {}).get("e") != "kline":
                return

            payload = data["data"]
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
