"""Test Gate-Owned Deduplicating Bar Router and indicator rewarm idempotency."""

from decimal import Decimal

import pytest
from cryptonite_execution.router import GateOwnedBarRouter
from nautilus_trader.model import Bar, BarType, Price, Quantity


class IndicatorTrackingStrategy:
    """Mock strategy tracking an Exponential Moving Average (EMA) and bar count."""

    def __init__(self, alpha: float = 0.2) -> None:
        self.alpha = alpha
        self.ema: float | None = None
        self.bars_processed: list[Bar] = []

    def on_bar(self, bar: Bar) -> None:
        close_f = float(bar.close)
        if self.ema is None:
            self.ema = close_f
        else:
            self.ema = self.alpha * close_f + (1 - self.alpha) * self.ema
        self.bars_processed.append(bar)


def make_test_bar(bar_type: BarType, ts_event: int, close_val: str) -> Bar:
    p = Price.from_str(close_val)
    q = Quantity.from_str("1.0")
    return Bar(
        bar_type=bar_type,
        open=p,
        high=p,
        low=p,
        close=p,
        volume=q,
        ts_event=ts_event,
        ts_init=ts_event,
    )


def test_gate_owned_bar_router_rewarm_and_indicator_parity():
    """Verify that bars arriving during rewarm pass through the router exactly once,
    guaranteeing that final strategy indicator state matches a single-reference replay.
    """
    bar_type = BarType.from_str("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL")
    router = GateOwnedBarRouter()
    strategy = IndicatorTrackingStrategy(alpha=0.2)

    # 1. Generate 10 consecutive historical bars (minutes 1..10)
    base_ts = 1_700_000_000_000_000_000
    step_ns = 60 * 1_000_000_000

    historical_bars = [
        make_test_bar(bar_type, base_ts + i * step_ns, f"{50000 + i * 10}.00")
        for i in range(1, 11)
    ]
    watermark_c = historical_bars[-1].ts_event  # bar 10 is watermark C

    # 2. Build reference strategy run with single-pass replay through bar 12
    reference_bars = list(historical_bars)
    live_bar_11 = make_test_bar(bar_type, base_ts + 11 * step_ns, "50110.00")
    live_bar_12 = make_test_bar(bar_type, base_ts + 12 * step_ns, "50120.00")
    reference_bars.extend([live_bar_11, live_bar_12])

    ref_strategy = IndicatorTrackingStrategy(alpha=0.2)
    for b in reference_bars:
        ref_strategy.on_bar(b)

    # 3. Simulate rewarm:
    # Router routes historical bars 1..10
    for b in historical_bars:
        routed = router.route_bar(b, [strategy])
        assert routed is True

    # 4. Simulate live bars that arrived during rewarm:
    # Includes duplicate bar 9, duplicate bar 10, and new live bars 11 & 12
    injected_during_rewarm = [
        historical_bars[8],   # bar 9 (duplicate)
        historical_bars[9],   # bar 10 (duplicate)
        live_bar_11,          # bar 11 (new > C)
        live_bar_12,          # bar 12 (new > C)
    ]

    for b in injected_during_rewarm:
        router.route_bar(b, [strategy])

    # 5. Assert deduplication guarantees
    assert router.deduped_count == 2
    assert router.routed_count == 12  # exactly 12 unique bars routed
    assert len(strategy.bars_processed) == 12
    assert router.max_processed_bar_ns == live_bar_12.ts_event

    # 6. Assert exact indicator parity between router-managed run and single reference replay
    assert strategy.ema is not None
    assert ref_strategy.ema is not None
    assert pytest.approx(strategy.ema, abs=1e-8) == ref_strategy.ema
