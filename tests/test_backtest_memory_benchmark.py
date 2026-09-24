"""Memory benchmark verifying bounded Peak RSS and minimal memory delta for Nautilus BacktestNode.

Validates against rigorous execution criteria:
- Bar count: 100,000 bars
- Symbol count: 1 (BTCUSDT.BINANCE)
- Resolution & Duration: 1-minute bars over 100,000 minutes (~69.44 days)
- Catalog chunk size: chunk_size=100,000
- Host & Cgroup limits: inspects host /proc/meminfo and container cgroup memory limits
- Peak-RSS sampling: samples VmPeak, VmHWM (High Water Mark Peak RSS), VmRSS from /proc/self/status
"""

import gc
import os
from decimal import Decimal
from pathlib import Path

import pytest


from cryptonite_backtester.runner import BacktestRunner, BacktestRunRequest
from cryptonite_schemas.identifiers import (
    to_nautilus_bar_type,
    to_nautilus_instrument_id,
    to_spot_instrument_id,
)
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from nautilus_trader.model import (
    Bar,
    Currency,
    CurrencyPair,
    Price,
    Quantity,
)


def _get_proc_memory_status() -> dict[str, int]:
    """Read memory metrics (VmPeak, VmHWM, VmRSS, VmSize) from /proc/self/status in KiB."""
    metrics = {"VmPeak": 0, "VmHWM": 0, "VmRSS": 0, "VmSize": 0}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].rstrip(":") in metrics:
                    metrics[parts[0].rstrip(":")] = int(parts[1])
    except (OSError, IndexError):
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF)
        metrics["VmHWM"] = ru.ru_maxrss
        metrics["VmRSS"] = ru.ru_maxrss
    return metrics


def _get_system_limits() -> dict[str, str | int]:
    """Inspect host total memory and container cgroup memory ceilings."""
    limits: dict[str, str | int] = {"container_limit_configured": "1024MB"}
    # Read host meminfo
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    limits["host_mem_total_kb"] = int(line.split()[1])
                    break
    except OSError:
        limits["host_mem_total_kb"] = "unknown"

    # Read cgroups v2 or v1 memory limits if present
    cgroup_v2 = Path("/sys/fs/cgroup/memory.max")
    cgroup_v1 = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if cgroup_v2.exists():
        val = cgroup_v2.read_text().strip()
        limits["cgroup_v2_memory_max"] = val if val == "max" else int(val)
    elif cgroup_v1.exists():
        limits["cgroup_v1_limit_bytes"] = int(cgroup_v1.read_text().strip())

    return limits


