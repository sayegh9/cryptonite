"""Test suite verifying strategy registry, source hashing, and deterministic execution."""

from decimal import Decimal
from pathlib import Path
import pytest

from cryptonite_backtester.runner import BacktestRunner, BacktestRunRequest
from cryptonite_schemas.identifiers import (
    to_nautilus_bar_type,
    to_nautilus_instrument_id,
    to_spot_instrument_id,
)
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from cryptonite_strategies.registry import (
    STRATEGY_REGISTRY,
    get_source_tree_hash,
    get_strategy_class,
    get_strategy_config_class,
    get_strategy_meta,
    get_strategy_source_hash,
    list_strategies,
)
from nautilus_trader.model import (
    Bar,
    Currency,
    CurrencyPair,
    Price,
    Quantity,
)


def test_source_tree_hashing_stability():
    """Verify source tree hash is 64-char hex, deterministic across repeated invocations."""
    h1 = get_source_tree_hash()
    h2 = get_source_tree_hash()
    assert len(h1) == 64
    assert h1 == h2


def test_strategy_metadata_and_hashes():
    """Verify registry methods return valid metadata and per-strategy hashes."""
    strategies = list_strategies()
    assert len(strategies) == 3
    slugs = {s["slug"] for s in strategies}
    assert slugs == {"ema-cross", "rsi-reversal", "supertrend"}

    for slug in slugs:
        meta = get_strategy_meta(slug)
        assert meta["slug"] == slug
        assert len(meta["code_hash"]) == 64
        assert meta["code_hash"] == get_strategy_source_hash(slug)

        cfg_cls = get_strategy_config_class(slug)
        assert cfg_cls is not None

        strat_cls = get_strategy_class(slug)
        assert strat_cls is not None

    with pytest.raises(KeyError):
        get_strategy_meta("non-existent-strategy")


def test_all_strategies_instantiation_and_execution(tmp_path: Path):
    """Verify all 3 registered strategies can execute cleanly against catalog data."""
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

    bars = []
    px = 50000.0
    for i in range(50):
        ts = base_ts + i * step_ns
        o = px
        h = px + 25.0
        l = px - 25.0
        c = px + (10.0 if (i % 3) == 0 else -10.0)
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price(Decimal(str(o)), 2),
                high=Price(Decimal(str(h)), 2),
                low=Price(Decimal(str(l)), 2),
                close=Price(Decimal(str(c)), 2),
                volume=Quantity(Decimal("1.0"), 5),
                ts_event=ts,
                ts_init=ts,
            )
        )
        px = c
    mgr.write_bars(bars)

    runner = BacktestRunner(cat_dir, art_dir)

    for slug in ["ema-cross", "rsi-reversal", "supertrend"]:
        req = BacktestRunRequest(
            run_id=f"run-strat-{slug}",
            strategy_slug=slug,
            strategy_version_id="00000000-0000-0000-0000-000000000001",
            instrument_id=str(inst_id),
            bar_type=str(bar_type),
            timeframe="1m",
            warmup_start_ns=base_ts,
            evaluation_start_ns=base_ts + 15 * step_ns,
            evaluation_end_ns=base_ts + 49 * step_ns,
            execution_profile="optimistic",
            initial_balance=10000.0,
        )
        result = runner.run(req)
        assert result["run_config_hash"] is not None
        assert len(result["run_config_hash"]) == 64
        assert result["total_return"] is not None


def test_rsi_oversold_recovery_and_exit_execution(tmp_path: Path):
    """Verify RSI Oversold Reversal triggers long entry on bounce and exit when recovering above exit threshold."""
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

    bars = []
    px = 50000.0
    # Create pattern: 15 flat bars, 15 sharp drop bars (pushing RSI < 30), then 15 sharp rising bars (reversal & exit)
    for i in range(50):
        ts = base_ts + i * step_ns
        if i < 15:
            delta = 0.0
        elif i < 30:
            delta = -40.0  # push into oversold
        else:
            delta = 60.0   # bounce out of oversold and push above 50
        c = px + delta
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price(Decimal(str(px)), 2),
                high=Price(Decimal(str(max(px, c) + 5.0)), 2),
                low=Price(Decimal(str(min(px, c) - 5.0)), 2),
                close=Price(Decimal(str(c)), 2),
                volume=Quantity(Decimal("1.0"), 5),
                ts_event=ts,
                ts_init=ts,
            )
        )
        px = c
    mgr.write_bars(bars)

    runner = BacktestRunner(cat_dir, art_dir)
    req = BacktestRunRequest(
        run_id="run-rsi-bounce-test",
        strategy_slug="rsi-reversal",
        strategy_version_id="00000000-0000-0000-0000-000000000001",
        instrument_id=str(inst_id),
        bar_type=str(bar_type),
        timeframe="1m",
        parameters={"rsi_period": 7, "oversold_threshold": 30.0, "exit_threshold": 50.0, "trade_size": 0.01},
        warmup_start_ns=base_ts,
        evaluation_start_ns=base_ts + 15 * step_ns,
        evaluation_end_ns=base_ts + 49 * step_ns,
        execution_profile="optimistic",
        initial_balance=10000.0,
    )
    result = runner.run(req)
    assert result["total_trades"] >= 1


def test_cryptonite_strategy_base_helpers():
    """Verify CryptoniteStrategy helper methods (get_trade_qty, is_in_warmup, order suppression)."""
    from cryptonite_strategies.base import CryptoniteStrategy, CryptoniteStrategyConfig
    from nautilus_trader.model import BarSpecification, BarAggregation, PriceType, InstrumentId

    cfg = CryptoniteStrategyConfig(
        instrument_id="BTCUSDT.BINANCE",
        bar_type="BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL",
        trade_size=0.05,
        evaluation_start_ns=1725700500000000000,
    )
    strat = CryptoniteStrategy(config=cfg)
    # Check trade_size fallback when _instrument is None
    qty = strat.get_trade_qty()
    assert float(qty) == 0.05

    # Mock portfolio and log
    from unittest.mock import MagicMock
    strat._portfolio = MagicMock()
    strat._portfolio.is_net_flat.return_value = True
    strat._portfolio.is_net_long.return_value = False
    strat._portfolio.is_net_short.return_value = False
    strat._log = MagicMock()

    # When no bar and clock unregistered, safely defaults to True
    assert strat.is_in_warmup() is True

    # With bar timestamps
    warm_bar = MagicMock()
    warm_bar.ts_event = 1725700400000000000  # Before evaluation_start_ns
    assert strat.is_in_warmup(warm_bar) is True

    eval_bar = MagicMock()
    eval_bar.ts_event = 1725700600000000000  # After evaluation_start_ns
    assert strat.is_in_warmup(eval_bar) is False

    assert strat.is_flat() is True
    assert strat.is_long() is False
    assert strat.is_short() is False

    # During warmup, order operations are suppressed without error
    order_mock = MagicMock()
    strat.submit_order(order_mock)
    strat.submit_order_list([order_mock])
    strat.buy_market()
    strat.sell_market()
    strat.close_all()
    strat.close_all_positions(strat.instrument_id)


