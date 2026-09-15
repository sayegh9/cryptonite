"""Content-level cadence and data integrity validation for backtests."""

from typing import Any, Sequence

from nautilus_trader.model import Bar

TIMEFRAME_STEP_NS = {
    "1m": 60_000_000_000,
    "3m": 180_000_000_000,
    "5m": 300_000_000_000,
    "15m": 900_000_000_000,
    "30m": 1_800_000_000_000,
    "1h": 3_600_000_000_000,
    "2h": 7_200_000_000_000,
    "4h": 14_400_000_000_000,
    "1d": 86_400_000_000_000,
}


class DataIntegrityError(Exception):
    """Base exception for data validation failures in backtesting."""


class DataGapError(DataIntegrityError):
    """Raised when a gap is detected in the input bar sequence."""


class InsufficientWarmupError(DataIntegrityError):
    """Raised when the warmup period contains insufficient data."""


def validate_bar_cadence(
    bars: Any,
    timeframe: str,
    warmup_start_ns: int,
    evaluation_start_ns: int,
    evaluation_end_ns: int,
    min_warmup_bars: int = 1,
) -> None:
    """Validate that the bar series contains continuous cadence with zero missing bars.
    
    Verifies:
    1. At least min_warmup_bars exist between warmup_start_ns and evaluation_start_ns.
    2. At least one bar exists inside the evaluation window.
    3. The delta between consecutive bars in the evaluation window matches TIMEFRAME_STEP_NS.
    """
    # Generator handles empty checks below("No bar data found in catalog for the requested instrument and time bounds.")

    step_ns = TIMEFRAME_STEP_NS.get(timeframe)
    if step_ns is None:
        raise ValueError(f"Unsupported timeframe '{timeframe}' for cadence check.")

    warmup_count = 0
    eval_count = 0

    def _extract_ts(item: Any) -> int:
        return int(getattr(item, "ts_event", item))

    prev_ts = None
    for item in bars:
        ts = _extract_ts(item)
        if warmup_start_ns <= ts < evaluation_start_ns:
            warmup_count += 1
        elif evaluation_start_ns <= ts <= evaluation_end_ns:
            eval_count += 1

        if prev_ts is not None:
            delta = ts - prev_ts

            if delta < step_ns:
                # Duplicate or out-of-order bar
                raise DataGapError(
                    f"Out of order or duplicate bars detected: prev={prev_ts}, curr={ts}, delta={delta}ns"
                )
            elif delta > step_ns:
                # Only raise DataGapError if the gap touches or falls inside evaluation range
                if ts >= evaluation_start_ns and prev_ts <= evaluation_end_ns:
                    missing_bars = (delta // step_ns) - 1
                    raise DataGapError(
                        f"Data gap detected in {timeframe} catalog: missing {missing_bars} bar(s) "
                        f"between {prev_ts} and {ts} (expected step {step_ns}ns)."
                    )
        prev_ts = ts

    if prev_ts is None:
        raise DataGapError("No bar data found in catalog for the requested instrument and time bounds.")

    if warmup_count < min_warmup_bars:
        raise InsufficientWarmupError(
            f"Insufficient warmup bars: found {warmup_count} bars between {warmup_start_ns} "
            f"and {evaluation_start_ns}, required at least {min_warmup_bars}."
        )

    if eval_count == 0:
        raise DataGapError(
            f"No bars found within the evaluation window [{evaluation_start_ns}, {evaluation_end_ns}]."
        )