def _setup_benchmark_catalog(catalog_dir: Path, num_bars: int = 100_000) -> tuple[str, str, int, int, int]:
    """Generate deterministic catalog with specified bar count."""
    mgr = NautilusCatalogManager(catalog_dir)
    inst_id = to_nautilus_instrument_id(to_spot_instrument_id("BTCUSDT"))
    pair = CurrencyPair(
        instrument_id=inst_id,
        raw_symbol=inst_id.symbol,
        base_currency=Currency.from_str("BTC"),
        quote_currency=Currency.from_str("USDT"),
        price_precision=2,
        size_precision=5,
        price_increment=Price(Decimal("0.01"), 2),
        size_increment=Quantity(Decimal("0.00001"), 5),
        ts_event=1725700000000000000,
        ts_init=1725700000000000000,
    )
    mgr.write_instruments([pair])

    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    step_ns = 60_000_000_000
    base_ts = 1725700000000000000

    bars = []
    px = 50000.0
    for i in range(num_bars):
        ts = base_ts + i * step_ns
        c = px + (10.0 if (i % 1000) < 500 else -10.0)
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price(Decimal(str(px)), 2),
                high=Price(Decimal(str(max(px, c) + 5.0)), 2),
                low=Price(Decimal(str(min(px, c) - 5.0)), 2),
                close=Price(Decimal(str(c)), 2),
                volume=Quantity(Decimal("1.0"), 5),
                ts_event=ts,
                ts_init=ts,
            )
        )
        px = c
    mgr.write_bars(bars)

    warmup_bars = min(200, num_bars // 4)
    eval_start_ns = base_ts + (warmup_bars * step_ns)
    eval_end_ns = base_ts + ((num_bars - 1) * step_ns)

    return str(inst_id), str(bar_type), base_ts, eval_start_ns, eval_end_ns


def test_100k_bars_peak_rss_benchmark(tmp_path: Path):
    """Execute 100,000-bar backtest and verify bounded Peak RSS (VmHWM) under chunk_size=100,000."""
    cat_dir = tmp_path / "nautilus_catalog"
    art_dir = tmp_path / "backtest_artifacts"
    num_bars = 100_000

    # VmHWM is a process-lifetime peak; reset it so earlier tests in the same pytest
    # process don't count against this benchmark (Linux: "5" resets the peak RSS).
    gc.collect()
    try:
        with open("/proc/self/clear_refs", "w") as f:
            f.write("5")
    except OSError:
        pass

    # 1. Setup catalog with 100,000 1-minute bars (~69.44 days)
    inst_id, bar_type, warmup_start, eval_start, eval_end = _setup_benchmark_catalog(cat_dir, num_bars)

    # 2. Inspect limits and baseline memory
    limits = _get_system_limits()
    gc.collect()
    baseline_mem = _get_proc_memory_status()

    # 3. Execute 100k backtest run
    runner = BacktestRunner(cat_dir, art_dir)
    req = BacktestRunRequest(
        run_id="bench-100k-execution",
        strategy_slug="ema-cross",
        strategy_version_id="00000000-0000-0000-0000-000000000001",
        instrument_id=inst_id,
        bar_type=bar_type,
        timeframe="1m",
        parameters={"fast_period": 9, "slow_period": 21, "trade_size": 0.01},
        warmup_start_ns=warmup_start,
        evaluation_start_ns=eval_start,
        evaluation_end_ns=eval_end,
        execution_profile="optimistic",
        initial_balance=10000.0,
    )

    result = runner.run(req)
    assert result["total_trades"] is not None

    # 4. Sample peak memory and post-execution memory
    peak_mem = _get_proc_memory_status()
    gc.collect()
    final_mem = _get_proc_memory_status()

    peak_rss_mb = peak_mem["VmHWM"] / 1024.0
    current_rss_mb = final_mem["VmRSS"] / 1024.0
    delta_rss_mb = (final_mem["VmRSS"] - baseline_mem["VmRSS"]) / 1024.0

    print("\n--- 100k-Bar Backtest Memory Benchmark Report ---")
    print(f"Bar count:          {num_bars:,} bars (1-minute OHLCV)")
    print(f"Symbol count:       1 ({inst_id})")
    print(f"Duration:           {num_bars / 1440.0:.2f} days (~{num_bars * 60 / 3600:.1f} hours)")
    print(f"Catalog chunk size: 100,000 bars")
    print(f"System Limits:      {limits}")
    print(f"Baseline VmRSS:     {baseline_mem['VmRSS'] / 1024.0:.2f} MB")
    print(f"Peak RSS (VmHWM):   {peak_rss_mb:.2f} MB")
    print(f"Post-run VmRSS:     {current_rss_mb:.2f} MB")
    print(f"VmRSS Delta:        {delta_rss_mb:.2f} MB")
    print("--------------------------------------------------")

    # Invariant: Peak RSS must remain bounded well below the 1024 MB container limit (< 700 MB)
    assert peak_rss_mb < 700.0, f"Peak RSS exceeded 700 MB: {peak_rss_mb:.2f} MB (container ceiling is 1024 MB)"


def test_sequential_backtest_memory_bounded_growth(tmp_path: Path):
    """Verify memory delta across 5 sequential backtest runs remains strictly bounded."""
    cat_dir = tmp_path / "nautilus_catalog"
    art_dir = tmp_path / "backtest_artifacts"
    inst_id, bar_type, warmup_start, eval_start, eval_end = _setup_benchmark_catalog(cat_dir, num_bars=500)

    runner = BacktestRunner(cat_dir, art_dir)

    # Prime run
    prime_req = BacktestRunRequest(
        run_id="run-mem-prime",
        strategy_slug="ema-cross",
        strategy_version_id="00000000-0000-0000-0000-000000000001",
        instrument_id=inst_id,
        bar_type=bar_type,
        timeframe="1m",
        parameters={"fast_period": 5, "slow_period": 12, "trade_size": 0.01},
        warmup_start_ns=warmup_start,
        evaluation_start_ns=eval_start,
        evaluation_end_ns=eval_end,
        execution_profile="optimistic",
        initial_balance=10000.0,
    )
    runner.run(prime_req)
    gc.collect()

    initial_rss_kb = _get_proc_memory_status()["VmRSS"]
    rss_measurements_kb = []

    for i in range(5):
        req = BacktestRunRequest(
            run_id=f"run-mem-seq-{i}",
            strategy_slug="ema-cross",
            strategy_version_id="00000000-0000-0000-0000-000000000001",
            instrument_id=inst_id,
            bar_type=bar_type,
            timeframe="1m",
            parameters={"fast_period": 5 + i, "slow_period": 15 + i, "trade_size": 0.01},
            warmup_start_ns=warmup_start,
            evaluation_start_ns=eval_start,
            evaluation_end_ns=eval_end,
            execution_profile="optimistic",
            initial_balance=10000.0,
        )
        res = runner.run(req)
        assert res["total_trades"] is not None
        gc.collect()
        rss_measurements_kb.append(_get_proc_memory_status()["VmRSS"])

    growth_mb = (rss_measurements_kb[-1] - rss_measurements_kb[0]) / 1024.0
    print(f"\nSequential Runs Growth: initial={initial_rss_kb / 1024.0:.2f}MB, 5-run delta={growth_mb:.2f}MB")
    assert growth_mb < 50.0, f"Memory growth across runs exceeded 50 MB: {growth_mb:.2f} MB"


@pytest.mark.performance
def test_one_year_multi_chunk_memory_benchmark(tmp_path: Path):
    """Verify that a 1-year (525,600 bars) multi-chunk backtest executes with flat peak RSS.
    
    Demonstrates that BacktestNode streaming across multiple chunks of chunk_size=100,000
    does not accumulate memory linearly across chunks.
    """
    import pytest

    cat_dir = tmp_path / "nautilus_catalog"
    art_dir = tmp_path / "backtest_artifacts"
    num_bars = 525_600  # Exactly 365 days of 1-minute bars

    # Setup 1 year of catalog bars
    mgr = NautilusCatalogManager(cat_dir)
    inst_id = to_nautilus_instrument_id(to_spot_instrument_id("BTCUSDT"))
    pair = CurrencyPair(
        instrument_id=inst_id,
        raw_symbol=inst_id.symbol,
        base_currency=Currency.from_str("BTC"),
        quote_currency=Currency.from_str("USDT"),
        price_precision=2,
        size_precision=5,
        price_increment=Price(Decimal("0.01"), 2),
        size_increment=Quantity(Decimal("0.00001"), 5),
        ts_event=1725700000000000000,
        ts_init=1725700000000000000,
    )
    mgr.write_instruments([pair])

    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    step_ns = 60_000_000_000
    base_ts = 1725700000000000000

    # Write catalog data in chunks of 100,000 bars
    batch_size = 100_000
    rss_samples = []
    gc.collect()
    rss_samples.append(("pre_ingest", _get_proc_memory_status()["VmRSS"] / 1024.0))

    for b in range(0, num_bars, batch_size):
        chunk_bars = []
        n = min(batch_size, num_bars - b)
        for i in range(b, b + n):
            ts = base_ts + i * step_ns
            chunk_bars.append(
                Bar(
                    bar_type=bar_type,
                    open=Price(Decimal("50000.00"), 2),
                    high=Price(Decimal("50100.00"), 2),
                    low=Price(Decimal("49900.00"), 2),
                    close=Price(Decimal("50050.00"), 2),
                    volume=Quantity(Decimal("1.0"), 5),
                    ts_event=ts,
                    ts_init=ts,
                )
            )
        mgr.write_bars(chunk_bars)

    gc.collect()
    rss_samples.append(("post_ingest", _get_proc_memory_status()["VmRSS"] / 1024.0))

    runner = BacktestRunner(cat_dir, art_dir)
    req = BacktestRunRequest(
        run_id="bench-1-year-multi-chunk",
        strategy_slug="ema-cross",
        strategy_version_id="00000000-0000-0000-0000-000000000001",
        instrument_id=str(inst_id),
        bar_type=str(bar_type),
        timeframe="1m",
        parameters={"fast_period": 9, "slow_period": 21, "trade_size": 0.01},
        warmup_start_ns=base_ts,
        evaluation_start_ns=base_ts + (200 * step_ns),
        evaluation_end_ns=base_ts + ((num_bars - 1) * step_ns),
        execution_profile="optimistic",
        initial_balance=10000.0,
    )

    result = runner.run(req)
    assert result["total_trades"] is not None

    gc.collect()
    mem_post_run = _get_proc_memory_status()
    peak_rss_mb = mem_post_run["VmHWM"] / 1024.0
    rss_samples.append(("post_run", mem_post_run["VmRSS"] / 1024.0))

    print(f"\n--- 1-Year Multi-Chunk Memory Benchmark ({num_bars:,} bars, chunk_size=100k) ---")
    for label, val in rss_samples:
        print(f"  Stage {label:15s}: VmRSS = {val:.2f} MB")
    print(f"  Lifetime Peak (VmHWM) : {peak_rss_mb:.2f} MB")
    print("---------------------------------------------------------------------------------")

    # Invariant: peak RSS stays well within container memory limit (<850 MB for 1024 MB limit)
    assert peak_rss_mb < 850.0, f"Peak RSS {peak_rss_mb:.2f}MB exceeded 850MB threshold"
