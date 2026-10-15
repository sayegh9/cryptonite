"""Quotes keep flowing while gap recovery runs (2026-10-15 stop-loss blindness).

On every reconnect of a flaky network the recorder runs gap recovery under ``_recovery_lock``. A closed
bar arriving meanwhile used to wait for that lock inside the WebSocket receive loop, so no quote was
published for the whole recovery (~14 s, ~300 times a day) and the exit monitor skipped stops on the
stale quotes. Closed bars now wait on their own task; and a reconnect with no closed bar missed costs no
REST call.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

from cryptonite_ingest.recorder import IngestRecorder
from cryptonite_ingest.recovery import GapRecoveryManager
from cryptonite_ingest.stream import BinanceStreamClient
from cryptonite_schemas.bar import RawBinanceKline
from cryptonite_schemas.identifiers import to_nautilus_bar_type

MINUTE_NS = 60_000_000_000


def closed_kline_msg(symbol: str = "BTCUSDT", open_ms: int = 1725700000000) -> str:
    return json.dumps({"stream": f"{symbol.lower()}@kline_1m", "data": {
        "e": "kline", "E": open_ms + 60_000, "s": symbol, "k": {
            "t": open_ms, "T": open_ms + 59_999, "s": symbol, "i": "1m", "o": "1", "c": "2", "h": "3",
            "l": "0.5", "v": "10", "n": 5, "x": True, "q": "20", "V": "4", "Q": "8", "B": "0"}}})


def quote_msg(symbol: str = "BTCUSDT", bid: str = "100.0") -> str:
    return json.dumps({"stream": f"{symbol.lower()}@bookTicker",
                       "data": {"u": 1, "s": symbol, "b": bid, "B": "1", "a": "100.1", "A": "1"}})


def recorder_with_slow_recovery(temp_nautilus_dir, temp_analytics_dir, hold_s: float):
    recorder = IngestRecorder(symbols=["BTCUSDT", "XRPUSDT"], timeframes=["1m", "15m", "1h", "4h"],
                              nautilus_catalog_path=str(temp_nautilus_dir),
                              analytics_lake_path=str(temp_analytics_dir))
    recorder._symbol_metadata = {"BTCUSDT": {"price_precision": 2, "size_precision": 5},
                                 "XRPUSDT": {"price_precision": 4, "size_precision": 1}}
    recorder.coordinator = MagicMock()
    recorder.coordinator.commit_batch = AsyncMock(return_value="batch")

    async def slow_recover(**kwargs):
        await asyncio.sleep(hold_s / 8)  # 2 symbols x 4 timeframes: the whole recovery takes hold_s
        return 0

    recorder.recovery_manager = MagicMock()
    recorder.recovery_manager.heal_empty_files = AsyncMock(return_value=0)
    recorder.recovery_manager.recover_gaps_for_stream = AsyncMock(side_effect=slow_recover)
    recorder.stream_client = BinanceStreamClient(symbols=recorder.symbols, timeframes=recorder.timeframes,
                                                 on_closed_bar=recorder._on_closed_bar_event,
                                                 on_reconnect=recorder._on_stream_reconnect)
    return recorder


async def test_quotes_keep_flowing_while_a_closed_bar_waits_for_recovery(temp_nautilus_dir, temp_analytics_dir):
    recorder = recorder_with_slow_recovery(temp_nautilus_dir, temp_analytics_dir, hold_s=0.8)
    client = recorder.stream_client
    recovery = asyncio.create_task(recorder._on_stream_reconnect())
    await asyncio.sleep(0)  # recovery holds the lock, as it does right after a reconnect
    assert recorder._recovery_lock.locked()

    # A closed bar, then quotes, all arriving mid-recovery: none of them may wait for the lock
    started = time.monotonic()
    await asyncio.wait_for(client._handle_message(closed_kline_msg(), time.time_ns()), timeout=0.1)
    for i in range(5):
        await asyncio.wait_for(client._handle_message(quote_msg(bid=f"10{i}.0"), time.time_ns()), timeout=0.1)
    assert time.monotonic() - started < 0.2
    assert client.quote_sequences["BTCUSDT"] == 5  # every quote handled while recovery still runs
    assert recorder._recovery_lock.locked()
    recorder.coordinator.commit_batch.assert_not_called()  # the bar still waits: subscribe-before-backfill

    await recovery
    await asyncio.wait_for(client._closed_bar_dispatch.join(), timeout=1)
    recorder.coordinator.commit_batch.assert_awaited_once()  # committed after recovery released the lock


async def test_reconnect_storm_never_stalls_quotes(temp_nautilus_dir, temp_analytics_dir):
    """Reconnects every ~0.1 s, recoveries overlapping, bars closing: the longest quote gap stays tiny."""
    recorder = recorder_with_slow_recovery(temp_nautilus_dir, temp_analytics_dir, hold_s=0.4)
    client = recorder.stream_client
    gaps, last = [], time.monotonic()
    recoveries = []
    for tick in range(60):
        if tick % 10 == 0:
            recoveries.append(asyncio.create_task(recorder._on_stream_reconnect()))
        if tick % 7 == 0:
            await client._handle_message(closed_kline_msg(open_ms=1725700000000 + tick * 60_000), time.time_ns())
        await client._handle_message(quote_msg(symbol="XRPUSDT", bid="1.30"), time.time_ns())
        now = time.monotonic()
        gaps.append(now - last)
        last = now
        await asyncio.sleep(0.01)
    assert max(gaps) < 0.1, f"a quote waited {max(gaps):.2f}s"
    assert client.quote_sequences["XRPUSDT"] == 60
    await asyncio.gather(*recoveries)
    await asyncio.wait_for(client._closed_bar_dispatch.join(), timeout=2)
    assert recorder.coordinator.commit_batch.await_count == 9  # every closed bar still committed, in order
    committed = [c.kwargs["bars"][0].ts_event_ns for c in recorder.coordinator.commit_batch.await_args_list]
    assert committed == sorted(committed)


def manager_with_checkpoint(checkpoint_ns: int | None):
    rest = MagicMock()
    rest.fetch_klines = AsyncMock(return_value=[])
    nautilus = MagicMock()
    nautilus.get_missing_intervals_for_request = MagicMock(return_value=[(0, 1)])
    mgr = GapRecoveryManager(rest_client=rest, coordinator=MagicMock(), nautilus_manager=nautilus, pg_pool=None)
    mgr.get_durable_checkpoint = AsyncMock(return_value=checkpoint_ns)
    return mgr, rest, nautilus


async def recover(mgr, stream, timeframe: str = "1m", now_ns: int = 1725700000000 * 1_000_000 + 30 * 10**9):
    with patch("cryptonite_ingest.recovery.time.time_ns", return_value=now_ns):
        return await mgr.recover_gaps_for_stream(
            symbol="BTCUSDT", timeframe=timeframe, bar_type=to_nautilus_bar_type("BTCUSDT.BINANCE", timeframe),
            price_precision=2, size_precision=5, stream_client=stream)


async def test_a_reconnect_with_no_missed_bar_makes_no_rest_call():
    now_ns = 1725700000000 * 1_000_000 + 30 * 10**9  # 30 s into a minute
    last_close = now_ns // MINUTE_NS * MINUTE_NS
    stream = BinanceStreamClient(symbols=["BTCUSDT"], timeframes=["1m"])
    raw = MagicMock(spec=RawBinanceKline)
    await stream.closed_bar_buffer.put(("BTCUSDT", "1m", raw, 1))  # this stream's bar, committed live
    await stream.closed_bar_buffer.put(("XRPUSDT", "1m", raw, 2))  # another stream's: kept for its recovery

    mgr, rest, nautilus = manager_with_checkpoint(last_close)
    assert await recover(mgr, stream, now_ns=now_ns) == 0
    rest.fetch_klines.assert_not_called()
    nautilus.get_missing_intervals_for_request.assert_not_called()
    assert stream.closed_bar_buffer.get_nowait()[0] == "XRPUSDT" and stream.closed_bar_buffer.empty()


async def test_a_missed_bar_is_still_recovered_from_rest():
    now_ns = 1725700000000 * 1_000_000 + 30 * 10**9
    last_close = now_ns // MINUTE_NS * MINUTE_NS
    stream = BinanceStreamClient(symbols=["BTCUSDT"], timeframes=["1m"])
    mgr, rest, nautilus = manager_with_checkpoint(last_close - MINUTE_NS)  # one closed bar not committed
    await recover(mgr, stream, now_ns=now_ns)
    nautilus.get_missing_intervals_for_request.assert_called_once()
    rest.fetch_klines.assert_awaited()

    mgr, rest, _ = manager_with_checkpoint(None)  # no checkpoint yet: the full lookback, as before
    await recover(mgr, stream, now_ns=now_ns)
    rest.fetch_klines.assert_awaited()


async def test_the_no_gap_check_uses_each_timeframes_own_boundary():
    four_h = 4 * 3600 * 10**9
    now_ns = 1725700000000 * 1_000_000 + 30 * 10**9
    stream = BinanceStreamClient(symbols=["BTCUSDT"], timeframes=["4h"])
    mgr, rest, _ = manager_with_checkpoint(now_ns // four_h * four_h)  # the newest 4h bar is committed
    await recover(mgr, stream, timeframe="4h", now_ns=now_ns)
    rest.fetch_klines.assert_not_called()
    mgr, rest, _ = manager_with_checkpoint(now_ns // four_h * four_h - four_h)
    await recover(mgr, stream, timeframe="4h", now_ns=now_ns)
    rest.fetch_klines.assert_awaited()
