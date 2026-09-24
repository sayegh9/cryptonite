"""Ingest publishes USD-M perpetual bookTicker quotes under <SYM>-PERP.BINANCE (never the spot id)."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from cryptonite_ingest.stream import (
    BINANCE_FUTURES_WS_QUOTE_STREAM_URL,
    BINANCE_FUTURES_WS_STREAM_URL,
    BinanceStreamClient,
)

BOOK_TICKER = {"e": "bookTicker", "s": "BTCUSDT", "b": "50000.10", "B": "1.5", "a": "50000.20", "A": "2.0"}


def recording_redis():
    """Fake redis whose pipeline records xadd calls as {stream: [decoded fields...]}."""
    written: dict[str, list[dict]] = {}
    pipe = MagicMock()
    pipe.xadd.side_effect = lambda stream, fields, **_: written.setdefault(stream, []).append(fields)
    pipe.execute = AsyncMock()

    @asynccontextmanager
    async def pipeline(transaction=False):
        yield pipe

    redis = MagicMock()
    redis.pipeline = pipeline
    return redis, written


def perp_quote_client(redis=None):
    return BinanceStreamClient(
        symbols=["BTCUSDT", "ETHUSDT"],
        timeframes=[],
        redis=redis,
        base_ws_url=BINANCE_FUTURES_WS_STREAM_URL,
        quote_ws_url=BINANCE_FUTURES_WS_QUOTE_STREAM_URL,
        instrument_suffix="-PERP",
    )


def test_quote_only_futures_client_opens_only_the_public_quote_connection():
    assert perp_quote_client().stream_urls == [
        f"{BINANCE_FUTURES_WS_QUOTE_STREAM_URL}?streams=btcusdt@bookTicker/ethusdt@bookTicker"
    ]


async def test_perp_book_ticker_is_keyed_as_perp_instrument():
    redis, written = recording_redis()
    await perp_quote_client(redis)._handle_book_ticker(BOOK_TICKER, arrival_ns=1_000)

    ui = json.loads(written["cryptonite:stream:ui_quote_tick"][0]["data"])
    health = json.loads(written["cryptonite:stream:ingress_health"][0]["data"])
    assert ui["instrument_id"] == health["instrument_id"] == "BTCUSDT-PERP.BINANCE"
    assert (ui["bid_price"], ui["ask_price"]) == ("50000.10", "50000.20")
    assert written["nautilus:stream:bus"][0]["topic"] == "data.quotes.BINANCE.BTCUSDT-PERP"


async def test_spot_book_ticker_keeps_the_spot_instrument():
    redis, written = recording_redis()
    spot = BinanceStreamClient(symbols=["BTCUSDT"], timeframes=["1m"], redis=redis)
    await spot._handle_book_ticker(BOOK_TICKER, arrival_ns=1_000)
    assert json.loads(written["cryptonite:stream:ui_quote_tick"][0]["data"])["instrument_id"] == "BTCUSDT.BINANCE"
