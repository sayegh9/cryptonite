"""The ``closed_bar_committed`` event names the committed bars by their OPEN (B1 follow-up to F-C1).

Bars are stamped at their CLOSE (``ts_event_ns``), but the event and its consumers (the chart's live candle,
the scanner watermark) speak the resampler's contract: ``bar_open_ns`` = the first bar's open,
``interval_end_exclusive_ns`` = the last bar's close, ``bar_close_inclusive_ns`` = that close - 1 ms. Before
the fix the event put the first bar's close in ``bar_open_ns`` and one step past the last close in
``interval_end_exclusive_ns``, so the chart (which keys its provisional candle on the open of the
``open_kline`` ticks) never matched it.

Runs the real ``TwoStoreCoordinator.commit_batch`` (Nautilus catalog + lake in tmp dirs) with a fake pool
that captures the outbox rows; no DB."""

import asyncio
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import pytest
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_schemas.identifiers import to_nautilus_bar_type, to_spot_instrument_id
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.coordinator import TwoStoreCoordinator
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager

MIN_MS = 60_000
T0_MS = 1_791_104_400_000  # 2026-10-04 09:00:00 UTC
CONTRACT = Path(__file__).resolve().parents[1] / "apps" / "web" / "e2e" / "fixtures" / "closed_bar_committed.json"


class FakeConn:
    def __init__(self, outbox):
        self.outbox = outbox

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql, *args):
        if "INSERT INTO outbox_events" in sql:
            self.outbox.append((args[1], json.loads(args[4])))
        return "OK"


class FakePool:
    def __init__(self):
        self.outbox: list[tuple[str, dict]] = []

    async def fetchrow(self, *a):
        return None  # no ingest checkpoint yet

    async def fetchval(self, *a):
        return None  # keeps the coordinator's batch id

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self.outbox)


def kline(open_ms: int, price: float, tf_ms: int = MIN_MS) -> RawBinanceKline:
    return RawBinanceKline(open_time_ms=open_ms, close_time_ms=open_ms + tf_ms - 1, open=f"{price:.2f}",
                           high=f"{price + 1:.2f}", low=f"{price - 1:.2f}", close=f"{price + 0.5:.2f}",
                           volume="2.5", quote_volume="250", trades_count=7, taker_buy_base_volume="1",
                           taker_buy_quote_volume="100")


def commit(tmp_path, klines: list[RawBinanceKline], timeframe: str = "1m") -> dict:
    inst = to_spot_instrument_id("BTCUSDT")
    bar_type = to_nautilus_bar_type(inst, timeframe)
    bars = [EnrichedBar.from_binance_raw(k, inst, str(bar_type), 2, 5, received_at_ns=(k.close_time_ms + 1) * 10**6)
            for k in klines]
    pool = FakePool()
    coord = TwoStoreCoordinator(NautilusCatalogManager(tmp_path / "nautilus"), AnalyticsLakeWriter(tmp_path / "lake"),
                                pool)  # type: ignore[arg-type]
    asyncio.run(coord.commit_batch(bars=bars, bar_type=bar_type, venue="BINANCE", market_type="SPOT",
                                   symbol="BTCUSDT", timeframe=timeframe))
    (event,) = [p for stream, p in pool.outbox if stream == "cryptonite:stream:closed_bar_committed"]
    return event


def test_a_live_closed_minute_is_named_by_its_open(tmp_path):
    """The recorder commits one closed kline per batch; the event must carry that kline's open, i.e. the
    same ``bar_open_ns`` as the ``open_kline`` ticks the chart built its provisional candle from."""
    k = kline(T0_MS, 100)
    event = commit(tmp_path, [k])
    open_tick_bar_open_ns = k.open_time_ms * 10**6  # what services/ingest stream.py publishes for this kline
    assert event["bar_open_ns"] == open_tick_bar_open_ns == T0_MS * 10**6
    assert event["last_bar_open_ns"] == T0_MS * 10**6
    assert event["interval_end_exclusive_ns"] == (T0_MS + MIN_MS) * 10**6
    assert event["bar_close_inclusive_ns"] == (T0_MS + MIN_MS) * 10**6 - 1_000_000
    assert event["bar_count"] == 1
    # the final values of that bar, so a consumer never depends on having seen its last tick
    assert {f: Decimal(event[f]) for f in ("open", "high", "low", "close", "volume")} == {
        "open": Decimal("100"), "high": Decimal("101"), "low": Decimal("99"), "close": Decimal("100.5"),
        "volume": Decimal("2.5")}


def test_the_event_matches_the_contract_fixture_the_chart_is_tested_with(tmp_path):
    """apps/web/e2e/fixtures/closed_bar_committed.json is what chart_units.spec.ts feeds the chart's
    closed-bar handler: it must be exactly what the coordinator emits (minus per-event ids and checksums)."""
    fixture = json.loads(CONTRACT.read_text())
    event = commit(tmp_path, [kline(T0_MS, 100)])
    volatile = {"occurred_at_ns", "event_id", "batch_id", "payload_checksum", "analytics_file_checksum",
                "nautilus_file_checksum"}
    assert {k: v for k, v in event.items() if k not in volatile} == fixture


def test_a_recovered_batch_spans_first_open_to_last_close(tmp_path):
    event = commit(tmp_path, [kline(T0_MS + i * MIN_MS, 100 + i) for i in range(3)])
    assert event["bar_open_ns"] == T0_MS * 10**6
    assert event["last_bar_open_ns"] == (T0_MS + 2 * MIN_MS) * 10**6
    assert event["interval_end_exclusive_ns"] == (T0_MS + 3 * MIN_MS) * 10**6
    assert event["bar_close_inclusive_ns"] == (T0_MS + 3 * MIN_MS) * 10**6 - 1_000_000
    assert event["bar_count"] == 3
    assert Decimal(event["close"]) == Decimal("102.5")  # the last bar's


@pytest.mark.parametrize("timeframe,tf_ms", [("1h", 3_600_000), ("15m", 900_000)])
def test_higher_timeframe_bars_use_their_own_step(tmp_path, timeframe, tf_ms):
    event = commit(tmp_path, [kline(T0_MS, 100, tf_ms)], timeframe)
    assert event["bar_open_ns"] == event["last_bar_open_ns"] == T0_MS * 10**6
    assert event["interval_end_exclusive_ns"] == (T0_MS + tf_ms) * 10**6


def test_the_corrected_event_is_schema_version_2(tmp_path):
    """Consumers tell the corrected meaning from a not-yet-redeployed producer's version-1 events."""
    assert commit(tmp_path, [kline(T0_MS, 100)])["schema_version"] == 2


@pytest.mark.parametrize("timeframe", ["1M", "1x", "m", ""])
def test_an_unknown_timeframe_is_refused_not_read_as_one_minute(tmp_path, timeframe):
    coord = TwoStoreCoordinator(NautilusCatalogManager(tmp_path / "n"), AnalyticsLakeWriter(tmp_path / "l"))
    with pytest.raises(ValueError):
        coord._timeframe_to_ns(timeframe)
