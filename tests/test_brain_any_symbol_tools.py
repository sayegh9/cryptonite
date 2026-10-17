"""WS-FIX P1 (Brain analysis for any Binance symbol): get_derivatives_context and get_market_regime for a symbol the
live feeds do not cover, read from Binance public REST.

Every HTTP call goes through a fake httpx.get that answers only the URLs a test sets up and records each call. Any other
URL raises a BaseException, which the tools' own error handling does not catch, so a test fails loudly instead of
reaching the network. Redis, the funding table and the analytics lake are patched per test. tests/conftest.py resets the
spot klines state; the USD-M state and cache are reset here.
"""

import datetime
import json
import time
from unittest.mock import patch

import httpx
import polars as pl
import pytest
from cryptonite_agents import tools
from cryptonite_scanner.regime import HIGH_VOL_WINDOW_BARS, MIN_BARS

FAPI = "https://fapi.binance.com"
SPOT_KLINES = "https://api.binance.com/api/v3/klines"
PREMIUM = f"{FAPI}/fapi/v1/premiumIndex"
OPEN_INTEREST = f"{FAPI}/fapi/v1/openInterest"
FUNDING_RATE = f"{FAPI}/fapi/v1/fundingRate"
NOT_PERPETUAL = "no USD-M perpetual is listed for {} on Binance; derivatives data does not apply"
REGIME_BARS = HIGH_VOL_WINDOW_BARS + MIN_BARS  # 902: the trailing window the learner reads, in bars of the timeframe
HOUR_MS = 3_600_000
T0_MS = 1_699_000_000_000  # a whole second, so the Date header of a fixture is exact
HOSTILE_SYMBOLS = [
    "BTC'USDT", "BTCUSDT;DROP TABLE x", "BTCUSDT/../../etc", "..", "BTC USDT", "B", "B" * 31, "", "BTC*",
    "BTCUSDT?x=1", "BTCUSDT&symbol=ETHUSDT",
]

PREMIUM_BODY = {"symbol": "XRPUSDT", "markPrice": "2.35012", "indexPrice": "2.34990", "lastFundingRate": "0.0001",
                "nextFundingTime": 1700035200000, "time": 1700000000000}
OI_BODY = {"symbol": "XRPUSDT", "openInterest": "12345.678", "time": 1700000000000}
FUNDING_BODY = [{"symbol": "XRPUSDT", "fundingTime": T0_MS + i * 8 * HOUR_MS, "fundingRate": f"0.000{i + 1}",
                 "markPrice": "2.3"} for i in range(6)]  # Binance answers oldest first


class _NoNetwork(BaseException):
    """A call the test did not set up. A BaseException, so no tool error handling turns it into a note."""


