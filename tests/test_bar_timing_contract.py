"""Test suite verifying execution timing contract under zero latency.

Verifies:
1. Orders submitted in `on_bar` at bar T execute against the simulated L1 book state
   at bar T's close, NOT next bar's open.
2. The disclosure string is present on CryptoniteStrategy.
"""

from decimal import Decimal
from pathlib import Path

from cryptonite_schemas.identifiers import (
    to_nautilus_bar_type,
    to_nautilus_instrument_id,
    to_spot_instrument_id,
)
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from cryptonite_strategies.base import CryptoniteStrategy, CryptoniteStrategyConfig
from nautilus_trader.backtest import (
    BacktestDataConfig,
    BacktestEngineConfig,
    BacktestNode,
    BacktestRunConfig,
    BacktestVenueConfig,
)
from nautilus_trader.execution import DefaultFillModel
from nautilus_trader.model import (
    AccountType,
    Bar,
    BarAggregation,
    BarSpecification,
    BookType,
    Currency,
    CurrencyPair,
    InstrumentId,
    OmsType,
    OrderSide,
    Price,
    PriceType,
    Quantity,
)
from nautilus_trader.trading import StrategyConfig


class TimingTestStrategy(CryptoniteStrategy):
    """Submits a single market BUY order upon the first bar event."""

    def __init__(self, config: CryptoniteStrategyConfig) -> None:
        super().__init__(config)
        self.submitted = False

    def on_bar(self, bar: Bar) -> None:
        if not self.submitted:
            self.buy_market()
            self.submitted = True


def test_timing_contract_disclosure_present():
    """Verify timing contract disclosure string is explicitly present on CryptoniteStrategy."""
    assert hasattr(CryptoniteStrategy, "EXECUTION_TIMING_DISCLOSURE")
    disclosure = CryptoniteStrategy.EXECUTION_TIMING_DISCLOSURE
    assert "execute against the simulated book state at bar close" in disclosure


def test_bar_execution_settles_at_closing_price_under_zero_latency(tmp_path: Path):
    """Verify order submitted in on_bar(T) fills at Bar(T).close, not Bar(T+1).open."""
    cat_dir = tmp_path / "nautilus_catalog"
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

    bar1_close = 50150.00
    bar2_open = 51000.00

    bars = [
        # Bar 1: Close is 50150.00
        Bar(
            bar_type,
            Price(Decimal("50000.00"), 2),
            Price(Decimal("50200.00"), 2),
            Price(Decimal("49800.00"), 2),
            Price(Decimal(str(bar1_close)), 2),
            Quantity(Decimal("1.0"), 5),
            base_ts,
            base_ts,
        ),
        # Bar 2: Gap up, open is 51000.00
        Bar(
            bar_type,
            Price(Decimal(str(bar2_open)), 2),
            Price(Decimal("51200.00"), 2),
            Price(Decimal("50900.00"), 2),
            Price(Decimal("51100.00"), 2),
            Quantity(Decimal("1.0"), 5),
            base_ts + step_ns,
            base_ts + step_ns,
        ),
    ]
    mgr.write_bars(bars)

    venue_cfg = BacktestVenueConfig(
        name="BINANCE",
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        starting_balances=["100000 USDT"],
        bar_execution=True,
        book_type=BookType.L1_MBP,
        bar_adaptive_high_low_ordering=False,
        fill_model=DefaultFillModel(),
    )

    data_cfg = BacktestDataConfig(
        catalog_path=str(cat_dir),
        data_type="Bar",
        instrument_id=inst_id,
        bar_spec=BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        bar_types=[str(bar_type)],
        start_time=base_ts,
        end_time=base_ts + step_ns,
        optimize_file_loading=True,
    )

    run_cfg = BacktestRunConfig(
        id="test-timing-run",
        engine=BacktestEngineConfig(),
        venues=[venue_cfg],
        data=[data_cfg],
        chunk_size=100000,
        dispose_on_completion=False,
    )

    node = BacktestNode(configs=[run_cfg])
    node.build()

    strat_cfg = CryptoniteStrategyConfig(
        instrument_id=str(inst_id),
        bar_type=str(bar_type),
        trade_size=0.1,
    )
    strategy = TimingTestStrategy(config=strat_cfg)
    node.add_strategy(run_cfg.id, strategy)
    node.run()

    cache = node.get_engine_cache(run_cfg.id)
    orders = cache.orders()
    assert len(orders) == 1
    order = orders[0]

    # Verify execution settled against Bar 1's close (50150.00) and NOT Bar 2's open (51000.00)
    assert float(order.avg_px) == bar1_close
    assert float(order.avg_px) != bar2_open

    node.dispose()


def test_execution_profile_resolution_and_validation():
    """Verify resolution of all valid profiles and rejection of unknown profiles."""
    import pytest
    from cryptonite_backtester.execution_profiles import resolve_execution_profile

    # 1. Optimistic profile
    v_opt, meta_opt = resolve_execution_profile("optimistic")
    assert v_opt.name == "BINANCE"
    assert meta_opt["fill_model"] == "DefaultFillModel"

    # 2. Adverse slippage profile
    v_adv, meta_adv = resolve_execution_profile("adverse_slippage")
    assert meta_adv["fill_model"] == "OneTickSlippageFillModel"

    # 3. Conservative profile
    v_cons, meta_cons = resolve_execution_profile("conservative", random_seed=99)
    assert meta_cons["fill_model"] == "ProbabilisticFillModel"
    assert meta_cons["random_seed"] == 99

    # 4. Unknown profile rejection
    with pytest.raises(ValueError, match="Unknown execution profile 'invalid'"):
        resolve_execution_profile("invalid")

