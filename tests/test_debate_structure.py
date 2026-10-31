"""On-demand debate levels are calculated from complete closed bars and fail closed."""

import importlib
import math
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/debate_worker"))
structure = importlib.import_module("structure")


def _bars(count: int, start_ns: int) -> pl.DataFrame:
    """Build continuous 1m lake rows with nonzero ATR and enough EMA warm-up."""
    opens = [start_ns + i * structure.ONE_MINUTE_NS for i in range(count)]
    close = [100 + math.sin(i / 30) * 0.5 for i in range(count)]
    return pl.DataFrame({
        "bar_open_ns": opens, "open": close,
        "high": [value + 0.2 for value in close],
        "low": [value - 0.2 for value in close],
        "close": close, "volume": [10.0] * count,
    })


def test_closed_intraday_bars_produce_sided_levels_and_stale_bars_refuse(monkeypatch):
    """Both sides share scanner math; a two-bar-old decision frame gives no entry levels."""
    minute = structure.ONE_MINUTE_NS
    start = (1_800_000_000_000_000_000 // minute) * minute
    bars = _bars(3120, start)

    class Lake:
        def __init__(self, *args, **kwargs):
            self._con = self

        def query_bars_df(self, *args, **kwargs):
            return bars

        def close(self):
            pass

    monkeypatch.setattr(structure, "AnalyticsQueryEngine", Lake)
    now = start + 3120 * minute
    long_stop, long_target, _, long_source = structure.levels_from_lake("BTCUSDT", "INTRADAY", "BUY", 2, decision_ns=now)
    short_stop, short_target, _, short_source = structure.levels_from_lake("BTCUSDT", "INTRADAY", "SELL", 2, decision_ns=now)
    # D-P3-7: the risk config is the first preset the style runs for that side, and the source names it
    assert (long_source, short_source) == ("structure:scalp-multi-tf", "structure:scalp-multi-tf-short")
    assert long_stop is not None and long_target is not None and long_stop < long_target
    assert short_stop is not None and short_target is not None and short_stop > short_target
    stop, target, reason, _ = structure.levels_from_lake(
        "BTCUSDT", "INTRADAY", "BUY", 2, decision_ns=now + 31 * minute)
    assert (stop, target) == (None, None)
    assert reason == "latest 15m bar is older than two bars"

    # Whole-candle gaps are absent from the resampled frame even though every remaining candle is complete.
    gapped = bars.filter(~pl.col("bar_open_ns").is_between(start + 600 * minute, start + 614 * minute))
    monkeypatch.setattr(Lake, "query_bars_df", lambda self, *args, **kwargs: gapped)
    stop, target, reason, _ = structure.levels_from_lake("BTCUSDT", "INTRADAY", "BUY", 2, decision_ns=now)
    assert (stop, target) == (None, None)
    assert reason == "fewer than 200 contiguous 15m bars"
