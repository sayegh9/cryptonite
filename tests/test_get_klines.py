"""Tests for K1 Phase 1 get_klines tool (validation, caching, closed candle filtering, rate-limit headers)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from cryptonite_agents.tools import _KLINES_CACHE, get_klines


@pytest.fixture(autouse=True)
def clear_caches():
    _KLINES_CACHE.clear()


def test_get_klines_invalid_symbol_format():
    res = get_klines("BTC-USDT!!!")
    assert "Error: Invalid symbol format" in res


def test_get_klines_invalid_interval():
    res = get_klines("BTCUSDT", interval="2h")
    assert "Error: Invalid interval '2h'" in res


def test_get_klines_invalid_limit():
    assert "Error: Limit must be" in get_klines("BTCUSDT", limit=0)
    assert "Error: Limit must be" in get_klines("BTCUSDT", limit=501)


@patch("cryptonite_agents.tools._get_valid_symbols")
def test_get_klines_invalid_active_symbol(mock_valid):
    mock_valid.return_value = {"BTCUSDT", "ETHUSDT"}
    res = get_klines("FAKESYMBOLUSDT")
    assert "Error: Symbol 'FAKESYMBOLUSDT' is not a valid active trading pair" in res


@patch("cryptonite_agents.tools._get_valid_symbols")
@patch("httpx.get")
def test_get_klines_successful_fetch_and_closed_filter(mock_http, mock_valid):
    mock_valid.return_value = {"SOLUSDT"}

    # Mock raw klines from Binance: 2 closed, 1 open candle
    now_ms = 1_700_000_000_000
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"X-MBX-USED-WEIGHT-1M": "100", "Date": "Tue, 14 Nov 2023 22:13:20 GMT"}
    mock_response.json.return_value = [
        [now_ms - 7200000, "150.0", "155.0", "149.0", "154.0", "1000.0", now_ms - 3600001, "150000.0", 50, "500.0", "75000.0", "0"],
        [now_ms - 3600000, "154.0", "160.0", "153.0", "158.0", "1200.0", now_ms - 1, "190000.0", 60, "600.0", "95000.0", "0"],
        [now_ms, "158.0", "162.0", "157.0", "161.0", "800.0", now_ms + 3599999, "128000.0", 40, "400.0", "64000.0", "0"],
    ]
    mock_http.return_value = mock_response

    with patch("time.time", return_value=now_ms / 1000.0):
        res_json = get_klines("SOLUSDT", interval="1h", limit=2)

    data = json.loads(res_json)
    assert data["source"] == "binance_spot_klines"
    assert data["symbol"] == "SOLUSDT"
    assert data["interval"] == "1h"
    assert data["in_feed"] == "unknown"
    assert data["candle_count"] == 2
    assert len(data["candles"]) == 2
    assert data["candles"][-1]["close"] == 158.0


@patch("cryptonite_agents.tools._get_valid_symbols")
@patch("httpx.get")
def test_get_klines_caching_behavior(mock_http, mock_valid):
    mock_valid.return_value = {"BTCUSDT"}

    now_ms = 1_700_000_000_000
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"X-MBX-USED-WEIGHT-1M": "50", "Date": "Tue, 14 Nov 2023 22:13:20 GMT"}
    mock_response.json.return_value = [
        [now_ms - 3600000, "50000.0", "51000.0", "49500.0", "50800.0", "500.0", now_ms - 1, "25000000.0", 1000, "250.0", "12500000.0", "0"],
    ]
    mock_http.return_value = mock_response

    with patch("time.time", return_value=now_ms / 1000.0):
        res1 = get_klines("BTCUSDT", interval="1h", limit=1)
        res2 = get_klines("BTCUSDT", interval="1h", limit=1)

    assert res1 == res2
    assert mock_http.call_count == 1  # 2nd call was served from 60s cache


@patch("cryptonite_agents.tools._get_valid_symbols")
@patch("httpx.get")
def test_get_klines_used_weight_threshold_block(mock_http, mock_valid):
    mock_valid.return_value = {"BTCUSDT"}

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"X-MBX-USED-WEIGHT-1M": "3005"}
    mock_response.json.return_value = []
    mock_http.return_value = mock_response

    res = get_klines("BTCUSDT", interval="1h", limit=10)
    assert "Error: Rate limit protection triggered (weight 3005 > 3000)" in res


@patch("cryptonite_agents.tools._get_valid_symbols")
@patch("httpx.get")
def test_get_klines_rate_limit_429_retry_after(mock_http, mock_valid):
    mock_valid.return_value = {"BTCUSDT"}

    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.headers = {"Retry-After": "120"}
    mock_http.return_value = mock_response

    res = get_klines("BTCUSDT", interval="1h", limit=10)
    assert "Error: Rate limited by Binance API (HTTP 429). Retry-After: 120s." in res
