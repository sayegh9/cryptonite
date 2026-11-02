"""H7b: 1m lake gaps are closed (recovery, sweep, repair) and the continuity check reports what debates see.

Deterministic, no network: REST is a fake serving klines from a dict; the lake is a temp directory.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptonite_ingest import recorder as recorder_mod
from cryptonite_ingest.lake_gaps import LakeGapFiller, _fmt, check_continuity, fetch_windows
from cryptonite_ingest.recorder import IngestRecorder
from cryptonite_ingest.recovery import GapRecoveryManager
from cryptonite_ingest.stream import BinanceStreamClient
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_schemas.identifiers import to_nautilus_bar_type
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.queries import AnalyticsQueryEngine

BASE_MS = 1_725_699_960_000  # a minute boundary
SYM = "BTCUSDT"
BAR_TYPE = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")


def kline(i: int, *, price: float = 100.0, closed: bool = True) -> RawBinanceKline:
    """The kline that opens i minutes after BASE_MS (its lake bar closes at minute i + 1)."""
    open_ms = BASE_MS + i * 60_000
    p = f"{price + i:.2f}"
    return RawBinanceKline(
        open_time_ms=open_ms, open=p, high=p, low=p, close=p, volume="1.0", close_time_ms=open_ms + 59_999,
        quote_volume="100.0", trades_count=5, taker_buy_base_volume="0.5", taker_buy_quote_volume="50.0",
        is_closed=closed,
    )


def close_ns(i: int) -> int:
    return (BASE_MS + (i + 1) * 60_000) * 1_000_000


def bar(k: RawBinanceKline, received_ns: int = 1) -> EnrichedBar:
    return EnrichedBar.from_binance_raw(raw=k, instrument_id="BTCUSDT.BINANCE", bar_type=str(BAR_TYPE),
                                        price_precision=2, size_precision=6, received_at_ns=received_ns)


def write_lake(lake, minutes, price: float = 100.0) -> None:
    AnalyticsLakeWriter(lake).write_batch([bar(kline(i, price=price)) for i in minutes], venue="BINANCE",
                                          market_type="SPOT", timeframe="1m", symbol=SYM)


def lake_rows(lake) -> list[tuple[int, float]]:
    engine = AnalyticsQueryEngine(lake)
    try:
        rows = engine._con.execute(
            f"SELECT ts_event_ns::BIGINT, close::DOUBLE FROM read_parquet('{engine._get_glob_path(SYM)}') ORDER BY 1"
        ).fetchall()
    finally:
        engine._con.close()
    return [(int(t), float(c)) for t, c in rows]


class FakeRest:
    """Binance klines by OPEN time in [start, end]; ``on_fetch`` runs before answering (a concurrent writer)."""

    def __init__(self, klines: dict[int, RawBinanceKline], on_fetch=None):
        self.klines = klines
        self.calls: list[tuple[int, int]] = []
        self.on_fetch = on_fetch

    async def fetch_klines(self, symbol, interval, start_time_ms=None, end_time_ms=None, limit=1000):
        self.calls.append((start_time_ms, end_time_ms))
        if self.on_fetch:
            self.on_fetch()
        return [k for t, k in sorted(self.klines.items()) if start_time_ms <= t <= end_time_ms][:limit]


def rest_for(minutes, price: float = 100.0, on_fetch=None) -> FakeRest:
    return FakeRest({BASE_MS + i * 60_000: kline(i, price=price) for i in minutes}, on_fetch)


# --- recovery ---------------------------------------------------------------------------------------------------

def recovery_with_checkpoint(lake, checkpoint_ns: int, rest_klines: list[RawBinanceKline], now_ns: int):
    """Recovery with the coordinator's live checkpoint guard: a batch starting at or before it is skipped."""
    writer = AnalyticsLakeWriter(lake)

    async def commit_batch(bars, **kw):
        bars = sorted(bars, key=lambda b: b.ts_event_ns)
        if bars[0].ts_event_ns <= checkpoint_ns:
            return ""
        writer.write_batch(bars, venue="BINANCE", market_type="SPOT", timeframe="1m", symbol=SYM)
        return "batch"

    coordinator = MagicMock(commit_batch=AsyncMock(side_effect=commit_batch))
    nautilus = MagicMock(get_missing_intervals_for_request=MagicMock(return_value=[(checkpoint_ns + 1, now_ns)]))
    rest = MagicMock(fetch_klines=AsyncMock(return_value=rest_klines))
    mgr = GapRecoveryManager(rest_client=rest, coordinator=coordinator, nautilus_manager=nautilus)
    mgr.get_durable_checkpoint = AsyncMock(return_value=checkpoint_ns)
    return mgr


