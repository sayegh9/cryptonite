"""Tests for strict 1m-derived UTC aggregation and window completeness validation."""

import polars as pl
import pytest
from cryptonite_scanner.resampler import ONE_MINUTE_NS, resample_1m_bars


def test_resample_complete_5m_window():
    """Verify that a complete 5-minute window produces exactly one 5m bar with exact timestamps."""
    # Create 5 contiguous 1m bars aligned at UTC 12:00:00 (12:00, 12:01, 12:02, 12:03, 12:04)
    base_ts = 1_700_000_000 * 1_000_000_000  # aligned in seconds
    # Round base_ts to 5m boundary
    five_min_ns = 5 * ONE_MINUTE_NS
    window_open = (base_ts // five_min_ns) * five_min_ns

    open_times = [window_open + (i * ONE_MINUTE_NS) for i in range(5)]
    df_1m = pl.DataFrame({
        "bar_open_ns": open_times,
        "open": [100.0, 101.0, 102.0, 103.0, 104.0],
        "high": [105.0, 106.0, 107.0, 108.0, 109.0],
        "low": [99.0, 98.0, 97.0, 96.0, 95.0],
        "close": [101.0, 102.0, 103.0, 104.0, 105.0],
        "volume": [10.0, 20.0, 30.0, 40.0, 50.0],
    })

    res = resample_1m_bars(df_1m, "5m")
    assert len(res) == 1
    row = res.to_dicts()[0]

    assert row["bar_open_ns"] == window_open
    assert row["interval_end_exclusive_ns"] == window_open + five_min_ns
    assert row["bar_close_inclusive_ns"] == window_open + five_min_ns - 1_000_000
    assert row["open"] == 100.0  # first open
    assert row["high"] == 109.0  # max high
    assert row["low"] == 95.0    # min low
    assert row["close"] == 105.0 # last close
    assert row["volume"] == 150.0 # sum volume


def test_resample_drops_incomplete_window():
    """Verify that a window with only 4 bars (incomplete) is dropped."""
    base_ts = 1_700_000_000 * 1_000_000_000
    five_min_ns = 5 * ONE_MINUTE_NS
    window_open = (base_ts // five_min_ns) * five_min_ns

    # Only 4 bars present (missing the 5th)
    open_times = [window_open + (i * ONE_MINUTE_NS) for i in range(4)]
    df_1m = pl.DataFrame({
        "bar_open_ns": open_times,
        "open": [100.0, 101.0, 102.0, 103.0],
        "high": [105.0, 106.0, 107.0, 108.0],
        "low": [99.0, 98.0, 97.0, 96.0],
        "close": [101.0, 102.0, 103.0, 104.0],
        "volume": [10.0, 20.0, 30.0, 40.0],
    })

    res = resample_1m_bars(df_1m, "5m")
    assert len(res) == 0, "Incomplete window must be dropped"


def test_resample_drops_gapped_window():
    """Verify that a window with 5 bars but a 1-minute internal gap is dropped."""
    base_ts = 1_700_000_000 * 1_000_000_000
    five_min_ns = 5 * ONE_MINUTE_NS
    window_open = (base_ts // five_min_ns) * five_min_ns

    # Minutes 0, 1, 2, 4 (missing minute 3)
    open_times = [
        window_open,
        window_open + ONE_MINUTE_NS,
        window_open + 2 * ONE_MINUTE_NS,
        window_open + 4 * ONE_MINUTE_NS,
    ]
    df_1m = pl.DataFrame({
        "bar_open_ns": open_times,
        "open": [100.0, 101.0, 102.0, 104.0],
        "high": [105.0, 106.0, 107.0, 109.0],
        "low": [99.0, 98.0, 97.0, 95.0],
        "close": [101.0, 102.0, 103.0, 105.0],
        "volume": [10.0, 20.0, 30.0, 50.0],
    })

    res = resample_1m_bars(df_1m, "5m")
    assert len(res) == 0, "Gapped window must be dropped"
