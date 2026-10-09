"""Unit tests for BinanceRestClient rate-limiting, 429/418 retry handling, and X-MBX-USED-WEIGHT-1M tracking."""

import asyncio
from typing import Any

import httpx
import pytest
from cryptonite_ingest.client import BinanceRestClient


def sample_kline_row(open_time_ms: int = 1700000000000, close_time_ms: int = 1700000059999) -> list[Any]:
    return [
        open_time_ms,
        "50000.00",
        "50100.00",
        "49900.00",
        "50050.00",
        "100.5",
        close_time_ms,
        "5030000.00",
        500,
        "50.25",
        "2515000.00",
        "0",
    ]


@pytest.mark.asyncio
async def test_fetch_klines_happy_path(monkeypatch):
    """Happy path returns parsed klines and updates last_used_weight_1m."""
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    rows = [sample_kline_row()]

    def handle_req(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=rows,
            headers={"X-MBX-USED-WEIGHT-1M": "15"},
        )

    client = BinanceRestClient()
    client._client = httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(handle_req))

    klines = await client.fetch_klines("BTCUSDT", "1m")
    await client._client.aclose()

    assert len(klines) == 1
    assert klines[0].open == "50000.00"
    assert client.last_used_weight_1m == 15


@pytest.mark.asyncio
async def test_fetch_klines_handles_three_429s_without_crashing(monkeypatch):
    """Three consecutive 429s raise HTTPStatusError instead of UnboundLocalError."""
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handle_req(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"code": -1003, "msg": "Too many requests"},
            headers={"Retry-After": "2", "X-MBX-USED-WEIGHT-1M": "1200"},
        )

    client = BinanceRestClient()
    client._client = httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(handle_req))

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.fetch_klines("BTCUSDT", "1m")

    await client._client.aclose()

    assert exc_info.value.response.status_code == 429
    assert client.last_used_weight_1m == 1200
    # Verified sleep was called for 429 retries
    assert 2.0 in sleep_calls


@pytest.mark.asyncio
async def test_fetch_klines_handles_418_ip_ban(monkeypatch):
    """418 IP ban parses Retry-After, sleeps, and raises HTTPStatusError cleanly."""
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handle_req(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            418,
            json={"code": -1003, "msg": "IP Banned"},
            headers={"Retry-After": "120", "X-MBX-USED-WEIGHT-1M": "6000"},
        )

    client = BinanceRestClient()
    client._client = httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(handle_req))

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.fetch_klines("BTCUSDT", "1m")

    await client._client.aclose()

    assert exc_info.value.response.status_code == 418
    assert client.last_used_weight_1m == 6000
    assert 120.0 in sleep_calls


@pytest.mark.asyncio
async def test_fetch_klines_handles_malformed_headers(monkeypatch):
    """Malformed Retry-After and X-MBX-USED-WEIGHT-1M headers do not cause unhandled exceptions."""
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    attempts = 0

    def handle_req(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                json={"code": -1003},
                headers={"Retry-After": "invalid-int", "X-MBX-USED-WEIGHT-1M": "bad_weight"},
            )
        return httpx.Response(
            200,
            json=[sample_kline_row()],
            headers={"X-MBX-USED-WEIGHT-1M": "100"},
        )

    client = BinanceRestClient()
    client._client = httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(handle_req))

    klines = await client.fetch_klines("BTCUSDT", "1m")
    await client._client.aclose()

    assert len(klines) == 1
    assert client.last_used_weight_1m == 100
    # Default fallback Retry-After (5s) was used
    assert 5.0 in sleep_calls


@pytest.mark.asyncio
async def test_fetch_klines_high_weight_backoff(monkeypatch):
    """When last_used_weight_1m is high (>= 1000), pre-request backoff is triggered."""
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handle_req(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[sample_kline_row()], headers={"X-MBX-USED-WEIGHT-1M": "1050"})

    client = BinanceRestClient()
    client.last_used_weight_1m = 1050
    client._client = httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(handle_req))

    klines = await client.fetch_klines("BTCUSDT", "1m")
    await client._client.aclose()

    assert len(klines) == 1
    assert 1.0 in sleep_calls


@pytest.mark.asyncio
async def test_get_exchange_info_updates_weight():
    """get_exchange_info updates last_used_weight_1m from response headers."""
    def handle_req(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "filters": [],
                    }
                ]
            },
            headers={"X-MBX-USED-WEIGHT-1M": "20"},
        )

    client = BinanceRestClient()
    client._client = httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(handle_req))

    info = await client.get_exchange_info("BTCUSDT")
    await client._client.aclose()

    assert len(info) == 1
    assert info[0]["symbol"] == "BTCUSDT"
    assert client.last_used_weight_1m == 20
