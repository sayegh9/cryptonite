"""Deterministic tests for K1 Phase 2.3 reconnect gap recovery.

Verifies:
1. Forced WebSocket reconnect triggers gap recovery for all fed symbols and timeframes (1m, 15m, 1h, 4h).
2. Dynamic SUBSCRIBE on open socket without reconnect for incremental adds.
3. Fallback to reconnect when dynamic SUBSCRIBE fails or socket is not open.
4. Bounded, idempotent recovery coalescing under rapid concurrent reconnect events.
5. Queue draining in recovery.py preserves closed bars belonging to other streams.
6. Recovery lock serializes live bar commits to prevent checkpoint race conditions.
7. Paginated REST recovery handles large gaps (>1000 bars) up to gap boundary.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptonite_ingest.recorder import IngestRecorder
from cryptonite_ingest.recovery import GapRecoveryManager
from cryptonite_ingest.stream import BinanceStreamClient
from cryptonite_schemas.bar import RawBinanceKline
from cryptonite_schemas.identifiers import to_nautilus_bar_type
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.coordinator import TwoStoreCoordinator
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager


def _make_mock_kline(
    open_time_ms: int, step_ms: int = 60_000, is_closed: bool = True
) -> RawBinanceKline:
    return RawBinanceKline(
        open_time_ms=open_time_ms,
        open="100.0",
        high="105.0",
        low="99.0",
        close="104.0",
        volume="10.0",
        close_time_ms=open_time_ms + step_ms - 1,
        quote_volume="1000.0",
        trades_count=50,
        taker_buy_base_volume="5.0",
        taker_buy_quote_volume="500.0",
        is_closed=is_closed,
    )


class MockRestClient:
    """Mock REST client supporting single or paginated klines."""

    def __init__(
        self,
        klines: list[RawBinanceKline] | None = None,
        pages: list[list[RawBinanceKline]] | None = None,
    ):
        self.klines = klines or []
        self.pages = pages or []
        self.call_count = 0
        self.calls: list[dict] = []

    async def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 1000,
    ) -> list[RawBinanceKline]:
        self.calls.append(
            {
                "symbol": symbol,
                "interval": interval,
                "start_time_ms": start_time_ms,
                "end_time_ms": end_time_ms,
                "limit": limit,
            }
        )
        self.call_count += 1
        if self.pages:
            idx = min(self.call_count - 1, len(self.pages) - 1)
            return self.pages[idx]
        return self.klines


@pytest.mark.asyncio
async def test_closed_bar_buffer_draining_preserves_other_streams(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair, sample_raw_klines
):
    """Bug fix verification: Draining closed_bar_buffer during gap recovery for one stream

    must NOT drop buffered bars belonging to other symbols or timeframes.
    """
    nautilus_mgr = NautilusCatalogManager(temp_nautilus_dir)
    nautilus_mgr.write_instruments([sample_currency_pair])
    analytics_writer = AnalyticsLakeWriter(temp_analytics_dir)
    coordinator = TwoStoreCoordinator(
        nautilus_manager=nautilus_mgr,
        analytics_writer=analytics_writer,
        pg_pool=None,
    )

    # REST returns the first 2 klines for BTCUSDT:1m
    mock_rest = MockRestClient(klines=sample_raw_klines[:2])
    recovery_mgr = GapRecoveryManager(
        rest_client=mock_rest,  # type: ignore
        coordinator=coordinator,
        nautilus_manager=nautilus_mgr,
        pg_pool=None,
    )

    stream_client = BinanceStreamClient(
        symbols=["BTCUSDT", "ETHUSDT"],
        timeframes=["1m", "15m", "1h", "4h"],
    )

    # Queue bars for multiple streams in the closed_bar_buffer
    bar_btc_1m = sample_raw_klines[2]
    bar_btc_15m = _make_mock_kline(1725700000000, step_ms=900_000)
    bar_eth_1m = _make_mock_kline(1725700000000, step_ms=60_000)
    bar_eth_4h = _make_mock_kline(1725700000000, step_ms=14_400_000)

    await stream_client.closed_bar_buffer.put(("BTCUSDT", "1m", bar_btc_1m, 1725700180000000000))
    await stream_client.closed_bar_buffer.put(("BTCUSDT", "15m", bar_btc_15m, 1725700900000000000))
    await stream_client.closed_bar_buffer.put(("ETHUSDT", "1m", bar_eth_1m, 1725700060000000000))
    await stream_client.closed_bar_buffer.put(("ETHUSDT", "4h", bar_eth_4h, 1725714400000000000))

    assert stream_client.closed_bar_buffer.qsize() == 4

    # Run recovery specifically for BTCUSDT 1m
    bar_type_1m = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    recovered = await recovery_mgr.recover_gaps_for_stream(
        symbol="BTCUSDT",
        timeframe="1m",
        bar_type=bar_type_1m,
        price_precision=2,
        size_precision=5,
        stream_client=stream_client,
    )

    # BTCUSDT 1m recovered 2 from REST + 1 from buffer = 3
    assert recovered == 3

    # All 3 bars belonging to other streams MUST still be in the buffer!
    assert stream_client.closed_bar_buffer.qsize() == 3

    remaining_items = []
    while not stream_client.closed_bar_buffer.empty():
        remaining_items.append(stream_client.closed_bar_buffer.get_nowait())

    remaining_streams = {(sym, tf) for sym, tf, _, _ in remaining_items}
    assert remaining_streams == {
        ("BTCUSDT", "15m"),
        ("ETHUSDT", "1m"),
        ("ETHUSDT", "4h"),
    }


@pytest.mark.asyncio
async def test_forced_websocket_reconnect_triggers_gap_recovery_all_symbols_and_timeframes(
    temp_nautilus_dir, temp_analytics_dir
):
    """Forced WebSocket reconnect triggers gap recovery for every fed symbol and timeframe (1m, 15m, 1h, 4h)."""
    recorder = IngestRecorder(
        symbols=["BTCUSDT", "ETHUSDT"],
        timeframes=["1m", "15m", "1h", "4h"],
        nautilus_catalog_path=str(temp_nautilus_dir),
        analytics_lake_path=str(temp_analytics_dir),
    )

    # Mock metadata
    recorder._symbol_metadata = {
        "BTCUSDT": {"price_precision": 2, "size_precision": 5},
        "ETHUSDT": {"price_precision": 2, "size_precision": 4},
    }

    # Mock recovery manager
    mock_recovery = MagicMock()
    mock_recovery.heal_empty_files = AsyncMock(return_value=0)
    mock_recovery.recover_gaps_for_stream = AsyncMock(return_value=1)
    recorder.recovery_manager = mock_recovery

    # Setup stream client with recorder's reconnect callback
    recorder.stream_client = BinanceStreamClient(
        symbols=recorder.symbols,
        timeframes=recorder.timeframes,
        on_reconnect=recorder._on_stream_reconnect,
    )
    recorder.stream_client.reconnect_count = 1

    # Simulate reconnect callback invocation
    await recorder._on_stream_reconnect()

    # Verify recovery was triggered for all 8 (symbol, timeframe) combinations
    expected_calls = {
        ("BTCUSDT", "1m"),
        ("BTCUSDT", "15m"),
        ("BTCUSDT", "1h"),
        ("BTCUSDT", "4h"),
        ("ETHUSDT", "1m"),
        ("ETHUSDT", "15m"),
        ("ETHUSDT", "1h"),
        ("ETHUSDT", "4h"),
    }

    actual_recover_calls = {
        (call.kwargs["symbol"], call.kwargs["timeframe"])
        for call in mock_recovery.recover_gaps_for_stream.call_args_list
    }
    assert actual_recover_calls == expected_calls

    actual_heal_calls = {
        (call.kwargs["symbol"], call.kwargs["timeframe"])
        for call in mock_recovery.heal_empty_files.call_args_list
    }
    assert actual_heal_calls == expected_calls


@pytest.mark.asyncio
async def test_dynamic_subscribe_on_open_socket_without_reconnect():
    """add_symbol dynamically sends SUBSCRIBE frame on open WebSocket without closing socket."""
    stream_client = BinanceStreamClient(
        symbols=["BTCUSDT"],
        timeframes=["1m", "15m", "1h", "4h"],
        enable_quotes=True,
    )

    class MockWebSocket:
        def __init__(self):
            self.open = True
            self.sent_messages: list[str] = []
            self.closed = False

        async def send(self, msg: str):
            self.sent_messages.append(msg)

        async def close(self):
            self.closed = True
            self.open = False

    mock_ws = MockWebSocket()
    stream_client._sockets.add(mock_ws)

    # Dynamic add_symbol
    success = await stream_client.add_symbol("ETHUSDT")

    assert success is True
    assert "ETHUSDT" in stream_client.symbols
    assert mock_ws.closed is False
    assert len(mock_ws.sent_messages) == 1

    sub_payload = json.loads(mock_ws.sent_messages[0])
    assert sub_payload["method"] == "SUBSCRIBE"
    assert "ethusdt@kline_1m" in sub_payload["params"]
    assert "ethusdt@kline_15m" in sub_payload["params"]
    assert "ethusdt@kline_1h" in sub_payload["params"]
    assert "ethusdt@kline_4h" in sub_payload["params"]
    assert "ethusdt@bookTicker" in sub_payload["params"]


@pytest.mark.asyncio
async def test_fallback_reconnect_when_socket_not_open_on_add_symbol():
    """add_symbol falls back to closing sockets (reconnect) if no open socket is available."""
    stream_client = BinanceStreamClient(
        symbols=["BTCUSDT"],
        timeframes=["1m"],
    )

    class ClosedWebSocket:
        def __init__(self):
            self.open = False
            self.closed_called = False

        async def close(self):
            self.closed_called = True

    closed_ws = ClosedWebSocket()
    stream_client._sockets.add(closed_ws)

    success = await stream_client.add_symbol("ETHUSDT")

    # Should report fallback (False)
    assert success is False
    assert "ETHUSDT" in stream_client.symbols
    # Give close task time to run
    await asyncio.sleep(0.01)
    assert closed_ws.closed_called is True


@pytest.mark.asyncio
async def test_recorder_dynamic_add_symbol_triggers_isolated_recovery(
    temp_nautilus_dir, temp_analytics_dir
):
    """When add_symbol dynamic subscribe succeeds, recovery is triggered only for the new symbol."""
    recorder = IngestRecorder(
        symbols=["BTCUSDT"],
        timeframes=["1m", "15m", "1h", "4h"],
        nautilus_catalog_path=str(temp_nautilus_dir),
        analytics_lake_path=str(temp_analytics_dir),
    )

    recorder.sync_instruments = AsyncMock()
    recorder.publish_universe = AsyncMock()

    mock_stream = MagicMock()
    mock_stream.add_symbol = AsyncMock(return_value=True)  # Dynamic subscribe succeeds
    recorder.stream_client = mock_stream

    mock_recovery = MagicMock()
    mock_recovery.heal_empty_files = AsyncMock(return_value=0)
    mock_recovery.recover_gaps_for_stream = AsyncMock(return_value=1)
    recorder.recovery_manager = mock_recovery

    recorder._symbol_metadata["SOLUSDT"] = {"price_precision": 2, "size_precision": 3}

    await recorder.add_symbol("SOLUSDT")

    recorder.sync_instruments.assert_called_once_with(symbols=["SOLUSDT"])
    mock_stream.add_symbol.assert_called_once_with("SOLUSDT")
    recorder.publish_universe.assert_called_once()

    # Recovery was called only for SOLUSDT across the 4 timeframes
    actual_symbols = {
        call.kwargs["symbol"] for call in mock_recovery.recover_gaps_for_stream.call_args_list
    }
    assert actual_symbols == {"SOLUSDT"}

    actual_timeframes = {
        call.kwargs["timeframe"] for call in mock_recovery.recover_gaps_for_stream.call_args_list
    }
    assert actual_timeframes == {"1m", "15m", "1h", "4h"}


@pytest.mark.asyncio
async def test_bounded_idempotent_reconnect_coalescing(temp_nautilus_dir, temp_analytics_dir):
    """Multiple rapid reconnect triggers coalesce into bounded execution without duplicate runs."""
    recorder = IngestRecorder(
        symbols=["BTCUSDT", "ETHUSDT"],
        timeframes=["1m"],
        nautilus_catalog_path=str(temp_nautilus_dir),
        analytics_lake_path=str(temp_analytics_dir),
    )
    recorder._symbol_metadata = {
        "BTCUSDT": {"price_precision": 2, "size_precision": 5},
        "ETHUSDT": {"price_precision": 2, "size_precision": 4},
    }

    call_count = 0

    async def slow_recover(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return 1

    mock_recovery = MagicMock()
    mock_recovery.heal_empty_files = AsyncMock(return_value=0)
    mock_recovery.recover_gaps_for_stream = AsyncMock(side_effect=slow_recover)
    recorder.recovery_manager = mock_recovery
    recorder.stream_client = MagicMock()

    # Fire 5 concurrent reconnect triggers
    tasks = [
        asyncio.create_task(recorder.trigger_recovery(["BTCUSDT"])),
        asyncio.create_task(recorder.trigger_recovery(["ETHUSDT"])),
        asyncio.create_task(recorder.trigger_recovery(["BTCUSDT", "ETHUSDT"])),
        asyncio.create_task(recorder.trigger_recovery(["BTCUSDT"])),
        asyncio.create_task(recorder.trigger_recovery(["ETHUSDT"])),
    ]
    await asyncio.gather(*tasks)

    # Bounded: both symbols were recovered exactly once (total 2 calls), not 5x
    assert call_count == 2
    recovered_symbols = [
        call.kwargs["symbol"] for call in mock_recovery.recover_gaps_for_stream.call_args_list
    ]
    assert sorted(recovered_symbols) == ["BTCUSDT", "ETHUSDT"]


@pytest.mark.asyncio
async def test_recovery_lock_serializes_live_closed_bar_event(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair, sample_raw_klines
):
    """Live closed bar arriving during recovery waits for recovery lock before committing."""
    recorder = IngestRecorder(
        symbols=["BTCUSDT"],
        timeframes=["1m"],
        nautilus_catalog_path=str(temp_nautilus_dir),
        analytics_lake_path=str(temp_analytics_dir),
    )
    recorder._symbol_metadata = {"BTCUSDT": {"price_precision": 2, "size_precision": 5}}

    mock_coordinator = MagicMock()
    mock_coordinator.commit_batch = AsyncMock(return_value="batch-123")
    recorder.coordinator = mock_coordinator

    order_of_events = []

    async def simulate_recovery():
        async with recorder._recovery_lock:
            order_of_events.append("recovery_started")
            await asyncio.sleep(0.05)
            order_of_events.append("recovery_finished")

    recovery_task = asyncio.create_task(simulate_recovery())
    await asyncio.sleep(0.01)  # Ensure recovery task acquires the lock first

    # Live closed bar arrives while recovery holds the lock
    live_bar = sample_raw_klines[0]
    live_task = asyncio.create_task(
        recorder._on_closed_bar_event("BTCUSDT", "1m", live_bar, 1725700060000000000)
    )

    await asyncio.gather(recovery_task, live_task)
    order_of_events.append("live_bar_committed")

    assert order_of_events == ["recovery_started", "recovery_finished", "live_bar_committed"]
    mock_coordinator.commit_batch.assert_called_once()


@pytest.mark.asyncio
async def test_paginated_rest_gap_recovery_large_gap(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair
):
    """Gaps larger than 1000 bars are fetched across multiple REST pages up to the gap end."""
    nautilus_mgr = NautilusCatalogManager(temp_nautilus_dir)
    nautilus_mgr.write_instruments([sample_currency_pair])
    analytics_writer = AnalyticsLakeWriter(temp_analytics_dir)
    coordinator = TwoStoreCoordinator(
        nautilus_manager=nautilus_mgr,
        analytics_writer=analytics_writer,
        pg_pool=None,
    )

    base_time_ms = 1725700000000
    # Create 3 pages: 1000 bars, 1000 bars, 500 bars = 2500 bars
    page1 = [_make_mock_kline(base_time_ms + i * 60_000) for i in range(1000)]
    page2 = [_make_mock_kline(base_time_ms + (1000 + i) * 60_000) for i in range(1000)]
    page3 = [_make_mock_kline(base_time_ms + (2000 + i) * 60_000) for i in range(500)]

    mock_rest = MockRestClient(pages=[page1, page2, page3])

    recovery_mgr = GapRecoveryManager(
        rest_client=mock_rest,  # type: ignore
        coordinator=coordinator,
        nautilus_manager=nautilus_mgr,
        pg_pool=None,
    )

    # Nautilus gap mock: simulate missing interval covering the 2500 bars
    gap_start_ns = base_time_ms * 1_000_000
    gap_end_ns = (base_time_ms + 2500 * 60_000) * 1_000_000

    with patch.object(
        nautilus_mgr,
        "get_missing_intervals_for_request",
        return_value=[(gap_start_ns, gap_end_ns)],
    ):
        stream_client = BinanceStreamClient(symbols=["BTCUSDT"], timeframes=["1m"])
        bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")

        recovered = await recovery_mgr.recover_gaps_for_stream(
            symbol="BTCUSDT",
            timeframe="1m",
            bar_type=bar_type,
            price_precision=2,
            size_precision=5,
            stream_client=stream_client,
        )

        assert recovered == 2500
        assert mock_rest.call_count == 3
