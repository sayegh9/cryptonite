"""Can a backtest window be read from the catalog? Checked before a run is queued, so a window with no
data is refused with a reason instead of failing later in the worker."""

from datetime import UTC, datetime
from pathlib import Path


def _when(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime("%Y-%m-%d %H:%M")


def instrument_defined(catalog_path: str | Path, instrument_id: str) -> bool:
    """True when the catalog holds an instrument definition (the backtest runner needs one)."""
    folder = Path(catalog_path) / "data" / "instruments" / instrument_id
    return folder.is_dir() and any(folder.glob("*.parquet"))


def window_problem(*, instrument_id: str, bar_type: str, instrument_defined: bool,
                   history: tuple[int, int] | None, step_ns: int,
                   window_start_ns: int, window_end_ns: int) -> str | None:
    """Why the window cannot be backtested, or None. ``history`` is the contiguous catalog range of the
    bar type (start, newest bar); the newest bar may lag the window end by one bar step."""
    if not instrument_defined:
        return f"{instrument_id} has no instrument definition in the catalog"
    if history is None:
        return f"the catalog has no bars for {bar_type}"
    start, end = history
    if start > window_start_ns:
        return (f"the catalog's bars for {bar_type} start {_when(start)}; the run needs data from "
                f"{_when(window_start_ns)} (warm-up included)")
    if end + step_ns < window_end_ns:
        return f"the catalog's bars for {bar_type} end {_when(end)}; the run ends {_when(window_end_ns)}"
    return None
