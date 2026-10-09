"""Tests for Phase 2.4 historical backfill coordinator and missing_runs.

Invariants tested:
1. missing_runs pure catalog interval logic and grid gap detection.
2. commit_history_batch allows writing pre-checkpoint history without skipping.
3. commit_history_batch creates 0 outbox events (neither closed_bar_committed nor Bar).
4. Covered bars in catalog intervals are never rewritten.
5. Live commit_batch behavior remains completely unchanged (guards checkpoint, creates outbox rows).
"""

import json
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_schemas.identifiers import to_nautilus_bar_type, to_spot_instrument_id
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.coordinator import TwoStoreCoordinator, missing_runs
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from cryptonite_storage.queries import AnalyticsQueryEngine

MIN_MS = 60_000
T0_MS = 1_791_104_400_000  # 2026-10-04 09:00:00 UTC


class FakeConn:
    def __init__(self, outbox: list[tuple[str, dict]], executed: list[tuple[str, tuple]]):
        self.outbox = outbox
        self.executed = executed

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        if "INSERT INTO outbox_events" in sql:
            self.outbox.append((args[1], json.loads(args[4])))
        return "OK"


class FakePool:
    def __init__(self, checkpoint_row: dict | None = None):
        self.outbox: list[tuple[str, dict]] = []
        self.executed: list[tuple[str, tuple]] = []
        self.checkpoint_row = checkpoint_row

    async def fetchrow(self, sql: str, *args):
        if "SELECT last_timestamp_ns FROM ingest_checkpoints" in sql:
            return self.checkpoint_row
        return None

    async def fetchval(self, sql: str, *args):
        return None

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self.outbox, self.executed)


def make_kline(open_ms: int, price: float = 100.0, tf_ms: int = MIN_MS) -> RawBinanceKline:
    return RawBinanceKline(
        open_time_ms=open_ms,
        close_time_ms=open_ms + tf_ms - 1,
        open=f"{price:.2f}",
        high=f"{price + 1.0:.2f}",
        low=f"{price - 1.0:.2f}",
        close=f"{price + 0.5:.2f}",
        volume="10.0",
        quote_volume="1000.0",
        trades_count=10,
        taker_buy_base_volume="5.0",
        taker_buy_quote_volume="500.0",
        is_closed=True,
    )


def make_bar(k: RawBinanceKline, symbol: str = "BTCUSDT", timeframe: str = "1m") -> EnrichedBar:
    inst = to_spot_instrument_id(symbol)
    bar_type = to_nautilus_bar_type(inst, timeframe)
    return EnrichedBar.from_binance_raw(
        raw=k,
        instrument_id=inst,
        bar_type=str(bar_type),
        price_precision=2,
        size_precision=5,
        received_at_ns=(k.close_time_ms + 1) * 1_000_000,
    )


# -----------------------------------------------------------------------------
# 1. missing_runs tests
# -----------------------------------------------------------------------------


def test_missing_runs_empty_catalog():
    step = 60 * 1_000_000_000
    start = 100 * step
    end = 105 * step
    runs = missing_runs([], start, end, step)
    assert len(runs) == 1
    assert runs[0] == [100 * step, 101 * step, 102 * step, 103 * step, 104 * step, 105 * step]


def test_missing_runs_fully_covered():
    step = 60 * 1_000_000_000
    start = 100 * step
    end = 105 * step
    intervals = [(100 * step, 105 * step)]
    runs = missing_runs(intervals, start, end, step)
    assert runs == []


def test_missing_runs_with_gaps():
    step = 60 * 1_000_000_000
    start = 100 * step
    end = 110 * step
    # Intervals cover [100, 102] and [106, 108]
    # Gaps are: [103, 104, 105] and [109, 110]
    intervals = [
        (100 * step, 102 * step),
        (106 * step, 108 * step),
    ]
    runs = missing_runs(intervals, start, end, step)
    assert len(runs) == 2
    assert runs[0] == [103 * step, 104 * step, 105 * step]
    assert runs[1] == [109 * step, 110 * step]


