"""Golden numerical reference tests for vectorized Polars technical indicators.

RSI and ATR follow Wilder / TA-Lib: null for the first ``period`` rows, seeded at row ``period`` with
the simple mean of the first ``period`` values (gains/losses or true ranges of rows 1..period), then
Wilder smoothing. Bollinger bands use the population standard deviation (ddof=0). Expected values come
from straightforward loop implementations below, written independently of the Polars expressions, and
from TA-Lib output recorded as fixtures.
"""

import math
import random
from pathlib import Path

import polars as pl
import pytest
from cryptonite_scanner import indicators
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.indicators import (
    calc_atr,
    calc_bollinger_bands,
    calc_ema,
    calc_macd,
    calc_rsi,
    calc_sma,
    calc_volume_ratio,
)
from cryptonite_scanner.resampler import resample_1m_bars
from cryptonite_storage.queries import AnalyticsQueryEngine

# --- independent reference implementations (plain Python loops) ---------------------------------

def ref_rsi(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_g, avg_l = sum(gains[:period]) / period, sum(losses[:period]) / period

    def rsi(g, ll):
        return 100.0 if ll == 0 else 100.0 - 100.0 / (1.0 + g / ll)

    out[period] = rsi(avg_g, avg_l)
    for i in range(period + 1, len(closes)):
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        out[i] = rsi(avg_g, avg_l)
    return out


def ref_atr(highs, lows, closes, period: int) -> list[float | None]:
    tr = [None] + [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                   for i in range(1, len(closes))]
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    atr = sum(tr[1:period + 1]) / period
    out[period] = atr
    for i in range(period + 1, len(closes)):
        atr = (atr * (period - 1) + tr[i]) / period
        out[i] = atr
    return out


def ref_bollinger(closes, period: int, k: float):
    out = []
    for i in range(len(closes)):
        if i < period - 1:
            out.append((None, None, None))
            continue
        w = closes[i - period + 1:i + 1]
        mid = sum(w) / period
        sd = math.sqrt(sum((x - mid) ** 2 for x in w) / period)  # population std
        out.append((mid - k * sd, mid, mid + k * sd))
    return out


def _series(n=120, seed=7):
    rng = random.Random(seed)
    closes, highs, lows = [], [], []
    price = 100.0
    for _ in range(n):
        price *= 1 + rng.gauss(0, 0.01)
        hi, lo = price * (1 + abs(rng.gauss(0, 0.004))), price * (1 - abs(rng.gauss(0, 0.004)))
        closes.append(price)
        highs.append(hi)
        lows.append(lo)
    return closes, highs, lows


def _close(a, b, tol=1e-9):
    assert (a is None) == (b is None), (a, b)
    if a is not None:
        assert a == pytest.approx(b, rel=tol, abs=tol)


# --- tests ---------------------------------------------------------------------------------------

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


# Wilder's worked RSI example closes (as reproduced by StockCharts). Expected values are TA-Lib 0.6
# ``talib.RSI(closes, 14)`` output, generated once outside the suite (TA-Lib is not a dependency).
# StockCharts' printed table (70.53, ...) rounds intermediate averages, hence its small difference.
WILDER_CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35,
                 44.03, 44.18, 44.22, 44.57, 43.42, 42.66, 43.13]
TALIB_RSI = [70.4641, 66.2496, 66.4809, 69.3469, 66.2947, 57.915, 62.8807, 63.2088, 56.0116, 62.3399, 54.671,
             50.3868, 40.0194, 41.4926, 41.9024, 45.4995, 37.3228, 33.0905, 37.7888]
