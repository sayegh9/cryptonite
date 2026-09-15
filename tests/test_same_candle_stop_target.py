"""Test suite verifying deterministic intrabar execution path (Open -> High -> Low -> Close).

When simulated bars are processed with bar_execution=True and bar_adaptive_high_low_ordering=False,
Nautilus Trader enforces a deterministic intrabar traversal order:
Open -> High -> Low -> Close.

This test verifies that when both a High trigger (take-profit limit) and a Low trigger (stop-loss)
are active on the book within the same bar, the High level matches first.
"""

from decimal import Decimal
from pathlib import Path

from cryptonite_schemas.identifiers import (
    to_nautilus_bar_type,
    to_nautilus_instrument_id,
    to_spot_instrument_id,
)
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from nautilus_trader.backtest import (
    BacktestDataConfig,
    BacktestEngineConfig,
    BacktestNode,
    BacktestRunConfig,
    BacktestVenueConfig,
)
from nautilus_trader.execution import DefaultFillModel, MakerTakerFeeModel
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
from nautilus_trader.trading import Strategy, StrategyConfig


class SameCandleTestStrategyConfig(StrategyConfig):
    def __init__(self, instrument_id: str, bar_type: str, **kwargs):
        super().__init__(**kwargs)
        self.instrument_id = instrument_id
        self.bar_type = bar_type


class SameCandleTestStrategy(Strategy):
    """Submits a long market order, then sets TP above open and SL below open."""

    def __init__(self, config: SameCandleTestStrategyConfig):
        super().__init__(config)
        self.inst_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
        self.step = 0
        self.fill_events = []
        self.tp_order = None
        self.sl_order = None

    def on_start(self):
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar):
        self.step += 1
        if self.step == 1:
            # Enter Long 0.5 BTC
            order = self.order_factory.market(
                instrument_id=self.inst_id,
                order_side=OrderSide.BUY,
                quantity=Quantity(Decimal("0.5"), 5),
            )
            self.submit_order(order)
        elif self.step == 2:
            # Place TP limit order at 50500
            self.tp_order = self.order_factory.limit(
                instrument_id=self.inst_id,
                order_side=OrderSide.SELL,
                quantity=Quantity(Decimal("0.5"), 5),
                price=Price(Decimal("50500.00"), 2),
            )
            self.submit_order(self.tp_order)
            # Place SL stop order at 49500
            self.sl_order = self.order_factory.stop_market(
                instrument_id=self.inst_id,
                order_side=OrderSide.SELL,
                quantity=Quantity(Decimal("0.5"), 5),
                trigger_price=Price(Decimal("49500.00"), 2),
            )
            self.submit_order(self.sl_order)

    def on_order_filled(self, event):
        self.fill_events.append(event)
        if self.tp_order and event.client_order_id == self.tp_order.client_order_id:
            if self.sl_order:
                self.cancel_order(self.sl_order)
        elif self.sl_order and event.client_order_id == self.sl_order.client_order_id:
            if self.tp_order:
                self.cancel_order(self.tp_order)


def test_deterministic_intrabar_traversal_open_high_low_close(tmp_path: Path):
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

    bars = [
        # Bar 1: Entry at 50000
        Bar(
            bar_type,
            Price(Decimal("50000.00"), 2),
            Price(Decimal("50050.00"), 2),
            Price(Decimal("49950.00"), 2),
            Price(Decimal("50000.00"), 2),
            Quantity(Decimal("1.0"), 5),
            base_ts,
            base_ts,
        ),
        # Bar 2: Resting TP and SL orders submitted
        Bar(
            bar_type,
            Price(Decimal("50000.00"), 2),
            Price(Decimal("50050.00"), 2),
            Price(Decimal("49950.00"), 2),
            Price(Decimal("50000.00"), 2),
            Quantity(Decimal("1.0"), 5),
            base_ts + step_ns,
            base_ts + step_ns,
        ),
        # Bar 3: Wide candle where both TP (50500) and SL (49500) are breached!
        # Open=50000, High=50600, Low=49400, Close=50000
        Bar(
            bar_type,
            Price(Decimal("50000.00"), 2),
            Price(Decimal("50600.00"), 2),
            Price(Decimal("49400.00"), 2),
            Price(Decimal("50000.00"), 2),
            Quantity(Decimal("1.0"), 5),
            base_ts + 2 * step_ns,
            base_ts + 2 * step_ns,
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
        bar_adaptive_high_low_ordering=False,  # Enforces Open -> High -> Low -> Close
        fill_model=DefaultFillModel(),
        fee_model=MakerTakerFeeModel(),
    )

    data_cfg = BacktestDataConfig(
        catalog_path=str(cat_dir),
        data_type="Bar",
        instrument_id=inst_id,
        bar_spec=BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST),
        bar_types=[str(bar_type)],
        start_time=base_ts,
        end_time=base_ts + 2 * step_ns,
        optimize_file_loading=True,
    )

    run_cfg = BacktestRunConfig(
        id="test-same-candle-run",
        engine=BacktestEngineConfig(),
        venues=[venue_cfg],
        data=[data_cfg],
        chunk_size=100000,
        dispose_on_completion=False,
    )

    node = BacktestNode(configs=[run_cfg])
    node.build()
    strat_cfg = SameCandleTestStrategyConfig(
        instrument_id=str(inst_id),
        bar_type=str(bar_type),
    )
    strategy = SameCandleTestStrategy(strat_cfg)
    node.add_strategy(run_cfg.id, strategy)
    node.run()

    # Find the exit fills (SELL orders)
    exit_fills = [f for f in strategy.fill_events if f.order_side == OrderSide.SELL]
    assert len(exit_fills) >= 1

    # First exit fill was at the High price (50500.00), demonstrating Open -> High -> Low -> Close ordering
    first_exit = exit_fills[0]
    assert float(first_exit.last_px) == 50500.00

    node.dispose()
