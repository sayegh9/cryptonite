"""Tests for the perpetual mark price / funding / open interest feed.

WebSocket and REST are mocked; Redis is the real test instance (test-only symbols);
funding dedup runs against a throwaway database migrated to head, so the dev DB is untouched.
"""

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
import redis.asyncio as aioredis
from connections import DB_URL, REDIS_URL
from cryptonite_scanner_service.derivatives_feed import (
    MARK_CHANNEL,
    MARK_KEY,
    OI_KEY,
    DerivativesFeed,
    FundingSettlement,
    FundingSettlementTracker,
    mark_stream_url,
    parse_mark_frame,
    store_funding_settlements,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SYM_A = "ZZFEEDAUSDT"  # test-only symbols so real perp:mark:* keys are never touched
SYM_B = "ZZFEEDBUSDT"
T0_MS = 1_758_700_800_000  # a funding timestamp (ms)
HOUR_MS = 3_600_000


def mark_evt(symbol, e_ms, mark, rate, t_ms, index="100.0"):
    return {"e": "markPriceUpdate", "E": e_ms, "s": symbol, "p": mark, "i": index, "P": mark, "r": rate, "T": t_ms}


def combined(evt):
    return json.dumps({"stream": f"{evt['s'].lower()}@markPrice@1s", "data": evt})


@pytest.fixture
async def redis_client():
    client = aioredis.from_url(REDIS_URL)
    yield client
    await client.delete(*(k.format(symbol=s) for k in (MARK_KEY, OI_KEY) for s in (SYM_A, SYM_B)))
    await client.aclose()


@pytest.fixture(scope="module")
def funding_db_url():
    name = f"cryptonite_feedtest_{uuid.uuid4().hex[:8]}"

    async def admin(sql):
        conn = await asyncpg.connect(DB_URL)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(admin(f'CREATE DATABASE "{name}"'))
    url = urlunsplit(urlsplit(DB_URL)._replace(path=f"/{name}"))
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", "upgrade", "head"],
            cwd=REPO_ROOT, env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True,
        )
        yield url
    finally:
        asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


# --- Parsing -----------------------------------------------------------------

def test_parse_array_frame_skips_non_mark_events_and_uses_event_time():
    frame = json.dumps([
        mark_evt("BTCUSDT", 1_758_700_000_123, "60000.10", "0.00010000", T0_MS),
        {"e": "kline", "E": 1, "s": "BTCUSDT"},
        mark_evt("ethusdt", 1_758_700_000_456, "3000.5", "-0.00002500", T0_MS),
    ])
    btc, eth = parse_mark_frame(frame)
    assert (btc.symbol, btc.instrument_id) == ("BTCUSDT", "BTCUSDT-PERP.BINANCE")
    assert btc.ts_event_ns == 1_758_700_000_123_000_000
    assert btc.mark_price == Decimal("60000.10") and btc.funding_rate == Decimal("0.00010000")
    assert btc.next_funding_time_ns == T0_MS * 1_000_000
    assert (eth.symbol, eth.funding_rate) == ("ETHUSDT", Decimal("-0.00002500"))


def test_parse_combined_stream_frame():
    (tick,) = parse_mark_frame(combined(mark_evt("BTCUSDT", 1, "1", "0", T0_MS, index="0.99")))
    assert tick.index_price == Decimal("0.99")


def test_mark_stream_url():
    assert mark_stream_url(["BTCUSDT", "ETHUSDT"]) == (
        "wss://fstream.binance.com/market/stream?streams=btcusdt@markPrice@1s/ethusdt@markPrice@1s"
    )


# --- Redis hot path ----------------------------------------------------------

async def test_handle_frame_caches_with_ttl_and_publishes(redis_client):
    feed = DerivativesFeed(redis_client, None, [SYM_A])
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(MARK_CHANNEL)
    await pubsub.get_message(timeout=1)  # subscribe confirmation

    await feed.handle_frame(combined(mark_evt(SYM_A, 1_000, "123.45", "0.0001", T0_MS)))

    key = MARK_KEY.format(symbol=SYM_A)
    cached = json.loads(await redis_client.get(key))
    assert cached["mark_price"] == "123.45" and cached["funding_rate"] == "0.0001"
    assert cached["next_funding_time_ns"] == T0_MS * 1_000_000 and cached["ts_event_ns"] == 1_000_000_000
    assert 0 < await redis_client.ttl(key) <= 15

    published = None
    for _ in range(20):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if msg and json.loads(msg["data"])["symbol"] == SYM_A:
            published = json.loads(msg["data"])
            break
    await pubsub.aclose()
    assert published == cached


async def test_out_of_order_tick_does_not_overwrite_newer_cache(redis_client):
    feed = DerivativesFeed(redis_client, None, [SYM_A])
    await feed.handle_frame(combined(mark_evt(SYM_A, 2_000, "200", "0.0001", T0_MS)))
    await feed.handle_frame(combined(mark_evt(SYM_A, 1_000, "100", "0.0001", T0_MS)))
    assert json.loads(await redis_client.get(MARK_KEY.format(symbol=SYM_A)))["mark_price"] == "200"


# --- Funding settlement detection ---------------------------------------------

