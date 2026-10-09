"""Tests for the PR #46 review-proposal hardening (branch review/k1-get-klines-hardening).

Covers the failure paths the author's suite does not:
  - 429/418 is NOT masked by the weight gate (Retry-After must survive a >3000 weight header);
  - the shared budget pre-flights (a request that would land while weight is at/over the cap is
    skipped BEFORE any HTTP call);
  - the budget window expires and the next call is allowed again;
  - a failed/unavailable exchangeInfo lookup fails CLOSED (no unverified symbol lookups).
"""

import json
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
        m.return_value = _resp(200, {"X-MBX-USED-WEIGHT-1M": "10"}, closed)
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