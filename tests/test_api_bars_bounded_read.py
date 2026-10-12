"""The bars route reads the lake off the event loop; windowed reads return exactly the bars in the window.

Every served result must equal the full-history read: the same bars, in the same order. The lake is written
through the production path (Binance kline -> ``EnrichedBar`` -> ``AnalyticsLakeWriter``).
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl
import pytest
from cryptonite_api.routes.bars import load_resampled_bars
from cryptonite_api.routes.bars import router as bars_router
from cryptonite_scanner.resampler import ONE_MINUTE_NS, resample_1m_bars
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_schemas.identifiers import to_nautilus_bar_type, to_spot_instrument_id
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.queries import AnalyticsQueryEngine
from fastapi import FastAPI
from fastapi.testclient import TestClient

MIN = ONE_MINUTE_NS
DAY = 24 * 60 * MIN
DAY0 = 1_790_812_800 * 10**9  # 2026-10-01 00:00:00 UTC


def _write_batch(lake: Path, symbol: str, first_open_ns: int, n: int, batch_id: str, skip: frozenset[int] = frozenset()) -> None:
    inst = to_spot_instrument_id(symbol)
    bar_type = str(to_nautilus_bar_type(inst, "1m"))
    bars = []
    for i in range(n):
        if i in skip:
            continue
        open_ms = (first_open_ns + i * MIN) // 1_000_000
        m = open_ms // 60_000
        base = 1000.0 + (m % 997) * 0.5
        volume = float(m % 13 + 1)
        raw = RawBinanceKline(
            open_time_ms=open_ms, close_time_ms=open_ms + 59_999,
            open=str(base), high=str(base + 0.75), low=str(base - 0.25), close=str(base + 0.5),
            volume=str(volume), quote_volume=str(volume * (base + 0.5)), trades_count=m % 7 + 1,
            taker_buy_base_volume="0", taker_buy_quote_volume="0", is_closed=True,
        )
        bars.append(EnrichedBar.from_binance_raw(raw, inst, bar_type, 2, 5, received_at_ns=(open_ms + 59_675) * 1_000_000))
    AnalyticsLakeWriter(lake).write_batch(bars, symbol=symbol, timeframe="1m", batch_id=batch_id)


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    # BTCUSDT: a day-0 batch; a 1000-bar batch dated day 0 that runs past midnight; a day-1 batch with a 40-minute
    # gap; a day-2 batch that starts after a gap.
    _write_batch(tmp_path, "BTCUSDT", DAY0, 600, "btc-a")
    _write_batch(tmp_path, "BTCUSDT", DAY0 + 23 * 60 * MIN + 30 * MIN, 1000, "btc-b")
    _write_batch(tmp_path, "BTCUSDT", DAY0 + DAY + 17 * 60 * MIN + 10 * MIN, 900, "btc-c", skip=frozenset(range(300, 340)))
    _write_batch(tmp_path, "BTCUSDT", DAY0 + 2 * DAY + 9 * 60 * MIN, 600, "btc-d")
    return tmp_path


def _served(df: pl.DataFrame, limit: int) -> list[dict]:
    """What the route returns from a loaded frame: the newest ``limit`` bars."""
    return (df.tail(limit) if len(df) > limit else df).to_dicts()


def _assert_same_bars(got: list[dict], want: list[dict]) -> None:
    """Same bars in the same order. Float aggregates may differ in the last bits: the resampler's group-by sums are
    not ordered, so even two runs on identical input can differ by about 1e-16 relative."""
    assert [r.keys() for r in got] == [r.keys() for r in want]
    assert len(got) == len(want)
    for g, w in zip(got, want):
        for key in w:
            if isinstance(w[key], float):
                assert g[key] == pytest.approx(w[key], rel=1e-12), key
            else:
                assert g[key] == w[key], key


def _full_history(engine: AnalyticsQueryEngine, symbol: str, tf: str, limit: int) -> list[dict]:
    """The route's answer for a request with no window: read the whole lake, resample, keep the newest ``limit``."""
    df = engine.query_bars_df(symbol=symbol, timeframe="1m")
    res = resample_1m_bars(df, tf) if not df.is_empty() else df
    return _served(res, limit)