def test_missing_runs_single_point_files():
    step = 60 * 1_000_000_000
    start = 100 * step
    end = 104 * step
    # Single point files [t, t] (as written by single-bar writers)
    intervals = [
        (100 * step, 100 * step),
        (102 * step, 102 * step),
        (104 * step, 104 * step),
    ]
    runs = missing_runs(intervals, start, end, step)
    # Missing points: 101 and 103 (each in separate run because not adjacent)
    assert len(runs) == 2
    assert runs[0] == [101 * step]
    assert runs[1] == [103 * step]


def test_missing_runs_invalid_step():
    with pytest.raises(ValueError, match="step_ns must be positive"):
        missing_runs([], 100, 200, 0)
    with pytest.raises(ValueError, match="step_ns must be positive"):
        missing_runs([], 100, 200, -10)


# -----------------------------------------------------------------------------
# 2. commit_history_batch vs live commit_batch checkpoint boundary
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_history_batch_bypasses_checkpoint_guard(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair
):
    naut_mgr = NautilusCatalogManager(temp_nautilus_dir)
    naut_mgr.write_instruments([sample_currency_pair])
    lake_writer = AnalyticsLakeWriter(temp_analytics_dir)

    # Checkpoint is set far in the future
    future_checkpoint_ts = (T0_MS + 100 * MIN_MS) * 1_000_000
    pool = FakePool(checkpoint_row={"last_timestamp_ns": future_checkpoint_ts})

    coordinator = TwoStoreCoordinator(naut_mgr, lake_writer, pg_pool=pool)  # type: ignore[arg-type]
    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")

    # Bar timestamp is older than future_checkpoint_ts
    bar = make_bar(make_kline(T0_MS, 100.0))
    assert bar.ts_event_ns < future_checkpoint_ts

    # 1. Normal live commit_batch should skip because older than checkpoint
    live_batch_id = await coordinator.commit_batch(
        bars=[bar],
        bar_type=bar_type,
        venue="BINANCE",
        market_type="SPOT",
        symbol="BTCUSDT",
        timeframe="1m",
    )
    assert live_batch_id == ""

    # 2. commit_history_batch must bypass the checkpoint guard and succeed
    history_batch_id = await coordinator.commit_history_batch(
        bars=[bar],
        bar_type=bar_type,
        venue="BINANCE",
        market_type="SPOT",
        symbol="BTCUSDT",
        timeframe="1m",
    )
    assert history_batch_id != ""

    # Verify checkpoint update was executed with LEAST / GREATEST upsert
    checkpoint_queries = [sql for sql, _ in pool.executed if "INSERT INTO ingest_checkpoints" in sql]
    assert len(checkpoint_queries) == 1
    assert "LEAST(ingest_checkpoints.first_timestamp_ns, EXCLUDED.first_timestamp_ns)" in checkpoint_queries[0]


# -----------------------------------------------------------------------------
# 3. No outbox events emitted on commit_history_batch
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_history_batch_emits_no_outbox_events(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair
):
    naut_mgr = NautilusCatalogManager(temp_nautilus_dir)
    naut_mgr.write_instruments([sample_currency_pair])
    lake_writer = AnalyticsLakeWriter(temp_analytics_dir)

    pool = FakePool(checkpoint_row=None)
    coordinator = TwoStoreCoordinator(naut_mgr, lake_writer, pg_pool=pool)  # type: ignore[arg-type]
    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")

    bars = [make_bar(make_kline(T0_MS + i * MIN_MS, 100.0 + i)) for i in range(3)]

    # Commit historical batch
    batch_id = await coordinator.commit_history_batch(
        bars=bars,
        bar_type=bar_type,
        venue="BINANCE",
        market_type="SPOT",
        symbol="BTCUSDT",
        timeframe="1m",
    )
    assert batch_id != ""

    # Invariant: 0 outbox events created for historical batches
    assert len(pool.outbox) == 0
    assert not any("INSERT INTO outbox_events" in sql for sql, _ in pool.executed)

    # Invariant: Live commit_batch DOES create outbox rows
    live_bars = [make_bar(make_kline(T0_MS + 10 * MIN_MS, 110.0))]
    live_batch_id = await coordinator.commit_batch(
        bars=live_bars,
        bar_type=bar_type,
        venue="BINANCE",
        market_type="SPOT",
        symbol="BTCUSDT",
        timeframe="1m",
    )
    assert live_batch_id != ""
    assert len(pool.outbox) > 0
    streams = [stream for stream, _ in pool.outbox]
    assert "cryptonite:stream:closed_bar_committed" in streams
    assert "nautilus:stream:bus" in streams