async def recover(mgr, stream, now_ns):
    with patch("cryptonite_ingest.recovery.time.time_ns", return_value=now_ns):
        return await mgr.recover_gaps_for_stream(symbol=SYM, timeframe="1m", bar_type=BAR_TYPE, price_precision=2,
                                                 size_precision=6, stream_client=stream)


async def test_stale_buffered_bars_no_longer_make_recovery_skip_the_gap(temp_analytics_dir):
    """Production: the buffer held ~240 bars committed live since the last reconnect; the merged batch started
    before the checkpoint, was skipped whole, and recovery still logged 'recovered 250 bars'."""
    stream = BinanceStreamClient(symbols=[SYM], timeframes=["1m"])
    for i in range(3):  # committed live long ago
        await stream.closed_bar_buffer.put((SYM, "1m", kline(i), 1))
    checkpoint = close_ns(2)
    mgr = recovery_with_checkpoint(temp_analytics_dir, checkpoint, [kline(3), kline(4, closed=False)],
                                   now_ns=close_ns(3) + 9 * 10**9)
    assert await recover(mgr, stream, close_ns(3) + 9 * 10**9) == 1
    assert [t for t, _ in lake_rows(temp_analytics_dir)] == [close_ns(3)]


async def test_recovery_after_a_disconnect_fills_the_minute_in_progress(temp_analytics_dir):
    stream = BinanceStreamClient(symbols=[SYM], timeframes=["1m"])
    checkpoint = close_ns(2)
    # reconnect 30 s into minute 3: REST only has it open, nothing to commit yet
    mgr = recovery_with_checkpoint(temp_analytics_dir, checkpoint, [kline(3, closed=False)], close_ns(2) + 30 * 10**9)
    assert await recover(mgr, stream, close_ns(2) + 30 * 10**9) == 0
    # its live bar never arrives: the run just after the minute closed refetches it
    mgr = recovery_with_checkpoint(temp_analytics_dir, checkpoint, [kline(3), kline(4, closed=False)],
                                   close_ns(3) + 10 * 10**9)
    assert await recover(mgr, stream, close_ns(3) + 10 * 10**9) == 1
    assert [t for t, _ in lake_rows(temp_analytics_dir)] == [close_ns(3)]


async def test_reconnect_schedules_one_more_recovery_after_the_minute_closes(
    monkeypatch, temp_nautilus_dir, temp_analytics_dir
):
    rec = IngestRecorder(symbols=[SYM], timeframes=["1m"], nautilus_catalog_path=str(temp_nautilus_dir),
                         analytics_lake_path=str(temp_analytics_dir))
    rec.trigger_recovery = AsyncMock()
    monkeypatch.setattr(recorder_mod, "RECONNECT_RECHECK_DELAY_S", 0.0)
    monkeypatch.setattr(recorder_mod.time, "time", lambda: 1_725_700_019.98)  # 20 ms before a minute closes
    await rec._on_stream_reconnect()
    await rec._on_stream_reconnect()  # a second reconnect meanwhile does not stack another recheck
    await asyncio.wait_for(rec._recheck_task, 1)
    assert rec.trigger_recovery.await_count == 3


# --- sweep / repair ---------------------------------------------------------------------------------------------

async def test_sweep_fills_a_short_and_a_25_minute_hole(temp_analytics_dir):
    holes = {10, 11} | set(range(50, 75))
    write_lake(temp_analytics_dir, [i for i in range(120) if i not in holes])
    rest = rest_for(range(130))
    filler = LakeGapFiller(rest, temp_analytics_dir, pause_s=0)
    result = await filler.fill(SYM, close_ns(0), close_ns(119), 2, 6, now_ns=close_ns(125))
    assert (result.missing, result.filled, result.rest_calls, result.unfilled) == (27, 27, 1, [])
    assert lake_rows(temp_analytics_dir) == [(close_ns(i), 100.0 + i) for i in range(120)]