@pytest.mark.parametrize("tf", ["15m", "1h", "4h"])
@pytest.mark.parametrize("limit", [1, 7, 500])
def test_a_request_without_a_window_serves_what_the_full_history_serves(lake, tf, limit):
    engine = AnalyticsQueryEngine(lake)
    loaded = load_resampled_bars(engine, "BTCUSDT", tf, None, None)
    assert _served(loaded, limit) == _full_history(engine, "BTCUSDT", tf, limit)


@pytest.mark.parametrize("tf", ["15m", "1h"])
def test_a_window_that_starts_inside_a_batch_dated_the_day_before_matches_the_unpruned_read(lake, tf):
    engine = AnalyticsQueryEngine(lake)
    start, end = DAY0 + DAY, DAY0 + DAY + 3 * 60 * MIN  # day 1, 00:00 to 03:00: inside the 23:30 day-0 batch
    unpruned = engine.query_bars_df(symbol="BTCUSDT", timeframe="1m")
    expected_1m = unpruned.filter((pl.col("ts_event_ns") >= start) & (pl.col("ts_event_ns") <= end))
    expected = resample_1m_bars(expected_1m, tf).to_dicts()
    loaded = load_resampled_bars(engine, "BTCUSDT", tf, start, end)
    _assert_same_bars(loaded.to_dicts(), expected)
    assert len(expected) > 0


def test_concurrent_reads_each_get_the_same_bars(lake):
    engine = AnalyticsQueryEngine(lake)
    start = DAY0 + DAY
    end = start + 6 * 60 * MIN
    expected_full = engine.query_bars_df(symbol="BTCUSDT", timeframe="1m").to_dicts()
    expected_window = engine.query_bars_df(symbol="BTCUSDT", timeframe="1m", start_ns=start, end_ns=end).to_dicts()
    with ThreadPoolExecutor(max_workers=8) as pool:
        full = list(pool.map(lambda _: engine.query_bars_df(symbol="BTCUSDT", timeframe="1m").to_dicts(), range(24)))
        window = list(pool.map(lambda _: engine.query_bars_df(symbol="BTCUSDT", timeframe="1m", start_ns=start, end_ns=end).to_dicts(), range(24)))
    assert all(r == expected_full for r in full)
    assert all(r == expected_window for r in window)


def test_the_route_serves_the_newest_bars_and_rejects_an_unsupported_timeframe(lake):
    engine = AnalyticsQueryEngine(lake)
    app = FastAPI()
    app.include_router(bars_router)
    app.state.query_engine = engine
    client = TestClient(app)

    resp = client.get("/api/v1/bars", params={"symbol": "btcusdt", "timeframe": "1h", "limit": 5})
    assert resp.status_code == 200
    expected = _full_history(engine, "BTCUSDT", "1h", 5)
    assert [r["bar_open_ns"] for r in resp.json()] == [e["bar_open_ns"] for e in expected]
    assert len(resp.json()) == 5

    resp = client.get("/api/v1/bars", params={"symbol": "BTCUSDT", "timeframe": "2h"})
    assert resp.status_code == 400


def test_a_batch_that_spans_days_after_an_outage_is_served_in_full_by_a_window(tmp_path):
    # Recovery can merge a REST page with buffered live bars: a file dated day 0 can hold bars three days later.
    # A window over those later bars must still return them, so reads cannot prune by the file's date.
    _write_batch(tmp_path, "BTCUSDT", DAY0, 1000, "btc-long")
    _write_batch(tmp_path, "BTCUSDT", DAY0 + 3 * DAY, 120, "btc-late")
    engine = AnalyticsQueryEngine(tmp_path)
    start, end = DAY0 + 3 * DAY, DAY0 + 3 * DAY + 120 * MIN
    unpruned = engine.query_bars_df(symbol="BTCUSDT", timeframe="1m")
    expected = unpruned.filter((pl.col("ts_event_ns") >= start) & (pl.col("ts_event_ns") <= end))
    windowed = engine.query_bars_df(symbol="BTCUSDT", timeframe="1m", start_ns=start, end_ns=end)
    assert windowed.height == expected.height > 0
    _assert_same_bars(windowed.to_dicts(), expected.to_dicts())
