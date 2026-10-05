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


def _wilder(values: pl.Expr, period: int) -> pl.Expr:
    """Wilder's smoothing seeded with the SMA of the first ``period`` values after the first row
    (the TA-Lib convention): null up to row ``period``, the SMA of rows 1..period there, then
    ``avg_t = avg_{t-1} + (x_t - avg_{t-1}) / period``. Row 0 is skipped because RSI gains and the
    true range need a previous close."""
    idx = pl.int_range(pl.len())
    seeded = (
        pl.when(idx == period).then(values.rolling_mean(window_size=period))
        .when(idx > period).then(values)
        .otherwise(None)
    )
    return seeded.ewm_mean(alpha=1.0 / period, adjust=False)


def calc_rsi(col: str = "close", period: int = 14) -> pl.Expr:
    """Wilder's Smoothed Relative Strength Index (RSI), SMA-seeded (TA-Lib convention)."""
    diff = pl.col(col).diff()
    gain = pl.when(diff > 0).then(diff).otherwise(0.0)
    loss = pl.when(diff < 0).then(-diff).otherwise(0.0)

    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)

    rs = avg_gain / pl.when(avg_loss == 0.0).then(1e-10).otherwise(avg_loss)
    return 100.0 - (100.0 / (1.0 + rs))


def calc_bollinger_bands(
    col: str = "close", period: int = 20, std_dev: float = 2.0
) -> tuple[pl.Expr, pl.Expr, pl.Expr]:
    """Bollinger Bands: returns (lower, middle, upper). Population standard deviation (ddof=0), as
    TA-Lib and most charting libraries compute it."""
    middle = pl.col(col).rolling_mean(window_size=period)
    std = pl.col(col).rolling_std(window_size=period, ddof=0)
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
    """Average True Range (Wilder's smoothed ATR), SMA-seeded (TA-Lib convention)."""
    prev_close = pl.col(close_col).shift(1)
    tr1 = pl.col(high_col) - pl.col(low_col)
    tr2 = (pl.col(high_col) - prev_close).abs()
    tr3 = (pl.col(low_col) - prev_close).abs()
    true_range = pl.max_horizontal(tr1, tr2, tr3)
    return _wilder(true_range, period)


def calc_volume_ratio(volume_col: str = "volume", period: int = 20) -> pl.Expr:
    """Volume ratio relative to its rolling simple moving average."""
    vol_sma = pl.col(volume_col).rolling_mean(window_size=period)
    return pl.col(volume_col) / pl.when(vol_sma == 0.0).then(1.0).otherwise(vol_sma)


def calc_stoch_rsi(
    col: str = "close", rsi_period: int = 14, stoch_period: int = 14, k_smooth: int = 3, d_smooth: int = 3
) -> tuple[pl.Expr, pl.Expr]:
    """Stochastic RSI. Returns (K, D)."""
    rsi = calc_rsi(col, rsi_period)
    rsi_min = rsi.rolling_min(window_size=stoch_period)
    rsi_max = rsi.rolling_max(window_size=stoch_period)
    
    stoch_rsi = pl.when(rsi_max == rsi_min).then(0.0).otherwise((rsi - rsi_min) / (rsi_max - rsi_min)) * 100.0
    
    k = stoch_rsi.rolling_mean(window_size=k_smooth)
    d = k.rolling_mean(window_size=d_smooth)
    
    return k, d


def calc_ema_set(col: str = "close") -> tuple[pl.Expr, pl.Expr, pl.Expr]:
    """EMA 7, 25, 99 set."""
    return calc_ema(col, 7), calc_ema(col, 25), calc_ema(col, 99)


def calc_volume_ma(volume_col: str = "volume", period: int = 5) -> pl.Expr:
    """Volume Simple Moving Average."""
    return calc_sma(volume_col, period)


def calc_wick_ratio(
    open_col: str = "open", high_col: str = "high", low_col: str = "low", close_col: str = "close"
) -> tuple[pl.Expr, pl.Expr]:
    """Returns (upper_wick_ratio, lower_wick_ratio) relative to the total candle range."""
    candle_range = pl.col(high_col) - pl.col(low_col)
    
    real_body_top = pl.max_horizontal(pl.col(open_col), pl.col(close_col))
    real_body_bottom = pl.min_horizontal(pl.col(open_col), pl.col(close_col))
    
    upper_wick = pl.col(high_col) - real_body_top
    lower_wick = real_body_bottom - pl.col(low_col)
    
    safe_range = pl.when(candle_range == 0.0).then(1e-10).otherwise(candle_range)
    
    return (upper_wick / safe_range), (lower_wick / safe_range)