# TA-Lib on ``_series()`` (seed 7): RSI(14) and ATR(14) at rows 14, 15, 50, 119; BBANDS(20, 2, 2)
# upper/lower at rows 19 and 119
TALIB_SERIES_ROWS = [14, 15, 50, 119]
TALIB_SERIES_RSI = [57.26996989793373, 62.78440695155846, 44.451526302914544, 50.69748838185174]
TALIB_SERIES_ATR = [0.9700140167534427, 1.0540641321998627, 1.0069075443387385, 1.402499212068195]
TALIB_SERIES_BB = {19: (103.38083459906368, 97.71887604990327), 119: (104.30695920464744, 100.00971287660282)}


def test_calc_rsi_matches_talib_on_wilders_example():
    res = pl.DataFrame({"close": WILDER_CLOSES}).select(rsi=calc_rsi("close", 14))["rsi"].to_list()
    assert res[:14] == [None] * 14  # SMA-seeded: nothing before row `period`
    assert res[14:] == pytest.approx(TALIB_RSI, abs=1e-4)


def test_scanner_indicators_match_talib():
    closes, highs, lows = _series()
    df = pl.DataFrame({"high": highs, "low": lows, "close": closes})
    lower, _, upper = calc_bollinger_bands("close", 20, 2.0)
    res = df.select(rsi=calc_rsi("close", 14), atr=calc_atr("high", "low", "close", 14), lo=lower, up=upper)
    assert [res["rsi"][i] for i in TALIB_SERIES_ROWS] == pytest.approx(TALIB_SERIES_RSI, rel=1e-9)
    assert [res["atr"][i] for i in TALIB_SERIES_ROWS] == pytest.approx(TALIB_SERIES_ATR, rel=1e-9)
    for i, (up, lo) in TALIB_SERIES_BB.items():
        assert (res["up"][i], res["lo"][i]) == pytest.approx((up, lo), rel=1e-9)


def test_calc_rsi_golden():
    closes, _, _ = _series()
    res = pl.DataFrame({"close": closes}).select(rsi=calc_rsi("close", 14))["rsi"].to_list()
    for got, exp in zip(res, ref_rsi(closes, 14), strict=True):
        _close(got, exp, 1e-9)


def test_calc_rsi_extremes():
    """A monotonic rise reads ~100, a monotonic fall ~0."""
    up = pl.DataFrame({"close": [float(i) for i in range(1, 30)]}).select(rsi=calc_rsi("close", 14))["rsi"]
    down = pl.DataFrame({"close": [float(30 - i) for i in range(1, 30)]}).select(rsi=calc_rsi("close", 14))["rsi"]
    assert up[-1] > 99.0 and down[-1] < 1.0


def test_calc_bollinger_bands_golden():
    """Population standard deviation (ddof=0), as TA-Lib and charting libraries use."""
    random.seed(42)
    prices = [100.0 + random.gauss(0, 1) for _ in range(50)]
    lower, mid, upper = calc_bollinger_bands("close", 20, 2.0)
    res = pl.DataFrame({"close": prices}).select(lower=lower, mid=mid, upper=upper)
    for row, (lo, m, up) in zip(res.iter_rows(named=True), ref_bollinger(prices, 20, 2.0), strict=True):
        _close(row["lower"], lo)
        _close(row["mid"], m)
        _close(row["upper"], up)


def test_calc_bollinger_bands_small_exact():
    """[1..5], period 5: mean 3, population std sqrt(2) (the sample std would be sqrt(2.5))."""
    lower, mid, upper = calc_bollinger_bands("close", 5, 2.0)
    row = pl.DataFrame({"close": [1.0, 2.0, 3.0, 4.0, 5.0]}).select(lower=lower, mid=mid, upper=upper).row(4, named=True)
    assert (row["lower"], row["mid"], row["upper"]) == pytest.approx((3 - 2 * math.sqrt(2), 3.0, 3 + 2 * math.sqrt(2)))


def test_calc_atr_golden():
    closes, highs, lows = _series()
    res = pl.DataFrame({"high": highs, "low": lows, "close": closes}).select(
        atr=calc_atr("high", "low", "close", 14))["atr"].to_list()
    for got, exp in zip(res, ref_atr(highs, lows, closes, 14), strict=True):
        _close(got, exp, 1e-9)