class Binance:
    """Stands in for httpx.get: answers by URL from ``routes`` (each a function of the params), records every call."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict, float | None]] = []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        if url not in self.routes:
            raise _NoNetwork(url)
        return self.routes[url](dict(params or {}))


def _json(body, status: int = 200, headers: dict | None = None):
    return lambda params: httpx.Response(status, json=body, headers=headers or {})


def _raises(error: Exception):
    def route(params):
        raise error

    return route


def _binance(monkeypatch, routes: dict) -> Binance:
    binance = Binance(routes)
    monkeypatch.setattr(httpx, "get", binance)
    return binance


def _iso(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.UTC).isoformat(timespec="seconds")


def _http_date(ms: int) -> dict:
    """The Binance 'Date' header of a response sent at ``ms`` (whole seconds)."""
    stamp = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
    return {"Date": stamp, "X-MBX-USED-WEIGHT-1M": "10"}


def _klines(count: int, interval_ms: int, *, forming: bool = True) -> list[list]:
    """``count`` closed klines of one interval in Binance's shape (plus one still forming): a steady uptrend whose
    range shrinks, so the regime is defined and not an artefact of flat prices."""
    rows, close = [], 1000.0
    for i in range(count + (1 if forming else 0)):
        open_ms = T0_MS + i * interval_ms
        following = close * 1.002
        rng = max(close, following) * 0.003 * (1 - i / (2 * (count + 1)))
        high, low = max(close, following) + rng, min(close, following) - rng
        rows.append([open_ms, f"{close:.4f}", f"{high:.4f}", f"{low:.4f}", f"{following:.4f}", "10.0",
                     open_ms + interval_ms - 1, "1.0", 1, "1.0", "1.0", "0"])
        close = following
    return rows


def _frame(rows: list[list]) -> pl.DataFrame:
    """The bars the way the lake and the REST path hand them to classify_regime: close time in ns, prices as floats."""
    return pl.DataFrame({"ts_event_ns": [int(r[6]) * 10**6 for r in rows], "high": [float(r[2]) for r in rows],
                         "low": [float(r[3]) for r in rows], "close": [float(r[4]) for r in rows]})


def _fed(monkeypatch, *symbols: str) -> None:
    monkeypatch.setattr(tools, "_fed_symbols", lambda: frozenset(symbols))


def _redis_keys(monkeypatch, keys: dict[str, str]) -> None:
    monkeypatch.setattr(tools, "_redis_get", lambda key: keys.get(key))


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """No feed keys, no funding rows, no fed symbols, an empty lake; the USD-M state and cache start empty."""
    monkeypatch.setattr(tools, "_redis_get", lambda key: None)
    monkeypatch.setattr(tools, "_readonly_rows", lambda statement, params=None: [])
    monkeypatch.setattr(tools, "_fed_symbols", lambda: frozenset())
    monkeypatch.setattr(tools, "_get_valid_symbols", lambda: {"ABCUSDT", "BTCUSDT", "WLDUSDT", "XRPUSDT"})
    monkeypatch.setattr(tools, "_FAPI_CACHE", {})
    monkeypatch.setattr(tools, "_FAPI_LAST_USED_WEIGHT", 0)
    monkeypatch.setattr(tools, "_FAPI_LAST_WEIGHT_AT", 0.0)
    monkeypatch.setenv("ANALYTICS_LAKE_PATH", str(tmp_path / "lake"))  # no lake directory: an empty lake


# --- get_derivatives_context: the live feed covers the symbol ------------------------------------------------------

def test_a_symbol_the_feed_covers_is_answered_from_the_feed_without_http(monkeypatch):
    mark = {"type": "perp.mark", "v": 1, "instrument_id": "BTCUSDT-PERP.BINANCE", "symbol": "BTCUSDT",
            "ts_event_ns": 1, "mark_price": "65000.5", "index_price": "65001.0", "funding_rate": "0.0001",
            "next_funding_time_ns": 2}
    _redis_keys(monkeypatch, {"perp:mark:BTCUSDT": json.dumps(mark)})
    binance = _binance(monkeypatch, {})
    out = json.loads(tools.get_derivatives_context("BTCUSDT"))
    assert out["source"] == "live feed"
    assert out["mark"] == mark and out["open_interest"] is None
    assert binance.calls == []


def test_funding_rows_alone_also_count_as_the_feed(monkeypatch):
    rows = [{"funding_time_ns": T0_MS * 10**6, "funding_rate": "0.0001", "mark_price": "65000",
             "funding_interval_hours": 8, "source": "BINANCE_REST"}]
    monkeypatch.setattr(tools, "_readonly_rows", lambda statement, params=None: rows)
    binance = _binance(monkeypatch, {})
    out = json.loads(tools.get_derivatives_context("BTCUSDT"))
    assert out["source"] == "live feed" and len(out["funding"]) == 1
    assert binance.calls == []


# --- get_derivatives_context: Binance USD-M REST for a symbol the feed does not cover -----------------------------

def _usdm_routes(**overrides) -> dict:
    routes = {PREMIUM: _json(PREMIUM_BODY), OPEN_INTEREST: _json(OI_BODY), FUNDING_RATE: _json(FUNDING_BODY)}
    for name, route in overrides.items():
        routes[{"premium": PREMIUM, "oi": OPEN_INTEREST, "funding": FUNDING_RATE}[name]] = route
    return routes


def test_an_uncovered_symbol_is_read_from_usdm_rest_with_its_source_and_fetched_at(monkeypatch):
    binance = _binance(monkeypatch, _usdm_routes())
    out = json.loads(tools.get_derivatives_context("XRPUSDT"))
    fetched = datetime.datetime.fromisoformat(out.pop("fetched_at"))
    assert abs(datetime.datetime.now(datetime.UTC) - fetched) < datetime.timedelta(minutes=1)
    assert out == {
        "symbol": "XRPUSDT", "source": "binance rest", "perpetual": True, "note": None,
        "mark": {"mark_price": 2.35012, "index_price": 2.3499, "funding_rate": 0.0001,
                 "next_funding_time": _iso(1700035200000)},
        "open_interest": {"open_interest": 12345.7, "time": _iso(1700000000000)},
        # newest first, as the live feed returns it
        "funding": [{"funding_time": _iso(T0_MS + i * 8 * HOUR_MS), "funding_rate": float(f"0.000{i + 1}"),
                     "mark_price": 2.3} for i in (5, 4, 3, 2, 1, 0)],
    }
    assert [(url, params) for url, params, _ in binance.calls] == [
        (PREMIUM, {"symbol": "XRPUSDT"}), (OPEN_INTEREST, {"symbol": "XRPUSDT"}),
        (FUNDING_RATE, {"symbol": "XRPUSDT", "limit": 6}),
    ]
    assert all(timeout == 5.0 for _, _, timeout in binance.calls)


@pytest.mark.parametrize(("name", "field", "label"), [
    ("oi", "open_interest", "open interest"),
    ("funding", "funding", "funding history"),
])
def test_one_failing_endpoint_leaves_only_its_field_null_with_a_note(monkeypatch, name, field, label):
    failing = _raises(httpx.ConnectError("refused")) if name == "oi" else _json({"msg": "down"}, status=500)
    _binance(monkeypatch, _usdm_routes(**{name: failing}))
    out = json.loads(tools.get_derivatives_context("XRPUSDT"))
    assert out["perpetual"] is True
    assert f"{label} is unknown" in out["note"]
    for key in ("mark", "open_interest", "funding"):
        assert (out[key] is None) == (key == field), key


def test_a_symbol_with_no_usdm_perpetual_is_a_neutral_answer_and_stops_there(monkeypatch):
    binance = _binance(monkeypatch, {PREMIUM: _json({"code": -1121, "msg": "Invalid symbol."}, status=400)})
    out = json.loads(tools.get_derivatives_context("ABCUSDT"))
    assert out == {"symbol": "ABCUSDT", "perpetual": False, "note": NOT_PERPETUAL.format("ABCUSDT")}
    assert len(binance.calls) == 1


def test_the_derivatives_docstring_says_missing_data_is_not_a_reason_against_a_trade():
    assert "not a reason against a trade" in tools.get_derivatives_context.__doc__


def test_the_usdm_answer_is_cached_for_30_seconds(monkeypatch):
    binance = _binance(monkeypatch, _usdm_routes())
    clock = [1_700_000_000.0]
    with patch("time.time", side_effect=lambda: clock[0]):
        first = json.loads(tools.get_derivatives_context("XRPUSDT"))
        clock[0] += 29
        second = json.loads(tools.get_derivatives_context("XRPUSDT"))
        assert len(binance.calls) == 3  # the repeat within 30 s made no request
        clock[0] += 2  # 31 s after the first answer
        json.loads(tools.get_derivatives_context("XRPUSDT"))
    assert second == first
    assert len(binance.calls) == 6


def test_a_near_limit_usdm_weight_refuses_before_any_request(monkeypatch):
    binance = _binance(monkeypatch, {})
    monkeypatch.setattr(tools, "_FAPI_LAST_USED_WEIGHT", 2200)
    monkeypatch.setattr(tools, "_FAPI_LAST_WEIGHT_AT", time.time())
    out = json.loads(tools.get_derivatives_context("XRPUSDT"))
    assert binance.calls == []
    assert out["source"] == "binance rest" and out["perpetual"] is None
    assert out["mark"] is None and out["open_interest"] is None and out["funding"] is None
    assert "near its limit" in out["note"]


def test_a_weight_header_near_the_cap_stops_the_rest_of_that_symbols_requests(monkeypatch):
    binance = _binance(monkeypatch, {PREMIUM: _json(PREMIUM_BODY, headers={"X-MBX-USED-WEIGHT-1M": "2200"})})
    out = json.loads(tools.get_derivatives_context("XRPUSDT"))
    assert [url for url, _, _ in binance.calls] == [PREMIUM]  # the next two were refused before being sent
    assert out["mark"] is not None and out["open_interest"] is None and out["funding"] is None
    assert "near its limit" in out["note"]
    assert tools._FAPI_LAST_USED_WEIGHT == 2200


@pytest.mark.parametrize("symbol", HOSTILE_SYMBOLS)
def test_a_hostile_symbol_is_refused_before_any_http_redis_or_sql(monkeypatch, symbol):
    binance = _binance(monkeypatch, {})

    def no_redis(key):
        raise _NoNetwork(key)

    def no_sql(statement, params=None):
        raise _NoNetwork(statement)

    monkeypatch.setattr(tools, "_redis_get", no_redis)
    monkeypatch.setattr(tools, "_readonly_rows", no_sql)
    assert tools.get_derivatives_context(symbol) == '{"error": "invalid symbol"}'
    assert binance.calls == []
