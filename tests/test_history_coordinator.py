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
from cryptonite_storage.coordinator import TwoStoreCoordinator, missing_runs, split_contiguous_runs
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
    def __init__(self, checkpoint_row: dict | None = None, pending_rows: list[dict] | None = None):
        self.outbox: list[tuple[str, dict]] = []
        self.executed: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.checkpoint_row = checkpoint_row
        self.pending_rows = pending_rows or []

    async def fetch(self, sql: str, *args):
        return list(self.pending_rows)

    async def fetchrow(self, sql: str, *args):
        if "SELECT last_timestamp_ns FROM ingest_checkpoints" in sql:
            return self.checkpoint_row
        return None

    async def fetchval(self, sql: str, *args):
        self.fetchval_calls.append((sql, args))
        return None

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        return "OK"

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


# -----------------------------------------------------------------------------
# 6. Hartening regression tests (independent review of PR #53)
#    - F1: catalog spans can never mask interior missing bars (contiguity + split)
#    - F4: fail-closed coverage filter
#    - F6: 1m/15m/1h/4h support
#    - F2: pooled pre-checkpoint history reconciliation restart-safe
# -----------------------------------------------------------------------------


def test_missing_runs_mid_grid_start_and_end():
    step = 60 * 1_000_000_000
    # start/end intentionally half a step off the grid
    start = 100 * step + step // 2
    end = 105 * step + step // 2
    intervals = [(101 * step, 103 * step)]
    runs = missing_runs(intervals, start, end, step)
    # Grid scan starts at 101*step; 101/102/103 covered; 104/105 missing in one run
    assert runs == [[104 * step, 105 * step]]

    # Interval bounds that fall mid-grid are clamped to the grid as well
    intervals2 = [(101 * step + step // 2, 103 * step + step // 2)]
    runs2 = missing_runs(intervals2, 100 * step, 104 * step, step)
    assert runs2 == [[100 * step, 101 * step], [104 * step]]


def test_split_contiguous_runs_splits_on_interior_gaps():
    step = 60 * 1_000_000_000
    bars = [
        make_bar(make_kline(T0_MS)),
        make_bar(make_kline(T0_MS + MIN_MS)),
        make_bar(make_kline(T0_MS + 3 * MIN_MS)),  # gap: T0 + 2*MIN_MS missing
        make_bar(make_kline(T0_MS + 4 * MIN_MS)),
    ]
    runs = split_contiguous_runs(bars, step)
    assert len(runs) == 2
    assert [b.ts_event_ns // 1_000_000 for b in runs[0]] == [T0_MS + MIN_MS, T0_MS + 2 * MIN_MS]
    assert [b.ts_event_ns // 1_000_000 for b in runs[1]] == [T0_MS + 4 * MIN_MS, T0_MS + 5 * MIN_MS]
    assert split_contiguous_runs([], step) == []


@pytest.mark.asyncio
async def test_commit_history_batch_gappy_input_never_masks_interior_gap(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair
):
    """F1 regression: writing [t1, t3] with t2 missing must not create a span hiding t2."""
    naut_mgr = NautilusCatalogManager(temp_nautilus_dir)
    naut_mgr.write_instruments([sample_currency_pair])
    lake_writer = AnalyticsLakeWriter(temp_analytics_dir)
    query_engine = AnalyticsQueryEngine(temp_analytics_dir)

    coordinator = TwoStoreCoordinator(naut_mgr, lake_writer, pg_pool=None)
    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    step_ns = 60 * 1_000_000_000

    # Bars are stamped at their CLOSE: a bar opening at 09:00 closes at 09:01 (C1); one opening at
    # 09:02 closes at 09:03 (C3). Close stamp C2 = 09:02 (i.e. T0_MS + MIN_MS) is intentionally absent.
    bars = [make_bar(make_kline(T0_MS, 101.0)), make_bar(make_kline(T0_MS + 2 * MIN_MS, 103.0))]

    batch_id = await coordinator.commit_history_batch(
        bars=bars,
        bar_type=bar_type,
        venue="BINANCE",
        market_type="SPOT",
        symbol="BTCUSDT",
        timeframe="1m",
    )
    assert batch_id != ""
    assert len(naut_mgr.query_bars(identifiers=[str(bar_type)])) == 2
    assert query_engine.count_bars("BTCUSDT", "1m") == 2

    # The absent C2 must remain visible: no catalog interval contains that close stamp.
    t_missing_close = (T0_MS + 2 * MIN_MS) * 1_000_000  # the absent 09:02 close stamp
    intervals = naut_mgr.get_intervals("bars", str(bar_type))
    assert not any(a <= t_missing_close <= c for a, c in intervals)

    # And missing_runs over close-stamps k=0..3 reports k=0 and k=2 missing (bars close at k=1 and k=3).
    missing = [x for run in missing_runs(intervals, T0_MS * 1_000_000, (T0_MS + 3 * MIN_MS) * 1_000_000, step_ns)
               for x in run]
    missing_ks = sorted({x // 1_000_000 // MIN_MS - T0_MS // MIN_MS for x in missing})
    assert missing_ks == [0, 2], f"expected k=0 and k=2 missing, got {missing_ks} (points {missing})"
    assert (T0_MS + MIN_MS) * 1_000_000 not in missing  # k=1 covered
    assert (T0_MS + 3 * MIN_MS) * 1_000_000 not in missing  # k=3 covered


@pytest.mark.asyncio
async def test_commit_history_batch_rejects_off_grid_and_sub_cadence(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair
):
    """F1 enforcement: fail closed rather than write a file that could span a genuine gap."""
    naut_mgr = NautilusCatalogManager(temp_nautilus_dir)
    naut_mgr.write_instruments([sample_currency_pair])
    lake_writer = AnalyticsLakeWriter(temp_analytics_dir)
    coordinator = TwoStoreCoordinator(naut_mgr, lake_writer, pg_pool=None)
    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")

    # 1. Duplicate / sub-cadence (two bars closing at the same 1m stamp)
    bad = [make_bar(make_kline(T0_MS)), make_bar(make_kline(T0_MS))]
    with pytest.raises(ValueError, match="sub-cadence"):
        await coordinator.commit_history_batch(
            bars=bad, bar_type=bar_type, venue="BINANCE", market_type="SPOT",
            symbol="BTCUSDT", timeframe="1m",
        )

    # 2. Off-grid close stamp for the timeframe
    off_grid = make_bar(make_kline(T0_MS + MIN_MS, tf_ms=15 * MIN_MS), timeframe="15m")  # 09:16 close
    with pytest.raises(ValueError, match="off the"):
        await coordinator.commit_history_batch(
            bars=[off_grid], bar_type=to_nautilus_bar_type("BTCUSDT.BINANCE", "15m"),
            venue="BINANCE", market_type="SPOT", symbol="BTCUSDT", timeframe="15m",
        )

    # Nothing was written by the rejected attempts
    assert len(naut_mgr.query_bars(identifiers=[str(bar_type)])) == 0


@pytest.mark.asyncio
async def test_commit_history_batch_fails_closed_when_coverage_unreadable(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair
):
    """F4 regression: a coverage-query failure must NOT fall back to writing unfiltered."""
    naut_mgr = NautilusCatalogManager(temp_nautilus_dir)
    naut_mgr.write_instruments([sample_currency_pair])
    lake_writer = AnalyticsLakeWriter(temp_analytics_dir)
    coordinator = TwoStoreCoordinator(naut_mgr, lake_writer, pg_pool=None)
    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")

    def boom(*args, **kwargs):
        raise RuntimeError("catalog read failed")

    naut_mgr.get_intervals = boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="refusing to write unfiltered"):
        await coordinator.commit_history_batch(
            bars=[make_bar(make_kline(T0_MS))],
            bar_type=bar_type,
            venue="BINANCE",
            market_type="SPOT",
            symbol="BTCUSDT",
            timeframe="1m",
        )
    assert len(naut_mgr.query_bars(identifiers=[str(bar_type)])) == 0


@pytest.mark.asyncio
async def test_commit_history_batch_supports_four_timeframes(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair
):
    """F6 regression: 1m/15m/1h/4h history commits write exact contiguous spans."""
    naut_mgr = NautilusCatalogManager(temp_nautilus_dir)
    naut_mgr.write_instruments([sample_currency_pair])
    lake_writer = AnalyticsLakeWriter(temp_analytics_dir)
    coordinator = TwoStoreCoordinator(naut_mgr, lake_writer, pg_pool=None)

    t4h = 1_791_100_800_000  # 2026-10-04 08:00 UTC (on the 4h grid)
    cases = [
        ("1m", MIN_MS, T0_MS),
        ("15m", 15 * MIN_MS, T0_MS),
        ("1h", 60 * MIN_MS, T0_MS),
        ("4h", 240 * MIN_MS, t4h),
    ]
    for tf, step_ms, base in cases:
        bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", tf)
        bars = [
            make_bar(make_kline(base + i * step_ms, 100.0 + i, tf_ms=step_ms), timeframe=tf)
            for i in range(3)
        ]
        b_id = await coordinator.commit_history_batch(
            bars=bars,
            bar_type=bar_type,
            venue="BINANCE",
            market_type="SPOT",
            symbol="BTCUSDT",
            timeframe=tf,
        )
        assert b_id != ""
        assert len(naut_mgr.query_bars(identifiers=[str(bar_type)])) == 3, tf

        intervals = naut_mgr.get_intervals("bars", str(bar_type))
        assert intervals, f"{tf} produced no catalog intervals"
        # Bars opened at base + i*step -> closed at base + (i+1)*step for i in 0..2; the contiguity
        # contract means the file span exactly equals the written closes, and k=4 stays missing.
        missing = [x for run in missing_runs(intervals, base * 1_000_000, (base + 4 * step_ms) * 1_000_000,
                                             step_ms * 1_000_000) for x in run]
        missing_ks = sorted({x // 1_000_000 // step_ms - base // step_ms for x in missing})
        assert missing_ks == [0, 4], f"{tf}: expected missing closes k=0 and k=4, got {missing_ks}"
        assert all((base + (i + 1) * step_ms) * 1_000_000 not in missing for i in range(3)), tf


class _SettlingCoordinator(TwoStoreCoordinator):
    """Coordinator whose replay writes nothing (simulates an all-covered history re-run)."""

    async def _execute_commit(self, **kwargs):  # type: ignore[override]
        return ""


@pytest.mark.asyncio
async def test_reconcile_history_batch_replays_as_history_with_origin_and_timeframe(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair
):
    """F2: a pooled pre-checkpoint history batch reconciles through the history path with its own timeframe."""
    naut_mgr = NautilusCatalogManager(temp_nautilus_dir)
    naut_mgr.write_instruments([sample_currency_pair])
    lake_writer = AnalyticsLakeWriter(temp_analytics_dir)

    future_checkpoint_ts = (T0_MS + 100 * MIN_MS) * 1_000_000
    bar15 = make_bar(make_kline(T0_MS, 100.0, tf_ms=15 * MIN_MS), timeframe="15m")  # pre-checkpoint
    row = {
        "batch_id": "7f6a29e0-0000-4000-8000-000000000001",
        "venue": "BINANCE",
        "market_type": "SPOT",
        "symbol": "BTCUSDT",
        "bar_type": bar15.bar_type,
        "timeframe": "15m",
        "origin": "history",
        "canonical_payload_json": [bar15.model_dump(mode="json")],
    }
    pool = FakePool(checkpoint_row={"last_timestamp_ns": future_checkpoint_ts}, pending_rows=[row])
    coordinator = TwoStoreCoordinator(naut_mgr, lake_writer, pg_pool=pool)  # type: ignore[arg-type]

    # Caller iterates with a 1m bar_type; the row must be replayed with ITS OWN 15m bar type/timeframe.
    caller_bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    reconciled = await coordinator.reconcile_pending_batches(caller_bar_type)
    assert reconciled == 1

    # Checkpoint upsert ran with the row's real bar_type and timeframe
    ckpt_args = [args for sql, args in pool.executed if "INSERT INTO ingest_checkpoints" in sql]
    assert len(ckpt_args) == 1
    assert ckpt_args[0][3] == bar15.bar_type
    assert ckpt_args[0][4] == "15m"

    # History replay -> zero outbox events
    assert len(pool.outbox) == 0
    assert not any("INSERT INTO outbox_events" in sql for sql, _ in pool.executed)

    # ingest_batches insert carried origin='history' and timeframe='15m'
    inserts = [args for sql, args in pool.fetchval_calls if "INSERT INTO ingest_batches" in sql]
    assert inserts
    assert inserts[0][-2:] == ("15m", "history")


@pytest.mark.asyncio
async def test_reconcile_history_batch_settles_fully_covered(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair
):
    """F2: an all-covered history replay returns '' and the pending row is settled, not stuck forever."""
    naut_mgr = NautilusCatalogManager(temp_nautilus_dir)
    naut_mgr.write_instruments([sample_currency_pair])
    lake_writer = AnalyticsLakeWriter(temp_analytics_dir)

    bar = make_bar(make_kline(T0_MS, 100.0))
    row = {
        "batch_id": "7f6a29e0-0000-4000-8000-000000000002",
        "venue": "BINANCE",
        "market_type": "SPOT",
        "symbol": "BTCUSDT",
        "bar_type": bar.bar_type,
        "timeframe": "1m",
        "origin": "history",
        "canonical_payload_json": [bar.model_dump(mode="json")],
    }
    pool = FakePool(pending_rows=[row])
    coordinator = _SettlingCoordinator(naut_mgr, lake_writer, pg_pool=pool)  # type: ignore[arg-type]

    reconciled = await coordinator.reconcile_pending_batches(to_nautilus_bar_type("BTCUSDT.BINANCE", "1m"))
    assert reconciled == 1

    settles = [args for sql, args in pool.executed if "SET status = 'committed'" in sql]
    assert len(settles) == 1
    assert settles[0][0] == row["batch_id"]


@pytest.mark.asyncio
async def test_reconcile_live_batch_preserves_live_guard(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair
):
    """Live pending rows above nothing: the checkpoint guard still skips stale live replays (unchanged)."""
    naut_mgr = NautilusCatalogManager(temp_nautilus_dir)
    naut_mgr.write_instruments([sample_currency_pair])
    lake_writer = AnalyticsLakeWriter(temp_analytics_dir)

    future_checkpoint_ts = (T0_MS + 100 * MIN_MS) * 1_000_000
    bar = make_bar(make_kline(T0_MS, 100.0))  # pre-checkpoint
    row = {
        "batch_id": "7f6a29e0-0000-4000-8000-000000000003",
        "venue": "BINANCE",
        "market_type": "SPOT",
        "symbol": "BTCUSDT",
        "bar_type": bar.bar_type,
        "timeframe": "1m",
        "origin": "live",
        "canonical_payload_json": [bar.model_dump(mode="json")],
    }
    pool = FakePool(checkpoint_row={"last_timestamp_ns": future_checkpoint_ts}, pending_rows=[row])
    coordinator = TwoStoreCoordinator(naut_mgr, lake_writer, pg_pool=pool)  # type: ignore[arg-type]

    reconciled = await coordinator.reconcile_pending_batches(to_nautilus_bar_type("BTCUSDT.BINANCE", "1m"))
    assert reconciled == 1
    # Guard skipped the replay entirely: no checkpoint upsert, no outbox, no settlement of the live row
    assert not any("INSERT INTO ingest_checkpoints" in sql for sql, _ in pool.executed)
    assert len(pool.outbox) == 0
    assert not any("SET status = 'committed'" in sql for sql, _ in pool.executed)
