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


@pytest.mark.asyncio
async def test_rest_client_reports_the_still_forming_kline_as_open(monkeypatch):
    """A range ending "now" returns the live kline last; it must not claim to be closed."""
    import time

    import httpx
    from cryptonite_ingest.client import BinanceRestClient

    now_ms = int(time.time() * 1000)

    def row(open_ms, close_ms):
        return [open_ms, "1", "2", "0.5", "1.5", "10", close_ms, "15", 3, "5", "7", "0"]

    rows = [row(now_ms - 7_200_000, now_ms - 3_600_001), row(now_ms - 1_800_000, now_ms + 1_799_999)]
    client = BinanceRestClient()
    client._rate_delay = 0
    client._client = httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(lambda req: httpx.Response(200, json=rows)))
    klines = await client.fetch_klines("BTCUSDT", "1h", start_time_ms=now_ms - 7_200_000, end_time_ms=now_ms)
    await client._client.aclose()
    assert [k.is_closed for k in klines] == [True, False]


@pytest.mark.asyncio
async def test_recovery_never_commits_the_open_kline(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair, sample_raw_klines
):
    """Committing the still-forming bar moved the checkpoint past it, so the stream's real closed bar
    was skipped as "older than checkpoint" (workstation, 2026-10-03)."""
    nautilus_mgr = NautilusCatalogManager(temp_nautilus_dir)
    nautilus_mgr.write_instruments([sample_currency_pair])
    coordinator = TwoStoreCoordinator(
        nautilus_manager=nautilus_mgr, analytics_writer=AnalyticsLakeWriter(temp_analytics_dir), pg_pool=None,
    )
    still_open = sample_raw_klines[2].model_copy(update={"is_closed": False})
    recovery_mgr = GapRecoveryManager(
        rest_client=MockRestClient([*sample_raw_klines[:2], still_open]),  # type: ignore
        coordinator=coordinator, nautilus_manager=nautilus_mgr, pg_pool=None,
    )
    recovered = await recovery_mgr.recover_gaps_for_stream(
        symbol="BTCUSDT", timeframe="1m", bar_type=to_nautilus_bar_type("BTCUSDT.BINANCE", "1m"),
        price_precision=2, size_precision=5,
        stream_client=BinanceStreamClient(symbols=["BTCUSDT"], timeframes=["1m"]),
    )
    assert recovered == 2
    assert AnalyticsQueryEngine(temp_analytics_dir).count_bars("BTCUSDT", "1m") == 2


# --- self-healing: empty files left by a killed writer --------------------------------------------


def _write_one_file_per_bar(nautilus_mgr, bar_type, klines):
    from cryptonite_schemas.bar import EnrichedBar, bar_to_nautilus

    for k in klines:
        b = EnrichedBar.from_binance_raw(raw=k, instrument_id="BTCUSDT.BINANCE", bar_type=str(bar_type),
                                         price_precision=2, size_precision=5, received_at_ns=0)
        nautilus_mgr.write_bars([bar_to_nautilus(b, bar_type)])


def _truncate_file_for(nautilus_mgr, bar_type, ts_ns):
    from cryptonite_storage.nautilus_catalog import _file_interval

    for f in (nautilus_mgr.catalog_path / "data" / "bars" / str(bar_type)).glob("*.parquet"):
        if _file_interval(f.name) == (ts_ns, ts_ns):
            f.write_bytes(b"")
            return f
    raise AssertionError("no file for that bar")


def test_quarantine_moves_empty_files_aside_and_exposes_their_gap(temp_nautilus_dir, sample_currency_pair, sample_raw_klines):
    nautilus_mgr = NautilusCatalogManager(temp_nautilus_dir / "cat")
    nautilus_mgr.write_instruments([sample_currency_pair])
    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    _write_one_file_per_bar(nautilus_mgr, bar_type, sample_raw_klines[:3])
    ts = sample_raw_klines[2].open_time_ms * 1_000_000  # a bar's ts_event is its close = next kline's open
    broken = _truncate_file_for(nautilus_mgr, bar_type, ts)

    assert nautilus_mgr.quarantine_empty_bar_files(str(bar_type)) == [(ts, ts)]
    assert not broken.exists()
    moved = list((temp_nautilus_dir / "catalog_quarantine" / str(bar_type)).iterdir())
    assert len(moved) == 1 and moved[0].name.startswith(broken.name) and moved[0].stat().st_size == 0
    assert all(not (s <= ts <= e) for s, e in nautilus_mgr.get_intervals("bars", str(bar_type)))
    assert nautilus_mgr.quarantine_empty_bar_files(str(bar_type)) == []  # nothing left to move


@pytest.mark.asyncio
async def test_heal_empty_files_refills_the_bar_into_the_catalog_only(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair, sample_raw_klines
):
    nautilus_mgr = NautilusCatalogManager(temp_nautilus_dir / "cat")
    nautilus_mgr.write_instruments([sample_currency_pair])
    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    _write_one_file_per_bar(nautilus_mgr, bar_type, sample_raw_klines[:3])
    ts = sample_raw_klines[2].open_time_ms * 1_000_000
    _truncate_file_for(nautilus_mgr, bar_type, ts)

    analytics_writer = AnalyticsLakeWriter(temp_analytics_dir)
    coordinator = TwoStoreCoordinator(nautilus_manager=nautilus_mgr, analytics_writer=analytics_writer, pg_pool=None)
    mgr = GapRecoveryManager(rest_client=MockRestClient(sample_raw_klines[:3]), coordinator=coordinator,  # type: ignore
                             nautilus_manager=nautilus_mgr, pg_pool=None)

    assert await mgr.heal_empty_files("BTCUSDT", "1m", bar_type, 2, 5) == 1
    got = [int(b.ts_event) for b in nautilus_mgr.query_bars(identifiers=[str(bar_type)])]
    assert ts in got and len(got) == len(set(got)) == 3
    assert AnalyticsQueryEngine(temp_analytics_dir).count_bars("BTCUSDT", "1m") == 0  # lake untouched
    assert await mgr.heal_empty_files("BTCUSDT", "1m", bar_type, 2, 5) == 0  # idempotent