def test_calc_atr_small_exact():
    """Period 3: TR of rows 1..3 = 10, 13, 9 -> seed 32/3; row 4 TR 7 -> (32/3*2 + 7)/3."""
    df = pl.DataFrame({"high": [105.0, 110.0, 115.0, 108.0, 104.0], "low": [95.0, 100.0, 102.0, 99.0, 97.0],
                       "close": [100.0, 108.0, 105.0, 102.0, 100.0]})
    res = df.select(atr=calc_atr("high", "low", "close", 3))["atr"].to_list()
    assert res[:3] == [None, None, None]
    assert res[3] == pytest.approx(32 / 3)
    assert res[4] == pytest.approx((32 / 3 * 2 + 7) / 3)


def test_macd_unchanged_shape():
    closes, _, _ = _series()
    macd, sig, hist = calc_macd("close")
    res = pl.DataFrame({"close": closes}).select(m=macd, s=sig, h=hist)
    assert all(abs(r["m"] - r["s"] - r["h"]) < 1e-12 for r in res.iter_rows(named=True))


def test_calc_volume_ratio_zero_protection():
    """Verify volume ratio does not fail on zero volume."""
    df = pl.DataFrame({"volume": [0.0, 0.0, 0.0, 10.0, 0.0]})
    res = df.select(ratio=calc_volume_ratio("volume", 3))["ratio"].to_list()
    assert res is not None
    assert len(res) == 5


def test_unused_supertrend_helper_is_gone():
    """R7.1: the scanner's calc_supertrend was unused and not a SuperTrend (the strategy is separate)."""
    assert not hasattr(indicators, "calc_supertrend")


# --- R7.2 extended through the lake path (B1 / F-C1) ---------------------------------------------
# The parity tests above feed every implementation the same bars, so they cannot see a bar built from
# the wrong minutes. Here the scanner's indicators on lake 1m -> query_bars_df -> resample_1m_bars must
# equal its indicators on ingest's native 1h bars for the same day (tests/fixtures/b1_lake, 2026-10-04).

LAKE_FIXTURE = Path(__file__).parent / "fixtures" / "b1_lake"
PARITY_COLUMNS = ["ema_20", "ema_50", "rsi", "bb_lower", "bb_middle", "bb_upper", "macd", "macd_signal",
                  "macd_hist", "atr", "volume_ratio"]


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT"])
def test_scanner_indicators_through_lake_resampler_match_native_1h(symbol):
    assert list(LAKE_FIXTURE.glob("binance_klines_enriched/*/*/*/*/date=*/*.parquet")), "B1 lake fixture missing"
    engine = AnalyticsQueryEngine(LAKE_FIXTURE)
    hour_ns = 3600 * 10**9
    native = engine.query_bars_df(symbol, "1h").select(
        bar_open_ns=pl.col("ts_event_ns").cast(pl.Int64) - hour_ns,
        **{c: pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close", "volume")})
    assert native.height == 24
    lake = resample_1m_bars(engine.query_bars_df(symbol, "1m"), "1h").filter(
        pl.col("bar_open_ns").is_in(native["bar_open_ns"].implode()))
    assert lake["bar_open_ns"].to_list() == native["bar_open_ns"].to_list()

    want = ScannerEngine.enrich_indicators(native)
    got = ScannerEngine.enrich_indicators(lake.select(native.columns))
    compared = 0
    for col in PARITY_COLUMNS:
        for i, (g, w) in enumerate(zip(got[col].to_list(), want[col].to_list(), strict=True)):
            assert (g is None) == (w is None), (symbol, col, i)
            if w is not None:
                assert g == pytest.approx(w, rel=1e-9, abs=1e-9), f"{symbol} {col} at hour {i}: lake {g} vs native {w}"
                compared += 1
    assert compared >= 150, compared
