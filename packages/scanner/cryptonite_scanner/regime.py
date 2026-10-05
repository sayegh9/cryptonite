"""Market regime tags from 1h bars (remediation Phase 8, F1). Reporting only: nothing here feeds a
trade decision, sizing, routing, triage or a gate.

One tag per bar, decided in this order:

1. ``HIGH_VOL``: ATR(14) as a percentage of the close is above the 90th percentile of its own values
   over the trailing 30 days (720 hourly bars, the current one included);
2. otherwise ``TREND_UP`` / ``TREND_DOWN``: ADX(14) >= 25, direction from +DI against -DI;
3. otherwise ``CHOP``.

``None`` when there are too few bars to compute all three inputs. ATR, the directional movement and
ADX use Wilder smoothing seeded with an SMA, the scanner's TA-Lib convention (``indicators.py``).
"""

from typing import Literal

import polars as pl

from cryptonite_scanner.indicators import calc_atr

Regime = Literal["TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOL"]
REGIMES: tuple[Regime, ...] = ("TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOL")

ATR_PERIOD = 14
ADX_PERIOD = 14
ADX_TREND_MIN = 25.0  # ADX at or above this is a trend
HIGH_VOL_QUANTILE = 0.90  # ATR% above this quantile of its trailing window is high volatility
HIGH_VOL_WINDOW_BARS = 30 * 24  # trailing 30 days of 1h bars
MIN_ATR_SAMPLES = 7 * 24  # the percentile needs at least a week of ATR% values
# ATR% needs ATR_PERIOD bars before its first value, then MIN_ATR_SAMPLES values; ADX needs 2 x period
MIN_BARS = max(ATR_PERIOD + MIN_ATR_SAMPLES, 2 * ADX_PERIOD)

_TS_COLUMNS = ("ts_event_ns", "ts_event", "ts", "timestamp")


def _seeded_wilder(values: pl.Expr, seed_row: int, period: int) -> pl.Expr:
    """Wilder smoothing whose first value, at row ``seed_row``, is the SMA of the ``period`` values
    ending there; then ``avg_t = avg_{t-1} + (x_t - avg_{t-1}) / period``. Null before ``seed_row``."""
    idx = pl.int_range(pl.len())
    seeded = (
        pl.when(idx == seed_row).then(values.rolling_mean(window_size=period))
        .when(idx > seed_row).then(values)
        .otherwise(None)
    )
    return seeded.ewm_mean(alpha=1.0 / period, adjust=False)


def _prepared(bars_1h: pl.DataFrame) -> pl.DataFrame:
    ts = next((c for c in _TS_COLUMNS if c in bars_1h.columns), None)
    df = bars_1h.sort(ts) if ts else bars_1h
    return df.select(pl.col("high").cast(pl.Float64), pl.col("low").cast(pl.Float64), pl.col("close").cast(pl.Float64))


def regime_inputs(bars_1h: pl.DataFrame) -> pl.DataFrame:
    """Per bar: ``atr_pct``, its trailing ``atr_pct_q90``, ``adx``, ``plus_di``, ``minus_di`` (nulls
    where not yet defined). Rows follow the bars sorted by time."""
    df = _prepared(bars_1h)
    p = ADX_PERIOD
    up = pl.col("high").diff()
    down = -pl.col("low").diff()
    prev_close = pl.col("close").shift(1)
    true_range = pl.max_horizontal(pl.col("high") - pl.col("low"), (pl.col("high") - prev_close).abs(),
                                   (pl.col("low") - prev_close).abs())
    df = df.with_columns(
        (calc_atr(period=ATR_PERIOD) / pl.col("close") * 100.0).alias("atr_pct"),
        pl.when((up > down) & (up > 0)).then(up).otherwise(0.0).alias("_pdm"),
        pl.when((down > up) & (down > 0)).then(down).otherwise(0.0).alias("_mdm"),
        true_range.alias("_tr"),
    ).with_columns(
        _seeded_wilder(pl.col("_pdm"), p, p).alias("_spdm"),
        _seeded_wilder(pl.col("_mdm"), p, p).alias("_smdm"),
        _seeded_wilder(pl.col("_tr"), p, p).alias("_str"),
    ).with_columns(
        (100.0 * pl.col("_spdm") / pl.col("_str")).alias("plus_di"),
        (100.0 * pl.col("_smdm") / pl.col("_str")).alias("minus_di"),
    ).with_columns(
        pl.when(pl.col("plus_di") + pl.col("minus_di") > 0)
        .then(100.0 * (pl.col("plus_di") - pl.col("minus_di")).abs() / (pl.col("plus_di") + pl.col("minus_di")))
        .otherwise(pl.when(pl.col("plus_di").is_null()).then(None).otherwise(0.0))
        .alias("_dx"),
        pl.col("atr_pct").rolling_quantile(quantile=HIGH_VOL_QUANTILE, interpolation="linear",
                                           window_size=HIGH_VOL_WINDOW_BARS, min_samples=MIN_ATR_SAMPLES)
        .alias("atr_pct_q90"),
    ).with_columns(
        _seeded_wilder(pl.col("_dx"), 2 * p - 1, p).alias("adx"),
    )
    return df.select("atr_pct", "atr_pct_q90", "adx", "plus_di", "minus_di")


def regime_series(bars_1h: pl.DataFrame) -> list[Regime | None]:
    """The regime at every bar (sorted by time), ``None`` where the inputs are not defined yet."""
    if bars_1h.height == 0:
        return []
    out: list[Regime | None] = []
    for atr_pct, q90, adx, pdi, mdi in regime_inputs(bars_1h).iter_rows():
        out.append(_tag(atr_pct, q90, adx, pdi, mdi))
    return out


def _tag(atr_pct, q90, adx, pdi, mdi) -> Regime | None:
    if atr_pct is None or q90 is None or adx is None or pdi is None or mdi is None:
        return None
    if atr_pct > q90:
        return "HIGH_VOL"
    if adx >= ADX_TREND_MIN:
        return "TREND_UP" if pdi >= mdi else "TREND_DOWN"
    return "CHOP"


def classify_regime(bars_1h: pl.DataFrame) -> Regime | None:
    """The regime at the last bar of ``bars_1h`` (columns ``high``, ``low``, ``close`` and optionally a
    time column), or ``None`` with fewer than ``MIN_BARS`` bars or undefined inputs."""
    if bars_1h is None or bars_1h.height < MIN_BARS:
        return None
    return _tag(*regime_inputs(bars_1h).row(-1))
