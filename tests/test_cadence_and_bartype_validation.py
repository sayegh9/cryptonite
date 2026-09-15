"""Tests verifying strict bar cadence, duplicate timestamps, missing intervals, and BarType enforcement."""

from decimal import Decimal
from pathlib import Path

import pytest
from cryptonite_backtester.cadence import (
    DataGapError,
    InsufficientWarmupError,
    validate_bar_cadence,
)
from cryptonite_backtester.runner import BacktestRunner, BacktestRunRequest
from cryptonite_schemas.identifiers import to_nautilus_bar_type
from nautilus_trader.model import Bar, Price, Quantity


def _make_bar(bar_type_str: str, ts_ns: int, close_px: float = 50000.0) -> Bar:
    bt = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m") if "EXTERNAL" in bar_type_str else None
    return Bar(
        bar_type=bt or to_nautilus_bar_type("BTCUSDT.BINANCE", "1m"),
        open=Price(Decimal(str(close_px)), 2),
        high=Price(Decimal(str(close_px + 10)), 2),
        low=Price(Decimal(str(close_px - 10)), 2),
        close=Price(Decimal(str(close_px)), 2),
        volume=Quantity(Decimal("1.0"), 5),
        ts_event=ts_ns,
        ts_init=ts_ns,
    )


def test_duplicate_bar_timestamp_rejection():
    """Verify that adjacent duplicate bar timestamps trigger DataGapError."""
    step = 60_000_000_000
    base = 1725700000000000000
    bars = [
        _make_bar("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL", base),
        _make_bar("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL", base),  # Duplicate timestamp!
        _make_bar("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL", base + step),
    ]

    with pytest.raises(DataGapError, match="Out of order or duplicate bars detected"):
        validate_bar_cadence(
            bars=bars,
            timeframe="1m",
            warmup_start_ns=base,
            evaluation_start_ns=base + step,
            evaluation_end_ns=base + (2 * step),
        )


def test_out_of_order_bar_timestamp_rejection():
    """Verify that decreasing timestamps trigger DataGapError."""
    step = 60_000_000_000
    base = 1725700000000000000
    bars = [
        _make_bar("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL", base + step),
        _make_bar("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL", base),  # Decreasing!
    ]

    with pytest.raises(DataGapError, match="Out of order or duplicate bars detected"):
        validate_bar_cadence(
            bars=bars,
            timeframe="1m",
            warmup_start_ns=base,
            evaluation_start_ns=base + step,
            evaluation_end_ns=base + (2 * step),
        )


def test_missing_bars_gap_rejection():
    """Verify that missing bars in the evaluation window trigger DataGapError."""
    step = 60_000_000_000
    base = 1725700000000000000
    bars = [
        _make_bar("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL", base),
        _make_bar("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL", base + (5 * step)),  # 4 missing bars!
    ]

    with pytest.raises(DataGapError, match="Data gap detected in 1m catalog"):
        validate_bar_cadence(
            bars=bars,
            timeframe="1m",
            warmup_start_ns=base,
            evaluation_start_ns=base + (2 * step),
            evaluation_end_ns=base + (10 * step),
        )


def test_wrong_bartype_last_internal_rejection(tmp_path: Path):
    """Verify that runner strictly rejects non-canonical LAST-INTERNAL bar types."""
    runner = BacktestRunner(tmp_path / "cat", tmp_path / "art")
    req = BacktestRunRequest(
        run_id="run-reject-internal",
        strategy_slug="ema-cross",
        strategy_version_id="00000000-0000-0000-0000-000000000001",
        instrument_id="BTCUSDT.BINANCE",
        bar_type="BTCUSDT.BINANCE-1-MINUTE-LAST-INTERNAL",  # WRONG! Internal bars do not drive simulated L1
        timeframe="1m",
        warmup_start_ns=1725700000000000000,
        evaluation_start_ns=1725700600000000000,
        evaluation_end_ns=1725701200000000000,
    )

    with pytest.raises(ValueError, match="Milestone 3 requires canonical '...-LAST-EXTERNAL'"):
        runner.run(req)
