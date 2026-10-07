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


async def test_perp_quotes_are_coalesced_per_symbol_with_contiguous_sequence():
    redis, written = recording_redis()
    client = BinanceStreamClient(
        symbols=["BTCUSDT"], timeframes=[], redis=redis, base_ws_url=BINANCE_FUTURES_WS_STREAM_URL,
        quote_ws_url=BINANCE_FUTURES_WS_QUOTE_STREAM_URL, instrument_suffix="-PERP", min_quote_interval_ms=20,
    )
    ms = 1_000_000
    for i in range(10):  # a 9 ms burst of changing books: only the first publishes
        await client._handle_book_ticker({**BOOK_TICKER, "b": f"49999.{i}0"}, arrival_ns=i * ms)
    await client._handle_book_ticker({**BOOK_TICKER, "b": "49999.00"}, arrival_ns=30 * ms)  # same as last published: dropped
    await client._handle_book_ticker({**BOOK_TICKER, "b": "49998.50"}, arrival_ns=31 * ms)  # changed + interval passed

    health = [json.loads(f["data"]) for f in written["cryptonite:stream:ingress_health"]]
    ui = [json.loads(f["data"]) for f in written["cryptonite:stream:ui_quote_tick"]]
    assert [q["bid_price"] for q in ui] == ["49999.00", "49998.50"]
    assert [h["publish_sequence"] for h in health] == [1, 2]  # no gaps for the freshness monitor


async def test_spot_quotes_are_not_throttled_by_default():
    redis, written = recording_redis()
    spot = BinanceStreamClient(symbols=["BTCUSDT"], timeframes=["1m"], redis=redis)
    for i in range(3):
        await spot._handle_book_ticker(BOOK_TICKER, arrival_ns=i)
    assert len(written["cryptonite:stream:ui_quote_tick"]) == 3


async def test_quote_snapshot_written_in_same_pipeline_as_stream_for_spot_and_perp():
    """B5: the same pipeline writes a per-instrument latest-quote snapshot (key, TTL, body) beside
    the stream entry, from the same tick — for both spot and perp instruments."""
    from contextlib import asynccontextmanager as _acm
    from unittest.mock import AsyncMock as _AsyncMock
    from unittest.mock import MagicMock as _MagicMock

    from cryptonite_schemas.marks import QUOTE_TICK_TTL_S, quote_tick_key

    assert QUOTE_TICK_TTL_S == 60

    for make, inst in ((lambda r: BinanceStreamClient(symbols=["BTCUSDT"], timeframes=["1m"], redis=r),
                        "BTCUSDT.BINANCE"),
                       (perp_quote_client, "BTCUSDT-PERP.BINANCE")):
        ops = []

        @_acm
        async def pipe_ctx(transaction=False):
            m = _MagicMock()
            m.xadd.side_effect = lambda s, f, **_: ops.append(("xadd", s, f))
            m.setex.side_effect = lambda k, t, v: ops.append(("setex", k, t, v))
            m.execute = _AsyncMock()
            yield m

        redis = _MagicMock()
        redis.pipeline = pipe_ctx
        await make(redis)._handle_book_ticker(BOOK_TICKER, arrival_ns=2_000)

        kinds = [o[0] for o in ops]
        assert kinds == ["xadd", "xadd", "xadd", "setex"]  # snapshot in the same pipeline
        _, key, ttl, body = ops[3]
        assert (key, ttl) == (quote_tick_key(inst), 60)
        stream_body = next(f for k, s, f in ops if k == "xadd" and s == "cryptonite:stream:ui_quote_tick")
        assert json.loads(body) == json.loads(stream_body["data"])  # same tick, same content
        assert json.loads(body)["instrument_id"] == inst
