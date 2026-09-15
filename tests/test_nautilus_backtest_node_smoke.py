"""CI Gate Smoke Test for NautilusTrader 2.0.0rc4 BacktestNode workflow.

Validates:
1. ProbabilisticFillModel constructor with limit fill probability and random seed.
2. BacktestDataConfig with bar_spec, bar_types, start_time, end_time, and optimize_file_loading.
3. BacktestNode build -> node.add_strategy(run_config_id, strategy) -> run() against a catalog fixture.
"""

from cryptonite_schemas.bar import bar_to_nautilus
from cryptonite_schemas.identifiers import to_nautilus_bar_type
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from nautilus_trader.backtest import (
    BacktestDataConfig,
    BacktestEngineConfig,
    BacktestNode,
    BacktestRunConfig,
    BacktestVenueConfig,
)
from nautilus_trader.execution import MakerTakerFeeModel, ProbabilisticFillModel
from nautilus_trader.model import (
    AccountType,
    BarAggregation,
    BarSpecification,
    BarType,
    BookType,
    InstrumentId,
    OmsType,
    PriceType,
)
from nautilus_trader.trading import Strategy, StrategyConfig


class SmokeTestStrategyConfig(StrategyConfig):
    def __init__(self, bar_type: BarType, **kwargs) -> None:
        super().__init__(**kwargs)
        self.bar_type = bar_type


class SmokeTestStrategy(Strategy):
    """Simple test strategy that tracks bar events."""

    def __init__(self, config: SmokeTestStrategyConfig) -> None:
        super().__init__(config)
        self.bar_type = config.bar_type
        self.bars_received = 0

    def on_start(self) -> None:
        self.subscribe_bars(self.bar_type)
        self.log.info("SmokeTestStrategy started and subscribed to bars")

    def on_bar(self, bar) -> None:
        self.bars_received += 1
        self.log.info(f"Received bar: {bar}")


def test_nautilus_backtest_node_exact_apis_smoke(
    temp_nautilus_dir, sample_currency_pair, sample_enriched_bars
):
    # 1. Setup Nautilus catalog fixture
    mgr = NautilusCatalogManager(temp_nautilus_dir)
    mgr.write_instruments([sample_currency_pair])

    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    assert str(bar_type).endswith("LAST-EXTERNAL")

    n_bars = [bar_to_nautilus(b, bar_type) for b in sample_enriched_bars]
    mgr.write_bars(n_bars)

    start_ns = n_bars[0].ts_event
    end_ns = n_bars[-1].ts_event

    # 2. Validate ProbabilisticFillModel constructor
    fill_model = ProbabilisticFillModel(
        prob_fill_on_limit=0.8,
        prob_slippage=0.5,
        random_seed=42,
    )
    assert fill_model is not None

    venue_cfg = BacktestVenueConfig(
        name="BINANCE",
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        starting_balances=["10000 USDT"],
        bar_execution=True,
        book_type=BookType.L1_MBP,
        bar_adaptive_high_low_ordering=False,
        fill_model=fill_model,
        fee_model=MakerTakerFeeModel(),
    )

    # 3. Validate BacktestDataConfig with exact bar filter & time bounds
    inst_id = InstrumentId.from_str("BTCUSDT.BINANCE")
    bar_spec = BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST)
    bar_types_str = [str(bar_type)]

    data_cfg = BacktestDataConfig(
        catalog_path=str(temp_nautilus_dir),
        data_type="Bar",
        instrument_id=inst_id,
        bar_spec=bar_spec,
        bar_types=bar_types_str,
        start_time=start_ns,
        end_time=end_ns,
        optimize_file_loading=True,
    )

    run_cfg = BacktestRunConfig(
        id="smoke-test-run-001",
        engine=BacktestEngineConfig(),
        venues=[venue_cfg],
        data=[data_cfg],
        chunk_size=100_000,
        dispose_on_completion=True,
    )

    # 4. Validate BacktestNode workflow
    node = BacktestNode(configs=[run_cfg])
    node.build()

    strat_cfg = SmokeTestStrategyConfig(bar_type=bar_type)
    strategy = SmokeTestStrategy(config=strat_cfg)
    # Exact rc4 run-specific strategy attachment:
    node.add_strategy(run_cfg.id, strategy)

    results = node.run()
    assert len(results) == 1
    assert results[0].run_config_id == "smoke-test-run-001"
    assert strategy.bars_received == len(n_bars)
    print("\nBacktestNode exact APIs smoke test PASSED with", strategy.bars_received, "bars processed!")
