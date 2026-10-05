"""B1 / F-C1: the API's onboarding and backfill routes write lake rows stamped exactly like ingest
(``ts_event_ns = ts_init_ns = kline CLOSE``, ``EnrichedBar.from_binance_raw``) and never a forming kline;
``/api/v1/bars?timeframe=1m`` then reports each minute at its OPEN. No network, no DB: httpx, the pool and
the two-store coordinator are fakes."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from cryptonite_api.routes import instruments
from cryptonite_api.routes.bars import get_bars
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.queries import AnalyticsQueryEngine

MIN_MS = 60_000
T0_MS = 1_791_104_400_000  # 2026-10-04 09:00:00 UTC


def rest_kline(open_ms: int, price: float) -> list:
    """A Binance /api/v3/klines row."""
    return [open_ms, f"{price:.2f}", f"{price + 1:.2f}", f"{price - 1:.2f}", f"{price + 0.5:.2f}", "3.5",
            open_ms + MIN_MS - 1, "350.0", 42, "1.5", "150.0", "0"]


def test_klines_to_bars_stamps_the_close_like_ingest():
    rows = [rest_kline(T0_MS + i * MIN_MS, 100 + i) for i in range(3)]
    bars = instruments.klines_to_bars(rows, "BTCUSDT.BINANCE", "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL", 2, 5,
                                      now_ms=T0_MS + 10 * MIN_MS)
    assert [b.ts_event_ns for b in bars] == [(T0_MS + (i + 1) * MIN_MS) * 10**6 for i in range(3)]
    assert all(b.ts_init_ns == b.ts_event_ns for b in bars)
    for b, r in zip(bars, rows, strict=True):
        raw = RawBinanceKline(open_time_ms=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5],
                              close_time_ms=r[6], quote_volume=r[7], trades_count=r[8],
                              taker_buy_base_volume=r[9], taker_buy_quote_volume=r[10])
        want = EnrichedBar.from_binance_raw(raw, b.instrument_id, b.bar_type, 2, 5, received_at_ns=b.received_at_ns)
        assert b == want
        # not the pre-B1 open-stamp fingerprint (received_at = Binance close_time = ts + 1m - 1 ms)
        assert b.received_at_ns - b.ts_event_ns != MIN_MS * 10**6 - 10**6


def test_klines_to_bars_skips_the_forming_kline():
    rows = [rest_kline(T0_MS + i * MIN_MS, 100 + i) for i in range(3)]
    # now = 09:02:30: the minute opening 09:02 is still forming
    bars = instruments.klines_to_bars(rows, "BTCUSDT.BINANCE", "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL", 2, 5,
                                      now_ms=T0_MS + 2 * MIN_MS + 30_000)
    assert [b.ts_event_ns // 10**6 for b in bars] == [T0_MS + MIN_MS, T0_MS + 2 * MIN_MS]


# --- both routes, end to end through fakes ------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._payload, self.status_code, self.text = payload, 200, ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeHttp:
    def __init__(self, klines):
        self.klines = klines

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        if "exchangeInfo" in url:
            return FakeResponse({"symbols": [{"baseAsset": "BTC", "quoteAsset": "USDT", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"}, {"filterType": "LOT_SIZE", "stepSize": "0.00001"}]}]})
        return FakeResponse(self.klines)


class FakePool:
    async def fetchrow(self, *a):
        return {"price_precision": 2, "size_precision": 5}

    async def execute(self, *a):
        return "OK"


@pytest.fixture
def committed(monkeypatch):
    now_ms = int(time.time() * 1000)
    m0 = now_ms // MIN_MS * MIN_MS
    klines = [rest_kline(m0 + i * MIN_MS, 100 + i) for i in (-3, -2, 0)]  # the last one is forming
    batches: list[list[EnrichedBar]] = []

    class FakeCoordinator:
        def __init__(self, *a, **k):
            pass

        async def commit_batch(self, bars, **kw):
            batches.append(bars)
            return "batch"

    monkeypatch.setattr(instruments.httpx, "AsyncClient", FakeHttp(klines))
    monkeypatch.setattr(instruments, "TwoStoreCoordinator", FakeCoordinator)
    monkeypatch.setattr(instruments, "AnalyticsLakeWriter", lambda *a, **k: None)
    monkeypatch.setattr(instruments, "NautilusCatalogManager", lambda *a, **k: None)
    return SimpleNamespace(batches=batches, opens_ms=[m0 - 3 * MIN_MS, m0 - 2 * MIN_MS])


@pytest.mark.parametrize("route", ["onboarding", "backfill"])
def test_api_routes_write_close_stamped_closed_bars(committed, route):
    if route == "onboarding":
        asyncio.run(instruments.run_onboarding("BTCUSDT", FakePool(), None))
    else:
        asyncio.run(instruments.run_backfill("BTCUSDT.BINANCE", "1m", 1, FakePool()))
    bars = [b for batch in committed.batches for b in batch]
    assert [b.ts_event_ns for b in bars] == [(o + MIN_MS) * 10**6 for o in committed.opens_ms]
    assert all(b.ts_init_ns == b.ts_event_ns for b in bars)


# --- /api/v1/bars?timeframe=1m reports the open --------------------------------------------------------

def test_bars_route_1m_reports_each_minute_at_its_open(tmp_path):
    rows = [rest_kline(T0_MS + i * MIN_MS, 100 + i) for i in range(5)]
    bars = instruments.klines_to_bars(rows, "BTCUSDT.BINANCE", "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL", 2, 5,
                                      now_ms=T0_MS + 10 * MIN_MS)
    AnalyticsLakeWriter(tmp_path).write_batch(bars, symbol="BTCUSDT", timeframe="1m", batch_id="api")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(query_engine=AnalyticsQueryEngine(tmp_path))))

    out = asyncio.run(get_bars(request, symbol="BTCUSDT", timeframe="1m", start_time_ms=None, end_time_ms=None,
                               limit=500))
    assert [b["time"] for b in out] == [(T0_MS + i * MIN_MS) // 1000 for i in range(5)]
    assert [b["interval_end_exclusive_ns"] for b in out] == [(T0_MS + (i + 1) * MIN_MS) * 10**6 for i in range(5)]
    assert [b["open"] for b in out] == [100.0 + i for i in range(5)]