# -----------------------------------------------------------------------------
# 4. Covered bars never rewritten
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_covered_bars_never_rewritten(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair
):
    naut_mgr = NautilusCatalogManager(temp_nautilus_dir)
    naut_mgr.write_instruments([sample_currency_pair])
    lake_writer = AnalyticsLakeWriter(temp_analytics_dir)
    query_engine = AnalyticsQueryEngine(temp_analytics_dir)

    coordinator = TwoStoreCoordinator(naut_mgr, lake_writer, pg_pool=None)
    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")

    bar1 = make_bar(make_kline(T0_MS, 100.0))
    bar2 = make_bar(make_kline(T0_MS + MIN_MS, 101.0))
    bar3 = make_bar(make_kline(T0_MS + 2 * MIN_MS, 102.0))

    # Commit initial bars (1 and 2)
    b1_id = await coordinator.commit_history_batch(
        bars=[bar1, bar2],
        bar_type=bar_type,
        venue="BINANCE",
        market_type="SPOT",
        symbol="BTCUSDT",
        timeframe="1m",
    )
    assert b1_id != ""

    assert len(naut_mgr.query_bars(identifiers=[str(bar_type)])) == 2
    assert query_engine.count_bars("BTCUSDT", "1m") == 2

    # Attempt to commit exact same bars again: all are covered, must return ""
    b2_id = await coordinator.commit_history_batch(
        bars=[bar1, bar2],
        bar_type=bar_type,
        venue="BINANCE",
        market_type="SPOT",
        symbol="BTCUSDT",
        timeframe="1m",
    )
    assert b2_id == ""
    # Store counts should NOT have changed
    assert len(naut_mgr.query_bars(identifiers=[str(bar_type)])) == 2
    assert query_engine.count_bars("BTCUSDT", "1m") == 2

    # Attempt to commit a batch with 1 covered bar and 1 new bar (bar2 and bar3)
    b3_id = await coordinator.commit_history_batch(
        bars=[bar2, bar3],
        bar_type=bar_type,
        venue="BINANCE",
        market_type="SPOT",
        symbol="BTCUSDT",
        timeframe="1m",
    )
    assert b3_id != ""

    # Now total bars should be 3 (bar3 was added, bar2 was filtered out)
    assert len(naut_mgr.query_bars(identifiers=[str(bar_type)])) == 3
    assert query_engine.count_bars("BTCUSDT", "1m") == 3


# -----------------------------------------------------------------------------
# 5. Live commit_batch behavior unchanged
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_commit_batch_behavior_unchanged(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair
):
    naut_mgr = NautilusCatalogManager(temp_nautilus_dir)
    naut_mgr.write_instruments([sample_currency_pair])
    lake_writer = AnalyticsLakeWriter(temp_analytics_dir)

    pool = FakePool(checkpoint_row=None)
    coordinator = TwoStoreCoordinator(naut_mgr, lake_writer, pg_pool=pool)  # type: ignore[arg-type]
    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")

    k = make_kline(T0_MS, 100.0)
    bar = make_bar(k)

    batch_id = await coordinator.commit_batch(
        bars=[bar],
        bar_type=bar_type,
        venue="BINANCE",
        market_type="SPOT",
        symbol="BTCUSDT",
        timeframe="1m",
    )
    assert batch_id != ""

    # Outbox has closed_bar_committed event with contract schema_version 2
    (event,) = [p for s, p in pool.outbox if s == "cryptonite:stream:closed_bar_committed"]
    assert event["schema_version"] == 2
    assert event["bar_open_ns"] == k.open_time_ms * 1_000_000
    assert event["interval_end_exclusive_ns"] == (k.open_time_ms + MIN_MS) * 1_000_000
    assert event["bar_count"] == 1
    assert Decimal(event["close"]) == Decimal("100.5")

    # Nautilus and Analytics lake have 1 bar
    assert len(naut_mgr.query_bars(identifiers=[str(bar_type)])) == 1
    query_engine = AnalyticsQueryEngine(temp_analytics_dir)
    assert query_engine.count_bars("BTCUSDT", "1m") == 1
