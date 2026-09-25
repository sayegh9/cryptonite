"""Strict 1m-derived UTC aggregation engine for higher timeframes.

Enforces:
1. Single-source derivation: all timeframes (5m, 15m, 1h, 4h, 1d) are derived from 1m base bars.
2. Strict completeness validation: higher-timeframe windows must be 100% complete and contiguous.
   Windows with missing minutes or gaps are dropped.
3. Precise timestamp contract:
   - bar_open_ns: exact candle open time (left-closed)
   - bar_close_inclusive_ns: exact candle close time (event time)
   - interval_end_exclusive_ns: exact right-exclusive aggregation boundary
"""

from typing import Final

import polars as pl

TIMEFRAME_MINUTES: Final[dict[str, int]] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}

ONE_MINUTE_NS: Final[int] = 60 * 1_000_000_000


def resample_1m_bars(df_1m: pl.DataFrame, target_timeframe: str) -> pl.DataFrame:
    """Resample 1m base bars into target_timeframe with strict window completeness validation.
    
    If target_timeframe == '1m', returns the validated 1m DataFrame with explicit timestamp columns.
    For higher timeframes, groups into UTC-aligned windows and verifies that each window
    contains exactly the required number of contiguous 1m bars.
    """
    target_tf = target_timeframe.lower()
    if target_tf not in TIMEFRAME_MINUTES:
        raise ValueError(f"Unsupported timeframe: {target_timeframe}. Allowed: {list(TIMEFRAME_MINUTES.keys())}")

    if df_1m.is_empty():
        return pl.DataFrame(
            schema={
                "bar_open_ns": pl.Int64,
                "bar_close_inclusive_ns": pl.Int64,
                "interval_end_exclusive_ns": pl.Int64,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Float64,
                "quote_volume": pl.Float64,
                "trades_count": pl.Int64,
            }
        )

    # 1. Ensure input columns are typed correctly
    # Handle ts_event_ns from analytics lake if bar_open_ns is not yet set
    exprs = []
    if "bar_open_ns" not in df_1m.columns:
        if "ts_init_ns" in df_1m.columns:
            exprs.append(pl.col("ts_init_ns").cast(pl.Int64).alias("bar_open_ns"))
        elif "ts_event_ns" in df_1m.columns:
            exprs.append(pl.col("ts_event_ns").cast(pl.Int64).alias("bar_open_ns"))
        else:
            raise ValueError("Input DataFrame missing bar_open_ns, ts_init_ns, or ts_event_ns")
    else:
        exprs.append(pl.col("bar_open_ns").cast(pl.Int64))

    for col_name in ["open", "high", "low", "close", "volume"]:
        if col_name in df_1m.columns:
            exprs.append(pl.col(col_name).cast(pl.Float64))

    if "quote_volume" in df_1m.columns:
        exprs.append(pl.col("quote_volume").cast(pl.Float64))
    else:
        exprs.append(pl.lit(0.0).alias("quote_volume"))

    if "trades_count" in df_1m.columns:
        exprs.append(pl.col("trades_count").cast(pl.Int64))
    else:
        exprs.append(pl.lit(0).cast(pl.Int64).alias("trades_count"))

    clean_df = df_1m.select(exprs).sort("bar_open_ns").unique(subset=["bar_open_ns"])

    # If target is 1m, enrich with explicit timestamps and return
    if target_tf == "1m":
        return clean_df.with_columns(
            bar_close_inclusive_ns=pl.col("bar_open_ns") + ONE_MINUTE_NS - 1_000_000,
            interval_end_exclusive_ns=pl.col("bar_open_ns") + ONE_MINUTE_NS,
        ).select([
            "bar_open_ns",
            "bar_close_inclusive_ns",
            "interval_end_exclusive_ns",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "trades_count",
        ])

    target_minutes = TIMEFRAME_MINUTES[target_tf]
    window_duration_ns = target_minutes * ONE_MINUTE_NS
    expected_bar_count = target_minutes

    # 2. Compute UTC window open: floor(bar_open_ns / window_duration_ns) * window_duration_ns
    windowed = clean_df.with_columns(
        window_open_ns=(pl.col("bar_open_ns") // window_duration_ns) * window_duration_ns
    )

    # 3. Aggregate each window
    aggregated = windowed.group_by("window_open_ns").agg([
        pl.len().alias("bar_count"),
        pl.min("bar_open_ns").alias("first_bar_open_ns"),
        pl.max("bar_open_ns").alias("last_bar_open_ns"),
        pl.first("open").alias("open"),
        pl.max("high").alias("high"),
        pl.min("low").alias("low"),
        pl.last("close").alias("close"),
        pl.sum("volume").alias("volume"),
        pl.sum("quote_volume").alias("quote_volume"),
        pl.sum("trades_count").alias("trades_count"),
    ]).sort("window_open_ns")

    # 4. Strict Completeness Verification:
    # A window is complete if and only if:
    # a) bar_count == expected_bar_count
    # b) first_bar_open_ns == window_open_ns
    # c) last_bar_open_ns == window_open_ns + (expected_bar_count - 1) * ONE_MINUTE_NS
    # Any gapped or partial window fails this predicate and is excluded.
    expected_last_open = pl.col("window_open_ns") + ((expected_bar_count - 1) * ONE_MINUTE_NS)
    is_complete_expr = (
        (pl.col("bar_count") == expected_bar_count)
        & (pl.col("first_bar_open_ns") == pl.col("window_open_ns"))
        & (pl.col("last_bar_open_ns") == expected_last_open)
    )

    complete_windows = aggregated.filter(is_complete_expr)

    # 5. Format final explicit timestamp contract
    return complete_windows.select(
        bar_open_ns=pl.col("window_open_ns"),
        bar_close_inclusive_ns=pl.col("window_open_ns") + window_duration_ns - 1_000_000,
        interval_end_exclusive_ns=pl.col("window_open_ns") + window_duration_ns,
        open=pl.col("open"),
        high=pl.col("high"),
        low=pl.col("low"),
        close=pl.col("close"),
        volume=pl.col("volume"),
        quote_volume=pl.col("quote_volume"),
        trades_count=pl.col("trades_count"),
    ).sort("bar_open_ns")
