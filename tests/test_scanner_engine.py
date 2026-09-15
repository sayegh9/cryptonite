"""Tests for ScannerEngine and candidate generation."""

from decimal import Decimal
from uuid import uuid4

import polars as pl
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.models import (
    RiskConfig,
    RuleCondition,
    RuleGroup,
    RuleOperator,
)
from cryptonite_scanner.resampler import ONE_MINUTE_NS


def test_scanner_engine_candidate_detection():
    """Verify scanner engine detects trigger conditions and outputs SignalCandidate."""
    base_ts = 1_700_000_000 * 1_000_000_000
    n = 250  # Enough rows for EMA200 warmup

    # Generate upward trending price series
    # At the end, create a sharp drop to create an RSI dip below 30
    prices = [100.0 + (i * 0.5) for i in range(n - 10)]
    # Dip for 10 bars
    dip_prices = [prices[-1] - (j * 3.0) for j in range(1, 11)]
    all_prices = prices + dip_prices

    open_times = [base_ts + (i * ONE_MINUTE_NS) for i in range(len(all_prices))]
    highs = [p + 1.0 for p in all_prices]
    lows = [p - 1.0 for p in all_prices]
    closes = all_prices
    volumes = [100.0] * len(all_prices)
    # Last bar has volume spike
    volumes[-1] = 500.0

    df = pl.DataFrame({
        "bar_open_ns": open_times,
        "bar_close_inclusive_ns": [t + ONE_MINUTE_NS - 1_000_000 for t in open_times],
        "interval_end_exclusive_ns": [t + ONE_MINUTE_NS for t in open_times],
        "open": all_prices,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "quote_volume": [v * p for v, p in zip(volumes, closes, strict=True)],
        "trades_count": [10] * len(all_prices),
    })

    # Rule: RSI < 35 AND volume_ratio >= 1.5
    rules = [
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(field="rsi", operator=RuleOperator.LT, value=35.0),
            ],
        ),
        RuleGroup(
            category="liquidity",
            logic="AND",
            conditions=[
                RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=1.5),
            ],
        ),
    ]

    candidates = ScannerEngine.evaluate(
        df=df,
        rule_groups=rules,
        risk_config=RiskConfig(stop_loss_atr_mult=2.0, take_profit_atr_mult=4.0),
        instrument_id="BTCUSDT.BINANCE",
        timeframe="1m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        price_precision=2,
    )

    assert len(candidates) >= 1
    c = candidates[-1]
    assert c.instrument_id == "BTCUSDT.BINANCE"
    assert c.timeframe == "1m"
    assert isinstance(c.suggested_entry, Decimal)
    assert isinstance(c.suggested_stop, Decimal)
    assert isinstance(c.suggested_target, Decimal)
    assert c.suggested_stop < c.suggested_entry < c.suggested_target
    assert len(c.matched_rules) >= 2
    assert any("rsi" in m.lower() for m in c.matched_rules)
    assert any("volume_ratio" in m.lower() for m in c.matched_rules)
    assert "rsi" in c.metrics
    assert "atr" in c.metrics
