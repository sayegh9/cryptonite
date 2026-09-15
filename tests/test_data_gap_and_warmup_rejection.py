"""Test suite verifying data gap rejection and insufficient warmup rejection."""

from decimal import Decimal
from pathlib import Path
import pytest

from cryptonite_backtester.cadence import (
    DataGapError,
    InsufficientWarmupError,
    validate_bar_cadence,
)
from cryptonite_backtester.runner import BacktestRunner, BacktestRunRequest
from cryptonite_schemas.identifiers import (
    to_nautilus_bar_type,
    to_nautilus_instrument_id,
    to_spot_instrument_id,
)
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from nautilus_trader.model import (
    Bar,
    Currency,
    CurrencyPair,
    Price,
    Quantity,
)


def _make_bar(bar_type, ts_event_ns: int, price: float = 50000.0) -> Bar:
    return Bar(
        bar_type=bar_type,
        open=Price(Decimal(str(price)), 2),
        high=Price(Decimal(str(price + 10.0)), 2),
        low=Price(Decimal(str(price - 10.0)), 2),
        close=Price(Decimal(str(price + 5.0)), 2),
        volume=Quantity(Decimal("1.0"), 5),
        ts_event=ts_event_ns,
        ts_init=ts_event_ns,
    )


def test_cadence_validator_unit_cases():
    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    step_ns = 60_000_000_000
    base_ts = 1725700000000000000

    # 1. Empty bars
    with pytest.raises(DataGapError, match="No bar data found"):
        validate_bar_cadence(
            bars=[],
            timeframe="1m",
            warmup_start_ns=base_ts,
            evaluation_start_ns=base_ts + 10 * step_ns,
            evaluation_end_ns=base_ts + 20 * step_ns,
        )

    # 2. Unsupported timeframe
    bars = [_make_bar(bar_type, base_ts)]
    with pytest.raises(ValueError, match="Unsupported timeframe '10s'"):
        validate_bar_cadence(
            bars=bars,
            timeframe="10s",
            warmup_start_ns=base_ts,
            evaluation_start_ns=base_ts + step_ns,
            evaluation_end_ns=base_ts + 2 * step_ns,
        )

    # 3. Duplicate or out of order bars
    bars = [
        _make_bar(bar_type, base_ts),
        _make_bar(bar_type, base_ts),  # duplicate timestamp
    ]
    with pytest.raises(DataGapError, match="Out of order or duplicate bars"):
        validate_bar_cadence(
            bars=bars,
            timeframe="1m",
            warmup_start_ns=base_ts,
            evaluation_start_ns=base_ts + step_ns,
            evaluation_end_ns=base_ts + 2 * step_ns,
        )

    # 4. Insufficient warmup bars
    bars = [
        _make_bar(bar_type, base_ts),
        _make_bar(bar_type, base_ts + step_ns),
        _make_bar(bar_type, base_ts + 2 * step_ns),
    ]
    with pytest.raises(InsufficientWarmupError, match="Insufficient warmup bars"):
        validate_bar_cadence(
            bars=bars,
            timeframe="1m",
            warmup_start_ns=base_ts,
            evaluation_start_ns=base_ts + 2 * step_ns,
            evaluation_end_ns=base_ts + 5 * step_ns,
            min_warmup_bars=5,
        )

    # 5. Missing bars inside evaluation window
    bars = [
        _make_bar(bar_type, base_ts),
        _make_bar(bar_type, base_ts + step_ns),  # warmup
        _make_bar(bar_type, base_ts + 2 * step_ns),  # eval start
        _make_bar(bar_type, base_ts + 4 * step_ns),  # GAP: skipped 3 * step_ns
    ]
    with pytest.raises(DataGapError, match="Data gap detected in 1m catalog"):
        validate_bar_cadence(
            bars=bars,
            timeframe="1m",
            warmup_start_ns=base_ts,
            evaluation_start_ns=base_ts + 2 * step_ns,
            evaluation_end_ns=base_ts + 5 * step_ns,
            min_warmup_bars=1,
        )

    # 6. No bars inside evaluation window
    bars = [
        _make_bar(bar_type, base_ts),
        _make_bar(bar_type, base_ts + step_ns),
    ]
    with pytest.raises(DataGapError, match="No bars found within the evaluation window"):
        validate_bar_cadence(
            bars=bars,
            timeframe="1m",
            warmup_start_ns=base_ts,
            evaluation_start_ns=base_ts + 5 * step_ns,
            evaluation_end_ns=base_ts + 10 * step_ns,
            min_warmup_bars=1,
        )


def test_runner_rejects_data_gap_and_insufficient_warmup(tmp_path):
    cat_dir = tmp_path / "nautilus_catalog"
    art_dir = tmp_path / "backtest_artifacts"
    mgr = NautilusCatalogManager(cat_dir)

    inst_id = to_nautilus_instrument_id(to_spot_instrument_id("BTCUSDT"))
    pair = CurrencyPair(
        instrument_id=inst_id,
        raw_symbol=inst_id.symbol,
        base_currency=Currency.from_str("BTC"),
        quote_currency=Currency.from_str("USDT"),
        price_precision=2,
        size_precision=5,
        price_increment=Price(Decimal("0.01"), 2),
        size_increment=Quantity(Decimal("0.00001"), 5),
        ts_event=1725700000000000000,
        ts_init=1725700000000000000,
    )
    mgr.write_instruments([pair])

    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    step_ns = 60_000_000_000
    base_ts = 1725700000000000000

    # Write a sequence with a missing bar at index 15
    bars = []
    for i in range(30):
        if i == 15:
            continue  # gap!
        bars.append(_make_bar(bar_type, base_ts + i * step_ns))
    mgr.write_bars(bars)

    runner = BacktestRunner(cat_dir, art_dir)

    # 1. Test data gap inside evaluation window rejection
    req_gap = BacktestRunRequest(
        run_id="run-gap-test",
        strategy_slug="ema-cross",
        strategy_version_id="00000000-0000-0000-0000-000000000001",
        instrument_id=str(inst_id),
        bar_type=str(bar_type),
        timeframe="1m",
        parameters={"fast_period": 3, "slow_period": 7, "trade_size": 0.01},
        warmup_start_ns=base_ts,
        evaluation_start_ns=base_ts + 10 * step_ns,
        evaluation_end_ns=base_ts + 28 * step_ns,
        execution_profile="optimistic",
        initial_balance=10000.0,
    )

    with pytest.raises(DataGapError, match="Data gap detected"):
        runner.run(req_gap)

    # 2. Test insufficient warmup rejection
    req_warmup = BacktestRunRequest(
        run_id="run-warmup-test",
        strategy_slug="ema-cross",
        strategy_version_id="00000000-0000-0000-0000-000000000001",
        instrument_id=str(inst_id),
        bar_type=str(bar_type),
        timeframe="1m",
        parameters={"fast_period": 3, "slow_period": 7, "trade_size": 0.01},
        warmup_start_ns=base_ts,
        evaluation_start_ns=base_ts,  # Invalid: warmup_start_ns == evaluation_start_ns
        evaluation_end_ns=base_ts + 10 * step_ns,
        execution_profile="optimistic",
        initial_balance=10000.0,
    )

    with pytest.raises(ValueError, match="warmup_start_ns .* must be strictly less than evaluation_start_ns"):
        runner.run(req_warmup)