async def test_recorder_sweep_covers_every_fed_symbol(temp_nautilus_dir, temp_analytics_dir):
    write_lake(temp_analytics_dir, [i for i in range(30) if i != 7])
    rec = IngestRecorder(symbols=[SYM], timeframes=["1m"], nautilus_catalog_path=str(temp_nautilus_dir),
                         analytics_lake_path=str(temp_analytics_dir))
    filler = LakeGapFiller(rest_for(range(40)), temp_analytics_dir, pause_s=0)
    [result] = await rec.sweep_gaps(filler, hours=6, now_ns=close_ns(35))
    assert result.filled == 1 and len(lake_rows(temp_analytics_dir)) == 30


async def test_rerunning_the_repair_adds_nothing_and_changes_nothing(temp_analytics_dir):
    write_lake(temp_analytics_dir, [i for i in range(60) if i not in (5, 6, 30)])
    await LakeGapFiller(rest_for(range(60)), temp_analytics_dir, pause_s=0).fill(
        SYM, close_ns(0), close_ns(59), 2, 6, now_ns=close_ns(70))
    before = lake_rows(temp_analytics_dir)
    # REST now disagrees on every value: existing minutes are never refetched or rewritten
    rest = rest_for(range(60), price=999.0)
    again = await LakeGapFiller(rest, temp_analytics_dir, pause_s=0).fill(
        SYM, close_ns(0), close_ns(59), 2, 6, now_ns=close_ns(70))
    assert (again.missing, again.filled, rest.calls) == (0, 0, [])
    assert lake_rows(temp_analytics_dir) == before and len(before) == 60


async def test_a_concurrent_live_bar_is_not_overwritten_and_the_open_edge_is_left_to_live(temp_analytics_dir):
    write_lake(temp_analytics_dir, [i for i in range(20) if i != 9])
    # while the sweep waits on REST, another writer lands minute 9 with its own values
    rest = rest_for(range(25), price=500.0,
                    on_fetch=lambda: write_lake(temp_analytics_dir, [9], price=100.0))
    result = await LakeGapFiller(rest, temp_analytics_dir, pause_s=0).fill(
        SYM, close_ns(0), close_ns(24), 2, 6, now_ns=close_ns(24))
    rows = lake_rows(temp_analytics_dir)
    assert result.filled == 0
    assert [r for r in rows if r[0] == close_ns(9)] == [(close_ns(9), 109.0)]
    assert rows[-1][0] == close_ns(19)  # minutes after the newest lake bar belong to live ingest / recovery


async def test_only_closed_klines_are_written(temp_analytics_dir):
    write_lake(temp_analytics_dir, [0, 2])
    rest = FakeRest({BASE_MS + 60_000: kline(1, closed=False)})
    result = await LakeGapFiller(rest, temp_analytics_dir, pause_s=0).fill(
        SYM, close_ns(0), close_ns(2), 2, 6, now_ns=close_ns(3))
    assert result.filled == 0 and result.unfilled == [(close_ns(1), close_ns(1))]


def test_rest_windows_stay_within_one_page():
    missing = [close_ns(i) for i in (*range(0, 5), 900, 999, 1000, 2500)]
    assert fetch_windows(missing) == [(close_ns(0), close_ns(999)), (close_ns(1000), close_ns(1000)),
                                      (close_ns(2500), close_ns(2500))]


# --- continuity -------------------------------------------------------------------------------------------------

INTRADAY_MINUTES = 3060  # 204 complete 15m bars


@pytest.fixture
def intraday_lake(temp_analytics_dir):
    start = (-BASE_MS // 60_000) % 15  # first minute index on a 15m boundary
    minutes = range(start, start + INTRADAY_MINUTES)
    return temp_analytics_dir, minutes


def test_continuity_passes_on_a_complete_lake_and_names_the_gap_on_a_holed_one(intraday_lake):
    lake, minutes = intraday_lake
    now = close_ns(minutes[-1])
    write_lake(lake, [i for i in minutes if i != minutes[-1] - 600])
    holed = check_continuity(lake, SYM, "INTRADAY", now_ns=now)
    assert not holed.ok
    missing_close = close_ns(minutes[-1] - 600)
    assert any("15m bar(s) missing" in p for p in holed.problems)
    assert any(_fmt(missing_close) in p for p in holed.problems if p.startswith("1m close stamps missing"))

    write_lake(lake, [minutes[-1] - 600])
    ok = check_continuity(lake, SYM, "INTRADAY", now_ns=now)
    assert ok.ok, ok.problems