def test_tracker_detects_rollover_and_infers_interval():
    tracker = FundingSettlementTracker()

    def observe(e_ms, mark, rate, t_ms):
        (tick,) = parse_mark_frame(json.dumps(mark_evt(SYM_A, e_ms, mark, rate, t_ms)))
        return tracker.observe(tick)

    assert observe(T0_MS - 2_000, "100", "0.0003", T0_MS) is None  # first sight
    assert observe(T0_MS - 1_000, "101", "0.0004", T0_MS) is None  # same T
    first = observe(T0_MS + 1_000, "102", "0.0001", T0_MS + 8 * HOUR_MS)  # T rolled
    assert first.funding_time_ns == T0_MS * 1_000_000
    assert (first.funding_rate, first.mark_price) == (Decimal("0.0004"), Decimal("101"))  # last pre-settlement tick
    assert first.funding_interval_hours is None  # no earlier settlement observed
    second = observe(T0_MS + 8 * HOUR_MS + 1_000, "103", "0.0002", T0_MS + 12 * HOUR_MS)
    assert second.funding_interval_hours == 8


async def test_funding_settlements_are_deduplicated(funding_db_url):
    pool = await asyncpg.create_pool(funding_db_url, min_size=1, max_size=2)
    redis_client = aioredis.from_url(REDIS_URL)
    frames = [
        combined(mark_evt(SYM_B, T0_MS - 1_000, "3000.25", "0.00012", T0_MS)),
        combined(mark_evt(SYM_B, T0_MS + 1_000, "3001.00", "0.00005", T0_MS + 4 * HOUR_MS)),
    ]
    try:
        for _ in range(2):  # second pass simulates a restarted feed replaying the same frames
            feed = DerivativesFeed(redis_client, pool, [SYM_B])
            for frame in frames:
                await feed.handle_frame(frame)

        rows = await pool.fetch("SELECT * FROM funding_rates WHERE instrument_id = $1", f"{SYM_B}-PERP.BINANCE")
        assert len(rows) == 1
        row = rows[0]
        assert row["funding_time_ns"] == T0_MS * 1_000_000
        assert row["funding_rate"] == Decimal("0.00012") and row["mark_price"] == Decimal("3000.25")
        assert row["source"] == "STREAM_SETTLEMENT"

        duplicate = FundingSettlement(
            f"{SYM_B}-PERP.BINANCE", T0_MS * 1_000_000, Decimal("0.00012"), Decimal("3000.25"), None
        )
        assert await store_funding_settlements(pool, [duplicate]) == 0
    finally:
        await redis_client.delete(MARK_KEY.format(symbol=SYM_B))
        await redis_client.aclose()
        await pool.close()


# --- Resilience -----------------------------------------------------------------

class FakeWebSocket:
    def __init__(self, frames):
        self.frames = list(frames)

    async def recv(self):
        if not self.frames:
            raise ConnectionResetError("stream dropped")
        return self.frames.pop(0)


async def test_mark_stream_reconnects_with_backoff_after_disconnect(redis_client):
    stop = asyncio.Event()
    calls = []

    @contextlib.asynccontextmanager
    async def fake_connect(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            yield FakeWebSocket([combined(mark_evt(SYM_A, 5_000, "77.7", "0.0001", T0_MS))])
        else:
            stop.set()
            raise OSError("connect refused")
            yield  # pragma: no cover

    feed = DerivativesFeed(redis_client, None, [SYM_A], connect=fake_connect, backoff_base_s=0.001)
    await asyncio.wait_for(feed.run_mark_stream(stop), timeout=5)

    assert len(calls) == 2  # dropped once, reconnected once
    assert calls[0][0] == mark_stream_url([SYM_A])
    assert json.loads(await redis_client.get(MARK_KEY.format(symbol=SYM_A)))["mark_price"] == "77.7"


def test_backoff_is_exponential_capped_and_jittered():
    feed = DerivativesFeed(None, None, [SYM_A], backoff_base_s=1.0, backoff_cap_s=60.0)
    for attempt, ceiling in [(0, 1), (3, 8), (10, 60)]:
        delay = feed._backoff_delay(attempt)
        assert ceiling * 0.5 <= delay <= ceiling


# --- Open interest poller ---------------------------------------------------------

async def test_open_interest_poll_caches_each_symbol_and_isolates_failures(redis_client):
    def handler(request):
        symbol = request.url.params["symbol"]
        if symbol == SYM_B:
            return httpx.Response(500, json={"code": -1000})
        return httpx.Response(200, json={"openInterest": "10659.509", "symbol": symbol, "time": 1_589_437_530_011})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        feed = DerivativesFeed(redis_client, None, [SYM_A, SYM_B], http_client=http)
        await feed.poll_open_interest_once()

    key = OI_KEY.format(symbol=SYM_A)
    cached = json.loads(await redis_client.get(key))
    assert cached["open_interest"] == "10659.509"
    assert cached["ts_event_ns"] == 1_589_437_530_011_000_000
    assert cached["instrument_id"] == f"{SYM_A}-PERP.BINANCE"
    assert 0 < await redis_client.ttl(key) <= 90
    assert await redis_client.get(OI_KEY.format(symbol=SYM_B)) is None
