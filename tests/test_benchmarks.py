"""Latency Benchmarks for DuckDB queries and Polars Scanner Engine.

Verifies:
1. DuckDB analytical query over 43,200 bars (30 days of 1m bars) has p95 latency < 50ms.
2. Polars vectorized scanner rule evaluation over 43,200 bars has p95 latency < 250ms.
"""

import math
import statistics
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.models import (
    RiskConfig,
    RuleCondition,
    RuleGroup,
    RuleOperator,
)
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.queries import AnalyticsQueryEngine


@pytest.fixture(scope="module")
def benchmark_lake(tmp_path_factory):
    """Creates a 43,200-bar Analytics Lake fixture (30 days x 1,440 1m bars)."""
    tmp_dir = tmp_path_factory.mktemp("bench_lake")
    writer = AnalyticsLakeWriter(tmp_dir)

    base_time = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    bars_per_day = 1440
    days = 30
    total_bars = bars_per_day * days  # 43,200 bars

    base_price = 50000.0
    current_time = base_time

    for day in range(days):
        day_bars = []
        for i in range(bars_per_day):
            bar_idx = day * bars_per_day + i
            # Synthetic random walk
            drift = math.sin(bar_idx / 500.0) * 100.0
            noise = (bar_idx % 17 - 8) * 5.0
            close_p = base_price + drift + noise
            open_p = close_p - (bar_idx % 7 - 3) * 3.0
            high_p = max(open_p, close_p) + 10.0
            low_p = min(open_p, close_p) - 10.0
            vol = 15.0 + (bar_idx % 23)

            open_ms = int(current_time.timestamp() * 1000)
            next_time = current_time + timedelta(minutes=1)
            close_ms = int(next_time.timestamp() * 1000) - 1

            raw = RawBinanceKline(
                open_time_ms=open_ms,
                open=f"{open_p:.2f}",
                high=f"{high_p:.2f}",
                low=f"{low_p:.2f}",
                close=f"{close_p:.2f}",
                volume=f"{vol:.4f}",
                close_time_ms=close_ms,
                quote_volume=f"{vol * close_p:.2f}",
                trades_count=120,
                taker_buy_base_volume=f"{vol * 0.5:.4f}",
                taker_buy_quote_volume=f"{vol * close_p * 0.5:.2f}",
                is_closed=True,
            )
            bar = EnrichedBar.from_binance_raw(
                raw=raw,
                instrument_id="BTCUSDT.BINANCE",
                bar_type="BTCUSDT.BINANCE-1-MINUTE-LAST-INTERNAL",
                price_precision=2,
                size_precision=5,
                received_at_ns=close_ms * 1_000_000,
            )
            day_bars.append(bar)
            current_time = next_time

        writer.write_batch(day_bars, symbol="BTCUSDT", timeframe="1m")

    engine = AnalyticsQueryEngine(tmp_dir)
    assert engine.count_bars("BTCUSDT", "1m") == total_bars
    return engine


def test_duckdb_query_p95_latency(benchmark_lake: AnalyticsQueryEngine):
    """Verify DuckDB analytical bar query p95 latency is < 50ms."""
    engine = benchmark_lake
    min_ts, max_ts = engine.get_timestamp_range("BTCUSDT", "1m")
    assert min_ts is not None and max_ts is not None

    # Warm-up DuckDB connection and query planner
    _ = engine.query_bars_df("BTCUSDT", "1m", start_ns=min_ts, end_ns=min_ts + 2000 * 60 * 1_000_000_000)

    latencies_ms = []
    iterations = 25
    # Query realistic terminal slice of 2000 bars
    slice_ns = 2000 * 60 * 1_000_000_000

    for i in range(iterations):
        start_ns = min_ts + (i * 300 * 60 * 1_000_000_000)
        end_ns = start_ns + slice_ns

        t0 = time.perf_counter()
        df = engine.query_bars_df("BTCUSDT", "1m", start_ns=start_ns, end_ns=end_ns)
        t1 = time.perf_counter()

        assert not df.is_empty()
        latencies_ms.append((t1 - t0) * 1000.0)

    latencies_ms.sort()
    p50 = statistics.median(latencies_ms)
    p95_idx = int(math.ceil(0.95 * len(latencies_ms))) - 1
    p95 = latencies_ms[p95_idx]
    print(f"\n[DuckDB 2000-bar Query] p50: {p50:.2f}ms, p95: {p95:.2f}ms, max: {max(latencies_ms):.2f}ms")
    assert p95 < 50.0, f"DuckDB query p95 was {p95:.2f}ms, expected < 50ms"


def test_polars_scanner_engine_p95_latency(benchmark_lake: AnalyticsQueryEngine):
    """Verify Polars Scanner Engine evaluating 43,200 bars has p95 latency < 250ms."""
    engine = benchmark_lake
    # Load all 43,200 bars into Polars DataFrame
    df = engine.query_bars_df("BTCUSDT", "1m")
    assert len(df) == 43_200

    rules = [
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(field="rsi", operator=RuleOperator.LT, value=30.0),
                RuleCondition(field="close", operator=RuleOperator.LT, value="bb_lower"),
            ],
        ),
        RuleGroup(
            category="liquidity",
            logic="AND",
            conditions=[
                RuleCondition(field="volume_ratio", operator=RuleOperator.GT, value=1.2),
            ],
        ),
    ]
    risk = RiskConfig(stop_loss_atr_mult=2.0, take_profit_atr_mult=4.0)

    # Warm-up
    _ = ScannerEngine.evaluate(
        df=df,
        rule_groups=rules,
        risk_config=risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="1m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )

    latencies_ms = []
    iterations = 15

    for _ in range(iterations):
        spec_vid = uuid4()
        run_id = uuid4()

        t0 = time.perf_counter()
        candidates = ScannerEngine.evaluate(
            df=df,
            rule_groups=rules,
            risk_config=risk,
            instrument_id="BTCUSDT.BINANCE",
            timeframe="1m",
            spec_version_id=spec_vid,
            scan_run_id=run_id,
        )
        t1 = time.perf_counter()

        latencies_ms.append((t1 - t0) * 1000.0)

    latencies_ms.sort()
    p50 = statistics.median(latencies_ms)
    p95_idx = int(math.ceil(0.95 * len(latencies_ms))) - 1
    p95 = latencies_ms[p95_idx]
    print(f"\n[Polars Scanner 43,200 bars] p50: {p50:.2f}ms, p95: {p95:.2f}ms, max: {max(latencies_ms):.2f}ms")
    assert p95 < 250.0, f"Polars scanner p95 was {p95:.2f}ms, expected < 250ms"
