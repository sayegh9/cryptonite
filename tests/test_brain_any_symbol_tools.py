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
from cryptonite_scanner.regime import HIGH_VOL_WINDOW_BARS, MIN_BARS, REGIMES, classify_regime
from cryptonite_storage.queries import AnalyticsQueryEngine

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


def _lake_close_ms(row: list) -> int:
    """A bar's close as the lake stamps it: one millisecond after Binance's close_time_ms (the bar's last millisecond)."""
    return int(row[6]) + 1


def _frame(rows: list[list]) -> pl.DataFrame:
    """The bars the way the lake and the REST path hand them to classify_regime: close time in ns, prices as floats."""
    return pl.DataFrame({"ts_event_ns": [_lake_close_ms(r) * 10**6 for r in rows], "high": [float(r[2]) for r in rows],
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


# --- get_market_regime: the analytics lake for a fed symbol --------------------------------------------------------

def test_a_fed_symbol_with_lake_bars_is_read_from_the_lake_and_not_from_binance(monkeypatch):
    _fed(monkeypatch, "XRPUSDT")
    lake_frame = _frame(_klines(REGIME_BARS, HOUR_MS, forming=False))
    requested = []

    def fake_query(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
        requested.append((symbol, timeframe, start_ns, end_ns))
        return lake_frame

    monkeypatch.setattr(AnalyticsQueryEngine, "query_bars_df", fake_query)
    binance = _binance(monkeypatch, {})
    out = json.loads(tools.get_market_regime("XRPUSDT"))
    assert binance.calls == []
    assert out["source"] == "analytics lake" and out["timeframe"] == "1h"
    assert out["regime"] == classify_regime(lake_frame)
    (symbol, timeframe, start_ns, end_ns), = requested
    assert (symbol, timeframe) == ("XRPUSDT", "1h")
    assert end_ns - start_ns == REGIME_BARS * HOUR_MS * 10**6  # the learner's window, in bars of the timeframe


def test_a_fed_symbol_with_no_lake_bars_for_the_timeframe_falls_back_to_binance(monkeypatch):
    _fed(monkeypatch, "XRPUSDT")  # the lake directory is missing: no 15m bars
    rows = _klines(REGIME_BARS, 15 * 60_000)
    binance = _binance(monkeypatch, {SPOT_KLINES: _json(rows, headers=_http_date(rows[-1][0]))})
    out = json.loads(tools.get_market_regime("XRPUSDT", "15m"))
    assert out["source"] == "binance rest" and out["timeframe"] == "15m"
    assert out["regime"] == classify_regime(_frame(rows[:-1]))
    assert len(binance.calls) == 1


def test_an_unknown_feed_universe_is_not_treated_as_fed(monkeypatch):
    monkeypatch.setattr(tools, "_fed_symbols", lambda: None)

    def no_read(self, *args, **kwargs):
        raise _NoNetwork("lake")

    monkeypatch.setattr(AnalyticsQueryEngine, "query_bars_df", no_read)
    rows = _klines(REGIME_BARS, HOUR_MS)
    _binance(monkeypatch, {SPOT_KLINES: _json(rows, headers=_http_date(rows[-1][0]))})
    out = json.loads(tools.get_market_regime("XRPUSDT"))
    assert out["source"] == "binance rest"


# --- get_market_regime: Binance spot klines for any other symbol ---------------------------------------------------

def test_an_unfed_symbol_is_classified_from_spot_klines_exactly_as_the_learner_classifies_bars(monkeypatch):
    rows = _klines(REGIME_BARS, HOUR_MS)  # the last one is still forming on the server
    binance = _binance(monkeypatch, {SPOT_KLINES: _json(rows, headers=_http_date(rows[-1][0]))})
    out = json.loads(tools.get_market_regime("XRPUSDT"))
    expected = classify_regime(_frame(rows[:-1]))
    assert expected in REGIMES  # not vacuous: the regime is defined on these bars
    assert out == {"symbol": "XRPUSDT", "timeframe": "1h", "source": "binance rest", "regime": expected,
                   "bar_time": _iso(_lake_close_ms(rows[-2]))}
    assert [(url, params) for url, params, _ in binance.calls] == [
        (SPOT_KLINES, {"symbol": "XRPUSDT", "interval": "1h", "limit": REGIME_BARS + 2}),
    ]


@pytest.mark.parametrize(("timeframe", "interval_ms"), [("15m", 15 * 60_000), ("4h", 4 * HOUR_MS)])
def test_the_timeframe_sets_the_klines_interval_and_the_bar_size(monkeypatch, timeframe, interval_ms):
    rows = _klines(REGIME_BARS, interval_ms)
    binance = _binance(monkeypatch, {SPOT_KLINES: _json(rows, headers=_http_date(rows[-1][0]))})
    out = json.loads(tools.get_market_regime("XRPUSDT", timeframe))
    assert out["timeframe"] == timeframe and out["source"] == "binance rest"
    assert out["regime"] == classify_regime(_frame(rows[:-1]))
    assert out["bar_time"] == _iso(_lake_close_ms(rows[-2]))
    assert binance.calls[0][1]["interval"] == timeframe


def test_too_few_closed_candles_is_a_null_regime_with_a_reason(monkeypatch):
    rows = _klines(100, HOUR_MS)
    _binance(monkeypatch, {SPOT_KLINES: _json(rows, headers=_http_date(rows[-1][0]))})
    out = json.loads(tools.get_market_regime("XRPUSDT"))
    assert out == {
        "symbol": "XRPUSDT", "timeframe": "1h", "source": "binance rest", "regime": None,
        "reason": f"not enough 1h history on Binance spot for XRPUSDT to tag the regime (it needs {MIN_BARS} bars)",
    }


def test_a_failed_spot_request_is_a_null_regime_with_a_short_reason(monkeypatch):
    _binance(monkeypatch, {SPOT_KLINES: _json({"msg": "secret detail"}, status=500)})
    out = json.loads(tools.get_market_regime("XRPUSDT"))
    assert out == {"symbol": "XRPUSDT", "timeframe": "1h", "source": "binance rest", "regime": None,
                   "reason": "binance spot klines unavailable for XRPUSDT 1h"}
    assert "secret" not in json.dumps(out)


def test_the_regime_request_waits_behind_the_shared_spot_weight_gate(monkeypatch):
    monkeypatch.setattr(tools, "_LAST_USED_WEIGHT", 3050)
    monkeypatch.setattr(tools, "_LAST_WEIGHT_AT", time.time())
    binance = _binance(monkeypatch, {})
    out = json.loads(tools.get_market_regime("XRPUSDT"))
    assert binance.calls == []
    assert out["regime"] is None and out["source"] == "binance rest"


@pytest.mark.parametrize("timeframe", ["5m", "1d", "1H", "", "1h;"])
def test_a_timeframe_outside_15m_1h_4h_is_refused_before_any_read(monkeypatch, timeframe):
    binance = _binance(monkeypatch, {})

    def no_read(self, *args, **kwargs):
        raise _NoNetwork("lake")

    monkeypatch.setattr(AnalyticsQueryEngine, "query_bars_df", no_read)
    assert json.loads(tools.get_market_regime("BTCUSDT", timeframe)) == {"error": "invalid timeframe"}
    assert binance.calls == []


@pytest.mark.parametrize("symbol", HOSTILE_SYMBOLS)
def test_a_hostile_symbol_is_refused_before_any_http_or_lake_read(monkeypatch, symbol):
    binance = _binance(monkeypatch, {})

    def no_read(self, *args, **kwargs):
        raise _NoNetwork("lake")

    monkeypatch.setattr(AnalyticsQueryEngine, "query_bars_df", no_read)
    assert tools.get_market_regime(symbol) == '{"error": "invalid symbol"}'
    assert binance.calls == []


# --- Bounded caches, and the pause after a USD-M rate limit ---------------------------------------------------------

@pytest.mark.parametrize(("raw", "seconds"), [
    ("120", 120.0), ("0", 0.0), ("-5", 0.0), (None, 60.0), ("soon", 60.0), ("nan", 60.0), ("inf", 60.0),
])
def test_retry_after_is_read_as_seconds_and_a_missing_or_unreadable_one_gives_the_default_pause(raw, seconds):
    assert tools._retry_after_s(raw) == seconds


def test_retry_after_as_an_http_date_is_the_time_left_until_that_date():
    now = 1_700_000_000.0
    stamp = datetime.datetime.fromtimestamp(now + 90, tz=datetime.UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
    with patch("time.time", return_value=now):
        assert tools._retry_after_s(stamp) == pytest.approx(90.0)


@pytest.mark.parametrize("status", [429, 418])
def test_a_rate_limit_from_usdm_pauses_every_usdm_request_until_retry_after_has_passed(monkeypatch, status):
    binance = _binance(monkeypatch, {PREMIUM: _json({"msg": "slow down"}, status=status,
                                                    headers={"Retry-After": "120"})})
    clock = [1_700_000_000.0]
    with patch("time.time", side_effect=lambda: clock[0]):
        first = json.loads(tools.get_derivatives_context("XRPUSDT"))
        assert [url for url, _, _ in binance.calls] == [PREMIUM]  # the other two sections were not sent
        assert f"HTTP {status}; Retry-After 120s" in first["note"]
        assert first["mark"] is None and first["open_interest"] is None and first["funding"] is None
        clock[0] += 60
        second = json.loads(tools.get_derivatives_context("ETHUSDT"))  # another symbol, still paused
        assert len(binance.calls) == 1
        assert "pause" in second["note"] and "60s more" in second["note"]
        clock[0] += 61  # 121 s after the rate limit: the pause is over and the futures host is asked again
        json.loads(tools.get_derivatives_context("SOLUSDT"))
    assert len(binance.calls) == 2
    assert binance.calls[1][0] == PREMIUM


def test_the_usdm_cache_drops_expired_answers_on_every_write_and_stays_capped(monkeypatch):
    _binance(monkeypatch, _usdm_routes())
    clock = [1_700_000_000.0]
    with patch("time.time", side_effect=lambda: clock[0]):
        for i in range(100):
            tools.get_derivatives_context(f"AAA{i:03d}USDT")
        assert len(tools._FAPI_CACHE) == 100
        clock[0] += 31  # the first hundred are now older than 30 s
        for i in range(100, 200):
            tools.get_derivatives_context(f"AAA{i:03d}USDT")
        assert len(tools._FAPI_CACHE) == 100
        assert "AAA000USDT" not in tools._FAPI_CACHE and "AAA150USDT" in tools._FAPI_CACHE
        for i in range(200, 500):  # written at one instant: the cap, not the age, makes room
            tools.get_derivatives_context(f"AAA{i:03d}USDT")
    assert len(tools._FAPI_CACHE) == tools._FAPI_CACHE_MAX
    assert "AAA150USDT" not in tools._FAPI_CACHE and "AAA499USDT" in tools._FAPI_CACHE


def test_the_klines_cache_drops_expired_candles_on_every_write(monkeypatch):
    symbols = [f"K{i:03d}USDT" for i in range(200)]
    monkeypatch.setattr(tools, "_get_valid_symbols", lambda: set(symbols))
    rows = _klines(5, HOUR_MS)
    _binance(monkeypatch, {SPOT_KLINES: _json(rows, headers=_http_date(rows[-1][0]))})
    clock = [1_700_000_000.0]
    with patch("time.time", side_effect=lambda: clock[0]):
        for symbol in symbols[:100]:
            tools.get_klines(symbol, "1h", 2)
        assert len(tools._KLINES_CACHE) == 100
        clock[0] += 61  # the TTL is 60 s
        for symbol in symbols[100:]:
            tools.get_klines(symbol, "1h", 2)
    assert len(tools._KLINES_CACHE) == 100
    assert ("K000USDT", "1h", 2) not in tools._KLINES_CACHE
    assert ("K150USDT", "1h", 2) in tools._KLINES_CACHE


def test_the_regime_answer_is_cached_for_60_seconds_and_its_candles_are_not_kept(monkeypatch):
    rows = _klines(REGIME_BARS, HOUR_MS)
    binance = _binance(monkeypatch, {SPOT_KLINES: _json(rows, headers=_http_date(rows[-1][0]))})
    clock = [1_700_000_000.0]
    with patch("time.time", side_effect=lambda: clock[0]):
        first = json.loads(tools.get_market_regime("XRPUSDT"))
        assert len(tools._KLINES_CACHE) == 0  # the ~900 candles behind the answer are not kept
        clock[0] += 30
        assert json.loads(tools.get_market_regime("XRPUSDT")) == first
        assert len(binance.calls) == 1
        clock[0] += 31
        json.loads(tools.get_market_regime("XRPUSDT"))
    assert len(binance.calls) == 2


def test_the_regime_cache_stays_bounded_and_drops_expired_answers(monkeypatch):
    symbols = [f"R{i:03d}USDT" for i in range(600)]
    monkeypatch.setattr(tools, "_get_valid_symbols", lambda: set(symbols))
    rows = _klines(REGIME_BARS, HOUR_MS)
    _binance(monkeypatch, {SPOT_KLINES: _json(rows, headers=_http_date(rows[-1][0]))})
    clock = [1_700_000_000.0]
    with patch("time.time", side_effect=lambda: clock[0]):
        for symbol in symbols[:100]:
            tools.get_market_regime(symbol)
        assert len(tools._REGIME_CACHE) == 100
        clock[0] += 61
        for symbol in symbols[100:300]:
            tools.get_market_regime(symbol)
        assert len(tools._REGIME_CACHE) == 200  # the first hundred expired and were dropped by the writes
        assert ("R000USDT", "1h") not in tools._REGIME_CACHE
        for symbol in symbols[300:]:  # 300 writes at one instant: the cap makes room
            tools.get_market_regime(symbol)
    assert len(tools._REGIME_CACHE) == tools._REGIME_CACHE_MAX
    assert len(tools._KLINES_CACHE) == 0  # only answers were kept, no candles


def test_a_failed_spot_request_is_not_cached_so_the_next_call_asks_again(monkeypatch):
    binance = _binance(monkeypatch, {SPOT_KLINES: _json({"msg": "down"}, status=500)})
    json.loads(tools.get_market_regime("XRPUSDT"))
    json.loads(tools.get_market_regime("XRPUSDT"))
    assert len(binance.calls) == 2
    assert not tools._REGIME_CACHE
