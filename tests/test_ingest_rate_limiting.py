"""Unit tests for BinanceRestClient rate-limiting, 429/418 retry handling, and X-MBX-USED-WEIGHT-1M tracking."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
from cryptonite_ingest.client import (
    _CRITICAL_WEIGHT_PACE_S,
    _HIGH_WEIGHT_PACE_S,
    BinanceRestClient,
)


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


def make_client(handler) -> BinanceRestClient:
    client = BinanceRestClient()
    client._client = httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(handler))
    return client


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

    client = make_client(handle_req)

    klines = await client.fetch_klines("BTCUSDT", "1m")
    await client._client.aclose()

    assert len(klines) == 1
    assert klines[0].open == "50000.00"
    assert client.last_used_weight_1m == 15
    assert client._weight_at > 0


@pytest.mark.asyncio
async def test_fetch_klines_handles_three_429s_without_crashing(monkeypatch):
    """Three consecutive 429s (short Retry-After) sleep within budget then raise HTTPStatusError."""
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

    client = make_client(handle_req)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.fetch_klines("BTCUSDT", "1m")

    await client._client.aclose()

    assert exc_info.value.response.status_code == 429
    assert client.last_used_weight_1m == 1200
    # Each of the three 429s slept its Retry-After of 2s (6s total, within budget)
    assert sleep_calls.count(2.0) == 3


@pytest.mark.asyncio
async def test_fetch_klines_418_raises_immediately_single_request(monkeypatch):
    """418 IP ban fails fast: exactly one request, no retries, no sleep, clear error."""
    sleep_calls: list[float] = []
    request_count = 0

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handle_req(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            418,
            json={"code": -1003, "msg": "IP Banned"},
            headers={"Retry-After": "120", "X-MBX-USED-WEIGHT-1M": "6000"},
        )

    client = make_client(handle_req)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.fetch_klines("BTCUSDT", "1m")

    await client._client.aclose()

    assert exc_info.value.response.status_code == 418
    assert "IP banned (418)" in str(exc_info.value)
    assert client.last_used_weight_1m == 6000
    # Exactly one request hit the banned endpoint and nothing waited on the ban
    assert request_count == 1
    assert all(s < 0.1 for s in sleep_calls)


@pytest.mark.asyncio
async def test_fetch_klines_429_then_recovers(monkeypatch):
    """A 429 (short Retry-After) then a 200 recovers and returns the rows."""
    sleep_calls: list[float] = []
    attempt = 0

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handle_req(request: httpx.Request) -> httpx.Response:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            return httpx.Response(
                429,
                json={"code": -1003},
                headers={"Retry-After": "2", "X-MBX-USED-WEIGHT-1M": "1200"},
            )
        return httpx.Response(
            200,
            json=[sample_kline_row()],
            headers={"X-MBX-USED-WEIGHT-1M": "30"},
        )

    client = make_client(handle_req)

    klines = await client.fetch_klines("BTCUSDT", "1m")
    await client._client.aclose()

    assert attempt == 2
    assert len(klines) == 1
    assert 2.0 in sleep_calls
    assert client.last_used_weight_1m == 30


@pytest.mark.asyncio
async def test_fetch_klines_400_raises_immediately_single_request(monkeypatch):
    """Deterministic 4xx (400) is permanent: exactly one request, no retries."""
    sleep_calls: list[float] = []
    request_count = 0

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handle_req(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            400,
            json={"code": -1121, "msg": "Invalid symbol"},
            headers={"X-MBX-USED-WEIGHT-1M": "10"},
        )

    client = make_client(handle_req)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.fetch_klines("NOTREAL", "1m")

    await client._client.aclose()

    assert exc_info.value.response.status_code == 400
    assert request_count == 1
    # Only the base _rate_delay was slept, no 1s/2s retry backoffs
    assert not any(s in (1.0, 2.0) for s in sleep_calls)


@pytest.mark.asyncio
async def test_fetch_klines_huge_retry_after_fails_closed(monkeypatch):
    """A 429 asking to wait far beyond the safe cap fails closed: one request, no giant sleep."""
    sleep_calls: list[float] = []
    request_count = 0

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handle_req(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            429,
            json={"code": -1003},
            headers={"Retry-After": "86400", "X-MBX-USED-WEIGHT-1M": "1200"},
        )

    client = make_client(handle_req)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.fetch_klines("BTCUSDT", "1m")

    await client._client.aclose()

    assert exc_info.value.response.status_code == 429
    assert "failing closed" in str(exc_info.value)
    assert request_count == 1
    assert all(s < 0.1 for s in sleep_calls)


@pytest.mark.asyncio
async def test_fetch_klines_retry_after_future_date_form(monkeypatch):
    """RFC 7231 HTTP-date Retry-After is honored as its remaining seconds and the call recovers."""
    sleep_calls: list[float] = []
    attempt = 0
    future = datetime.now(UTC) + timedelta(seconds=10)
    date_header = format_datetime(future, usegmt=True)

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handle_req(request: httpx.Request) -> httpx.Response:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            return httpx.Response(
                429,
                json={"code": -1003},
                headers={"Retry-After": date_header, "X-MBX-USED-WEIGHT-1M": "1200"},
            )
        return httpx.Response(200, json=[sample_kline_row()], headers={"X-MBX-USED-WEIGHT-1M": "30"})

    client = make_client(handle_req)

    klines = await client.fetch_klines("BTCUSDT", "1m")
    await client._client.aclose()

    assert attempt == 2
    assert len(klines) == 1
    assert any(8.0 <= s <= 11.0 for s in sleep_calls)


@pytest.mark.asyncio
async def test_fetch_klines_retry_after_past_date_form_waits_1s(monkeypatch):
    """An HTTP-date Retry-After already in the past waits the minimum 1s and recovers."""
    sleep_calls: list[float] = []
    attempt = 0
    past = datetime.now(UTC) - timedelta(minutes=5)
    date_header = format_datetime(past, usegmt=True)

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handle_req(request: httpx.Request) -> httpx.Response:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            return httpx.Response(
                429,
                json={"code": -1003},
                headers={"Retry-After": date_header, "X-MBX-USED-WEIGHT-1M": "1200"},
            )
        return httpx.Response(200, json=[sample_kline_row()], headers={"X-MBX-USED-WEIGHT-1M": "30"})

    client = make_client(handle_req)

    klines = await client.fetch_klines("BTCUSDT", "1m")
    await client._client.aclose()

    assert len(klines) == 1
    assert 1.0 in sleep_calls


@pytest.mark.asyncio
async def test_fetch_klines_total_retry_budget_fails_closed(monkeypatch):
    """The total sleep budget caps one call: a 3rd 429 that would blow the budget raises instead."""
    sleep_calls: list[float] = []
    request_count = 0

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handle_req(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            429,
            json={"code": -1003},
            headers={"Retry-After": "25", "X-MBX-USED-WEIGHT-1M": "1200"},
        )

    client = make_client(handle_req)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.fetch_klines("BTCUSDT", "1m")

    await client._client.aclose()

    assert exc_info.value.response.status_code == 429
    # Two sleeps of 25s (total 50 <= 60 budget), the third would exceed it -> raise, no third sleep
    assert sleep_calls.count(25.0) == 2
    assert request_count == 3


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

    client = make_client(handle_req)

    klines = await client.fetch_klines("BTCUSDT", "1m")
    await client._client.aclose()

    assert len(klines) == 1
    assert client.last_used_weight_1m == 100
    # Default fallback Retry-After (5s) was used
    assert 5.0 in sleep_calls


@pytest.mark.asyncio
async def test_fetch_klines_high_weight_pacing_fresh_window(monkeypatch):
    """A fresh reading >= 1000 paces by _HIGH_WEIGHT_PACE_S before the request."""
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handle_req(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[sample_kline_row()], headers={"X-MBX-USED-WEIGHT-1M": "1050"})

    client = make_client(handle_req)
    client.last_used_weight_1m = 1050
    client._weight_at = time.time()  # fresh: still inside the 1-minute window

    klines = await client.fetch_klines("BTCUSDT", "1m")
    await client._client.aclose()

    assert len(klines) == 1
    assert _HIGH_WEIGHT_PACE_S in sleep_calls


@pytest.mark.asyncio
async def test_fetch_klines_critical_weight_paces_longer(monkeypatch):
    """A fresh reading >= 3000 paces by _CRITICAL_WEIGHT_PACE_S."""
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handle_req(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[sample_kline_row()], headers={"X-MBX-USED-WEIGHT-1M": "3500"})

    client = make_client(handle_req)
    client.last_used_weight_1m = 3500
    client._weight_at = time.time()

    klines = await client.fetch_klines("BTCUSDT", "1m")
    await client._client.aclose()

    assert len(klines) == 1
    assert _CRITICAL_WEIGHT_PACE_S in sleep_calls


@pytest.mark.asyncio
async def test_fetch_klines_stale_weight_skips_pacing(monkeypatch):
    """A reading older than the 1-minute window (stale) must not trigger the pacing gate."""
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handle_req(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[sample_kline_row()], headers={"X-MBX-USED-WEIGHT-1M": "15"})

    client = make_client(handle_req)
    client.last_used_weight_1m = 1050
    client._weight_at = 0.0  # stale: last reading far outside the window

    klines = await client.fetch_klines("BTCUSDT", "1m")
    await client._client.aclose()

    assert len(klines) == 1
    # No pacing/delay sleeps beyond the base _rate_delay
    assert not any(s >= _HIGH_WEIGHT_PACE_S for s in sleep_calls)
    assert client.last_used_weight_1m == 15
    assert client._weight_at > 0


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

    client = make_client(handle_req)

    info = await client.get_exchange_info("BTCUSDT")
    await client._client.aclose()

    assert len(info) == 1
    assert info[0]["symbol"] == "BTCUSDT"
    assert client.last_used_weight_1m == 20