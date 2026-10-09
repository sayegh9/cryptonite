"""Tests for the PR #46 review-proposal hardening (branch review/k1-get-klines-hardening).

Covers the failure paths the author's suite does not:
  - 429/418 is NOT masked by the weight gate (Retry-After must survive a >3000 weight header);
  - the shared budget pre-flights (a request that would land while weight is at/over the cap is
    skipped BEFORE any HTTP call);
  - the budget window expires and the next call is allowed again;
  - a failed/unavailable exchangeInfo lookup fails CLOSED (no unverified symbol lookups);
  - C1: the closed-candle boundary is derived from the SERVER's 'Date' header (clock-independent,
    conservative at second granularity) and fails CLOSED when server time is missing/unparseable;
  - T1: in_feed reflects the authoritative ingest universe, or 'unknown' — never a hardcoded guess;
  - C2: the klines cache is LRU-bounded and the budget gate + fetch serialize on one lock so a
    parallel burst cannot bypass the preflight gate.
"""

import json
import threading
from unittest.mock import MagicMock, patch

import cryptonite_agents.tools as tools


def _resp(status=200, headers=None, body=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = body if body is not None else []
    r.text = json.dumps(body) if body is not None else ""
    return r


def _kline(open_ms, close_ms):
    return [open_ms, "1.0", "2.0", "0.5", "1.5", "10.0", close_ms, "100.0", 10, "5.0", "50.0", "0"]


NOW = 1_700_000_000_000
# NOW == 2023-11-14 22:13:20 UTC -> Binance response 'Date' carries second granularity.
DATE_HDR = "Tue, 14 Nov 2023 22:13:20 GMT"


def test_429_with_over_budget_weight_still_reports_retry_after():
    """A 429 carrying weight>3000 must surface Retry-After, never be masked by the weight gate."""
    with patch.object(tools, "_get_valid_symbols", return_value={"BTCUSDT"}), patch(
        "httpx.get",
        return_value=_resp(429, {"Retry-After": "120", "X-MBX-USED-WEIGHT-1M": "3005"}),
    ):
        res = tools.get_klines("BTCUSDT", limit=5)
    assert "Rate limited by Binance API (HTTP 429). Retry-After: 120s" in res
    assert "Rate limit protection triggered" not in res


def test_200_over_budget_records_weight_and_next_call_preflights():
    """After a 200 that read weight>=cap, the NEXT call is skipped before any HTTP request."""
    closed = [_kline(NOW - 3600000, NOW - 1)]
    with patch.object(tools, "_get_valid_symbols", return_value={"BTCUSDT"}), patch("httpx.get") as m:
        m.return_value = _resp(200, {"X-MBX-USED-WEIGHT-1M": "3050"}, closed)
        with patch("time.time", return_value=NOW / 1000.0):
            first = tools.get_klines("BTCUSDT", "1h", 1)
            second = tools.get_klines("BTCUSDT", "1h", 1)  # 1s later, still in the window
    assert "Rate limit protection triggered (weight 3050 > 3000)" in first
    assert "Request skipped before sending" in second
    assert m.call_count == 1  # the skip never issued a request


def test_preflight_window_expires_and_next_call_is_allowed():
    closed = [_kline(NOW - 3600000, NOW - 1)]
    with patch.object(tools, "_get_valid_symbols", return_value={"BTCUSDT"}), patch("httpx.get") as m:
        m.return_value = _resp(200, {"X-MBX-USED-WEIGHT-1M": "3050"}, closed)
        with patch("time.time", return_value=NOW / 1000.0):
            first = tools.get_klines("BTCUSDT", "1h", 1)
        assert "Rate limit protection triggered (weight 3050 > 3000)" in first

        # 61s later the recorded weight is outside the budget window -> request allowed again.
        m.return_value = _resp(200, {"X-MBX-USED-WEIGHT-1M": "10", "Date": DATE_HDR}, closed)
        with patch("time.time", return_value=(NOW + 61_000) / 1000.0):
            second = tools.get_klines("BTCUSDT", "1h", 1)
    data = json.loads(second)
    assert data["candle_count"] == 1
    assert m.call_count == 2


def test_exchangeinfo_unavailable_fails_closed():
    """With no exchangeInfo cache and the lookup failing, refuse instead of skipping membership."""
    with patch.object(tools, "_get_valid_symbols", return_value=set()), patch("httpx.get") as m:
        res = tools.get_klines("ZZZZZZUSDT", limit=5)
    assert "Cannot verify symbol 'ZZZZZZUSDT' against Binance exchangeInfo" in res
    m.assert_not_called()


# ---------------------------------------------------------------------------
# C1: server-time closed-candle boundary
# ---------------------------------------------------------------------------


def test_server_date_boundary_is_clock_independent():
    """C1: the boundary comes from the response's 'Date' header, never the client clock.

    The client clock is mocked 1 day ahead and 1 day behind the server Date; both runs must select
    the exact same single closed candle (the forming candle is excluded in both).
    """
    closed = _kline(NOW - 3600000, NOW - 1)  # closed: close_time < server Date second
    forming = _kline(NOW, NOW + 3599999)  # still open on the server
    resp = _resp(200, {"X-MBX-USED-WEIGHT-1M": "10", "Date": DATE_HDR}, [forming, closed])
    with patch.object(tools, "_get_valid_symbols", return_value={"BTCUSDT"}), patch.object(
        tools, "_fed_symbols", return_value=frozenset({"BTCUSDT"})
    ), patch("httpx.get", return_value=resp):
        with patch("time.time", return_value=(NOW + 86_400_000) / 1000.0):  # client 1 day AHEAD
            ahead = json.loads(tools.get_klines("BTCUSDT", "1h", 2))
        with patch("time.time", return_value=(NOW - 86_400_000) / 1000.0):  # client 1 day BEHIND
            behind = json.loads(tools.get_klines("BTCUSDT", "1h", 2))
    assert ahead["candle_count"] == behind["candle_count"] == 1
    assert ahead["candles"][-1]["close_time_ms"] == NOW - 1
    assert behind["candles"][-1]["close_time_ms"] == NOW - 1


def test_candle_closed_at_server_date_second_is_included():
    """A candle whose close_time equals the server Date second is treated as closed (<=)."""
    closed = _kline(NOW - 3600000, NOW)  # close_time == server Date second
    resp = _resp(200, {"X-MBX-USED-WEIGHT-1M": "10", "Date": DATE_HDR}, [closed])
    with patch.object(tools, "_get_valid_symbols", return_value={"BTCUSDT"}), patch.object(
        tools, "_fed_symbols", return_value=frozenset({"BTCUSDT"})
    ), patch("httpx.get", return_value=resp), patch("time.time", return_value=NOW / 1000.0):
        data = json.loads(tools.get_klines("BTCUSDT", "1h", 1))
    assert data["candle_count"] == 1


def test_missing_date_header_fails_closed():
    """C1: no server 'Date' on a 200 response => fail closed, never fall back to the client clock."""
    with patch.object(tools, "_get_valid_symbols", return_value={"BTCUSDT"}), patch.object(
        tools, "_fed_symbols", return_value=frozenset({"BTCUSDT"})
    ), patch("httpx.get") as m:
        m.return_value = _resp(200, {"X-MBX-USED-WEIGHT-1M": "10"}, [_kline(NOW - 3600000, NOW - 1)])
        res = tools.get_klines("BTCUSDT", "1h", 1)
    assert "Error" in res
    assert "Binance server time unavailable" in res
    assert len(tools._KLINES_CACHE) == 0  # the failed response must not be cached


def test_unparseable_date_header_fails_closed():
    """C1: a 200 whose 'Date' cannot be parsed fails closed as well."""
    with patch.object(tools, "_get_valid_symbols", return_value={"BTCUSDT"}), patch.object(
        tools, "_fed_symbols", return_value=frozenset({"BTCUSDT"})
    ), patch("httpx.get") as m:
        m.return_value = _resp(
            200,
            {"X-MBX-USED-WEIGHT-1M": "10", "Date": "not-a-real-date"},
            [_kline(NOW - 3600000, NOW - 1)],
        )
        res = tools.get_klines("BTCUSDT", "1h", 1)
    assert "Binance server time unavailable" in res


# ---------------------------------------------------------------------------
# T1: in_feed truthfulness against the authoritative ingest universe
# ---------------------------------------------------------------------------


def test_fed_symbols_uses_authoritative_universe():
    """T1 unit: _fed_symbols parses the Redis `ingest:universe` payload ingest publishes."""
    fake_client = MagicMock()
    fake_client.get.return_value = json.dumps(
        {
            "v": 1,
            "venue": "binance",
            "symbols": ["BTCUSDT", "SOLUSDT"],
            "timeframes": ["1m", "1h"],
            "perp_symbols": [],
            "published_at": None,
        }
    )
    with patch("redis.Redis.from_url", return_value=fake_client):
        got = tools._fed_symbols()
    assert got == frozenset({"BTCUSDT", "SOLUSDT"})
    fake_client.get.assert_called_once_with("ingest:universe")
    fake_client.close.assert_called_once()


def test_fed_symbols_degrades_to_empty_on_failure():
    """T1 unit: any Redis/parse failure yields an empty set (callers report 'unknown')."""
    with patch("redis.Redis.from_url", side_effect=ConnectionError("redis down")):
        assert tools._fed_symbols() == frozenset()


def test_in_feed_unknown_when_universe_unavailable():
    """T1: an unavailable authoritative universe yields in_feed 'unknown', never a hardcoded 'fed'."""
    with patch.object(tools, "_get_valid_symbols", return_value={"BTCUSDT"}), patch.object(
        tools, "_fed_symbols", return_value=frozenset()
    ), patch(
        "httpx.get",
        return_value=_resp(200, {"X-MBX-USED-WEIGHT-1M": "10", "Date": DATE_HDR}, [_kline(NOW - 3600000, NOW - 1)]),
    ), patch("time.time", return_value=NOW / 1000.0):
        data = json.loads(tools.get_klines("BTCUSDT", "1h", 1))
    assert data["in_feed"] == "unknown"


def test_in_feed_fed_only_for_universe_members():
    """T1: a symbol in the authoritative universe is 'fed'; an out-of-universe symbol is 'unknown'."""
    with patch.object(tools, "_get_valid_symbols", return_value={"BTCUSDT", "SOLUSDT", "WLDUSDT"}), patch.object(
        tools, "_fed_symbols", return_value=frozenset({"BTCUSDT", "SOLUSDT"})
    ), patch(
        "httpx.get",
        return_value=_resp(200, {"X-MBX-USED-WEIGHT-1M": "10", "Date": DATE_HDR}, [_kline(NOW - 3600000, NOW - 1)]),
    ), patch("time.time", return_value=NOW / 1000.0):
        btc = json.loads(tools.get_klines("BTCUSDT", "1h", 1))
        sol = json.loads(tools.get_klines("SOLUSDT", "1h", 1))
        wld = json.loads(tools.get_klines("WLDUSDT", "1h", 1))
    assert btc["in_feed"] == "fed"
    assert sol["in_feed"] == "fed"
    assert wld["in_feed"] == "unknown"


# ---------------------------------------------------------------------------
# C2: bounded cache + serialized budget gate (parallel bursts cannot bypass it)
# ---------------------------------------------------------------------------


def test_klines_cache_is_bounded():
    """C2: the LRU cache never exceeds _KLINES_CACHE_MAX; the oldest entries are evicted first."""
    with patch.object(tools, "_KLINES_CACHE_MAX", 5), patch.object(
        tools, "_get_valid_symbols", return_value={"BTCUSDT"}
    ), patch.object(tools, "_fed_symbols", return_value=frozenset({"BTCUSDT"})), patch(
        "httpx.get",
        return_value=_resp(200, {"X-MBX-USED-WEIGHT-1M": "10", "Date": DATE_HDR}, [_kline(NOW - 3600000, NOW - 1)]),
    ), patch("time.time", return_value=NOW / 1000.0):
        for i in range(8):
            tools.get_klines("BTCUSDT", "1h", 1 + i)
    assert len(tools._KLINES_CACHE) == 5
    assert min(key[2] for key in tools._KLINES_CACHE) == 4  # limits 1..3 were evicted oldest-first


def test_parallel_calls_cannot_bypass_preflight_gate():
    """C2: a 4-thread burst serializes on the lock; only the first call issues HTTP, the rest are
    refused by the preflight gate. Parallelism cannot drive requests past the shared budget."""
    calls: list = []
    lock = threading.Lock()

    def fake_get(*args, **kwargs):
        with lock:
            calls.append(kwargs.get("params"))
        return _resp(
            200,
            {"X-MBX-USED-WEIGHT-1M": "3050", "Date": DATE_HDR},
            [_kline(NOW - 3600000, NOW - 1)],
        )

    results: list[str] = []
    barrier = threading.Barrier(5)  # 4 workers + main

    def gated_worker():
        barrier.wait()
        results.append(tools.get_klines("BTCUSDT", "1h", 1))

    with patch.object(tools, "_get_valid_symbols", return_value={"BTCUSDT"}), patch.object(
        tools, "_fed_symbols", return_value=frozenset({"BTCUSDT"})
    ), patch("httpx.get", side_effect=fake_get):
        threads = [threading.Thread(target=gated_worker) for _ in range(4)]
        for t in threads:
            t.start()
        barrier.wait()  # release the burst together
        for t in threads:
            t.join(timeout=20)
    assert not any(t.is_alive() for t in threads)
    assert len(calls) == 1  # 4 concurrent calls -> 1 HTTP request: the gate held under the lock
    assert len(results) == 4
    assert all("Rate limit protection triggered" in r for r in results)