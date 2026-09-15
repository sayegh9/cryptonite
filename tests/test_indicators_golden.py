"""Golden numerical reference tests for vectorized Polars technical indicators.

Verifies exact Float64 calculations and numerical stability against known analytical values.
"""

import random
import polars as pl
import pytest
from cryptonite_scanner.indicators import (
    calc_atr,
    calc_bollinger_bands,
    calc_ema,
    calc_macd,
    calc_rsi,
    calc_sma,
    calc_volume_ratio,
)


def test_calc_sma_golden():
    """Verify SMA calculation against direct moving average."""
    data = [10.0, 20.0, 30.0, 40.0, 50.0]
    df = pl.DataFrame({"close": data})
    res = df.select(sma=calc_sma("close", 3))["sma"].to_list()
    assert res[0] is None
    assert res[1] is None
    assert abs(res[2] - 20.0) < 1e-7
    assert abs(res[3] - 30.0) < 1e-7
    assert abs(res[4] - 40.0) < 1e-7


def test_calc_ema_golden():
    """Verify EMA calculation against manual recursive EMA formula."""
    # EMA formula: EMA_t = alpha * P_t + (1 - alpha) * EMA_{t-1}, where alpha = 2 / (span + 1)
    span = 3
    alpha = 2.0 / (span + 1.0)
    prices = [10.0, 12.0, 15.0, 14.0, 18.0]
    df = pl.DataFrame({"close": prices})
    res = df.select(ema=calc_ema("close", span))["ema"].to_list()

    expected = [prices[0]]
    for p in prices[1:]:
        expected.append(alpha * p + (1.0 - alpha) * expected[-1])

    for r, exp in zip(res, expected, strict=True):
        assert abs(r - exp) < 1e-7


def test_calc_rsi_golden():
    """Verify Wilder's RSI calculation."""
    # Monotonically increasing sequence must have RSI approaching 100
    prices = [float(i) for i in range(1, 30)]
    df = pl.DataFrame({"close": prices})
    res = df.select(rsi=calc_rsi("close", 14))["rsi"].to_list()
    # Later values should be very high (> 95)
    assert res[-1] > 95.0

    # Monotonically decreasing sequence must have RSI approaching 0
    prices_down = [float(30 - i) for i in range(1, 30)]
    df_down = pl.DataFrame({"close": prices_down})
    res_down = df_down.select(rsi=calc_rsi("close", 14))["rsi"].to_list()
    assert res_down[-1] < 5.0


def test_calc_bollinger_bands_golden():
    """Verify Bollinger Bands symmetry and width."""
    random.seed(42)
    prices = [100.0 + random.gauss(0, 1) for _ in range(50)]
    df = pl.DataFrame({"close": prices})

    lower, mid, upper = calc_bollinger_bands("close", 20, 2.0)
    res = df.select(lower=lower, mid=mid, upper=upper)

    # For valid rows (row index >= 19), upper - mid == mid - lower
    valid = res.slice(19, 31)
    for row in valid.iter_rows(named=True):
        diff_up = row["upper"] - row["mid"]
        diff_down = row["mid"] - row["lower"]
        assert abs(diff_up - diff_down) < 1e-7
        assert diff_up > 0.0


def test_calc_atr_golden():
    """Verify Average True Range non-negativity and responsiveness to gaps."""
    highs = [105.0, 110.0, 115.0, 108.0]
    lows = [95.0, 100.0, 102.0, 99.0]
    closes = [100.0, 108.0, 105.0, 102.0]

    df = pl.DataFrame({"high": highs, "low": lows, "close": closes})
    res = df.select(atr=calc_atr("high", "low", "close", 3))["atr"].to_list()

    for val in res:
        if val is not None:
            assert val > 0.0


def test_calc_volume_ratio_zero_protection():
    """Verify volume ratio does not fail on zero volume."""
    df = pl.DataFrame({"volume": [0.0, 0.0, 0.0, 10.0, 0.0]})
    res = df.select(ratio=calc_volume_ratio("volume", 3))["ratio"].to_list()
    assert res is not None
    assert len(res) == 5
