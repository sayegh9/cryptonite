"""Tests for subscribe-before-backfill gap recovery and interval agreement."""

import pytest
from cryptonite_ingest.recovery import GapRecoveryManager
from cryptonite_ingest.stream import BinanceStreamClient
from cryptonite_schemas.identifiers import to_nautilus_bar_type
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.coordinator import TwoStoreCoordinator
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from cryptonite_storage.queries import AnalyticsQueryEngine


class MockRestClient:
    """Mock Binance REST client for offline gap recovery testing."""

    def __init__(self, klines_to_return):
        self._klines = klines_to_return

    async def fetch_klines(self, symbol, interval, start_time_ms=None, end_time_ms=None, limit=1000):
        return self._klines


@pytest.mark.asyncio
async def test_subscribe_before_backfill_recovery(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair, sample_raw_klines
):
    nautilus_mgr = NautilusCatalogManager(temp_nautilus_dir)
    nautilus_mgr.write_instruments([sample_currency_pair])

    analytics_writer = AnalyticsLakeWriter(temp_analytics_dir)
    coordinator = TwoStoreCoordinator(
        nautilus_manager=nautilus_mgr,
        analytics_writer=analytics_writer,
        pg_pool=None,
    )

    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")

    # 1. Setup mock REST client with the first 2 klines as historical gap
    mock_rest = MockRestClient(sample_raw_klines[:2])

    recovery_mgr = GapRecoveryManager(
        rest_client=mock_rest,  # type: ignore
        coordinator=coordinator,
        nautilus_manager=nautilus_mgr,
        pg_pool=None,
    )

    # 2. Setup mock stream client that buffered the 3rd kline while REST was running
    stream_client = BinanceStreamClient(symbols=["BTCUSDT"], timeframes=["1m"])
    # Put 3rd kline in buffer as if it arrived live
    third_kline = sample_raw_klines[2]
    await stream_client.closed_bar_buffer.put(
        ("BTCUSDT", "1m", third_kline, 1725700180000000000)
    )

    # 3. Execute gap recovery
    recovered_count = await recovery_mgr.recover_gaps_for_stream(
        symbol="BTCUSDT",
        timeframe="1m",
        bar_type=bar_type,
        price_precision=2,
        size_precision=5,
        stream_client=stream_client,
    )

    # Total 3 bars: 2 from REST backfill + 1 merged from live buffer
    assert recovered_count == 3

    # 4. Verify data in Analytics Lake
    query_engine = AnalyticsQueryEngine(temp_analytics_dir)
    assert query_engine.count_bars("BTCUSDT", "1m") == 3

    # 5. Verify interval agreement: now no missing intervals exist for that range!
    intervals = nautilus_mgr.get_intervals("bars", str(bar_type))
    assert len(intervals) >= 1
    start_ns, end_ns = intervals[0]
    missing = nautilus_mgr.get_missing_intervals_for_request(
        start=start_ns,
        end=end_ns,
        data_cls="bars",
        identifier=str(bar_type),
    )
    assert len(missing) == 0
