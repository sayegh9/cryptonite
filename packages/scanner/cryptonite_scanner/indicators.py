"""Vectorized technical indicators implemented with Polars expressions.

Operates purely on Float64 values for vectorized execution speed and precision.
Golden-tested against reference TA values.
"""

import polars as pl


def calc_sma(col: str = "close", period: int = 20) -> pl.Expr:
    """Simple Moving Average."""
    return pl.col(col).rolling_mean(window_size=period)


def calc_ema(col: str = "close", period: int = 20) -> pl.Expr:
    """Exponential Moving Average."""
    return pl.col(col).ewm_mean(span=period, adjust=False)


def calc_rsi(col: str = "close", period: int = 14) -> pl.Expr:
    """Wilder's Smoothed Relative Strength Index (RSI)."""
    diff = pl.col(col).diff()
    gain = pl.when(diff > 0).then(diff).otherwise(0.0)
    loss = pl.when(diff < 0).then(-diff).otherwise(0.0)

    avg_gain = gain.ewm_mean(alpha=1.0 / period, adjust=False)
    avg_loss = loss.ewm_mean(alpha=1.0 / period, adjust=False)

    rs = avg_gain / pl.when(avg_loss == 0.0).then(1e-10).otherwise(avg_loss)
    return 100.0 - (100.0 / (1.0 + rs))


def calc_bollinger_bands(
    col: str = "close", period: int = 20, std_dev: float = 2.0
) -> tuple[pl.Expr, pl.Expr, pl.Expr]:
    """Bollinger Bands: returns (lower, middle, upper)."""
    middle = pl.col(col).rolling_mean(window_size=period)
    std = pl.col(col).rolling_std(window_size=period)
    upper = middle + (std * std_dev)
    lower = middle - (std * std_dev)
    return lower, middle, upper


def calc_macd(
    col: str = "close", fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pl.Expr, pl.Expr, pl.Expr]:
    """Moving Average Convergence Divergence (MACD). Returns (macd, signal, hist)."""
    ema_fast = pl.col(col).ewm_mean(span=fast, adjust=False)
    ema_slow = pl.col(col).ewm_mean(span=slow, adjust=False)
    macd = ema_fast - ema_slow
    sig = macd.ewm_mean(span=signal, adjust=False)
    hist = macd - sig
    return macd, sig, hist


def calc_atr(
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
    period: int = 14,
) -> pl.Expr:
    """Average True Range (Wilder's smoothed ATR)."""
    prev_close = pl.col(close_col).shift(1)
    tr1 = pl.col(high_col) - pl.col(low_col)
    tr2 = (pl.col(high_col) - prev_close).abs()
    tr3 = (pl.col(low_col) - prev_close).abs()
    true_range = pl.max_horizontal(tr1, tr2, tr3)
    return true_range.ewm_mean(alpha=1.0 / period, adjust=False)


def calc_volume_ratio(volume_col: str = "volume", period: int = 20) -> pl.Expr:
    """Volume ratio relative to its rolling simple moving average."""
    vol_sma = pl.col(volume_col).rolling_mean(window_size=period)
    return pl.col(volume_col) / pl.when(vol_sma == 0.0).then(1.0).otherwise(vol_sma)


def calc_supertrend(
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
    period: int = 10,
    multiplier: float = 3.0,
) -> pl.Expr:
    """Supertrend upper/lower threshold level."""
    hl2 = (pl.col(high_col) + pl.col(low_col)) / 2.0
    atr = calc_atr(high_col, low_col, close_col, period=period)
    # Basic basic supertrend anchor
    return hl2 - (multiplier * atr)
