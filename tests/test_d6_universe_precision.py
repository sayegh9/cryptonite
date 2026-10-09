"""D6: Universe-Aware Price and Size Precision Unit & Regression Test Suite.

Verifies:
1. ScannerEngine.evaluate fails closed (returns []) when price_precision is missing/None/invalid.
2. BTCUSDT / ETHUSDT regression: price_precision=2 retains 2 decimal places.
3. Sub-$0.01 asset (SHIBUSDT, price_precision=8) and low-priced asset (WLDUSDT, price_precision=4) retain exact precision in candidates and explanations without price destruction.
4. ScannerWorker._load_instrument_precisions fails closed when metadata is absent.
"""

from decimal import Decimal
from uuid import uuid4
import polars as pl
import pytest
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.models import (
    RiskConfig,
    RuleCondition,
    RuleGroup,
    RuleOperator,
)
from cryptonite_scanner_service.worker import ScannerWorker


@pytest.fixture
def sample_rule_groups():
    return [
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(
                    field="rsi",
                    operator=RuleOperator.LT,
                    value=30.0,
                )
            ],
        )
    ]


@pytest.fixture
def risk_config():
    return RiskConfig(
        direction="LONG",
        stop_mode="structure",
        min_atr_mult=0.5,
        max_atr_mult=4.0,
        fallback_stop_atr_mult=2.0,
        fallback_target_atr_mult=3.0,
    )


def make_dummy_dataframe(close_price: float, atr_val: float):
    return pl.DataFrame(
        {
            "bar_open_ns": [1700000000000000000],
            "bar_close_inclusive_ns": [1700000059999999999],
            "interval_end_exclusive_ns": [1700000060000000000],
            "open": [close_price],
            "high": [close_price + atr_val],
            "low": [close_price - atr_val],
            "close": [close_price],
            "volume": [1000.0],
            "rsi": [25.0],
            "ema_20": [close_price],
            "ema_50": [close_price],
            "ema_200": [close_price],
            "bb_lower": [close_price - 2 * atr_val],
            "bb_middle": [close_price],
            "bb_upper": [close_price + 2 * atr_val],
            "atr": [atr_val],
            "volume_ratio": [1.5],
            "donchian_lower": [close_price - atr_val],
            "donchian_middle": [close_price],
            "donchian_upper": [close_price + atr_val],
        }
    )


def test_evaluate_fails_closed_on_missing_or_invalid_precision(sample_rule_groups, risk_config):
    df = make_dummy_dataframe(50000.0, 500.0)
    spec_id, run_id = uuid4(), uuid4()

    # None price_precision -> fails closed
    candidates = ScannerEngine.evaluate(
        df=df,
        rule_groups=sample_rule_groups,
        risk_config=risk_config,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="1h",
        spec_version_id=spec_id,
        scan_run_id=run_id,
        price_precision=None,
    )
    assert candidates == []

    # Invalid negative price_precision -> fails closed
    candidates_neg = ScannerEngine.evaluate(
        df=df,
        rule_groups=sample_rule_groups,
        risk_config=risk_config,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="1h",
        spec_version_id=spec_id,
        scan_run_id=run_id,
        price_precision=-1,
    )
    assert candidates_neg == []


def test_btcusdt_regression_precision_2(sample_rule_groups, risk_config):
    df = make_dummy_dataframe(65432.1987, 123.456)
    candidates = ScannerEngine.evaluate(
        df=df,
        rule_groups=sample_rule_groups,
        risk_config=risk_config,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="1h",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        price_precision=2,
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.suggested_entry == Decimal("65432.20")
    assert c.metrics["price"] == 65432.20
    assert c.metrics["stop_distance"] == float(round(Decimal("65432.20") - c.suggested_stop, 2))


def test_sub_cent_symbol_shibusdt_precision_8(risk_config):
    # SHIBUSDT price $0.00001742
    close_val = 0.00001742
    atr_val = 0.00000100
    df = make_dummy_dataframe(close_val, atr_val)

    rule_groups = [
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(
                    field="close",
                    operator=RuleOperator.LT,
                    value=0.0001,
                )
            ],
        )
    ]

    candidates = ScannerEngine.evaluate(
        df=df,
        rule_groups=rule_groups,
        risk_config=risk_config,
        instrument_id="SHIBUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        price_precision=8,
    )
    assert len(candidates) == 1
    c = candidates[0]
    # Verify sub-cent entry price precision is NOT rounded to 0.00
    assert c.suggested_entry == Decimal("0.00001742")
    assert c.metrics["price"] == 0.00001742
    assert "0.00001742" in c.matched_rules[0]


def test_low_priced_symbol_wldusdt_precision_4(sample_rule_groups, risk_config):
    # WLDUSDT price $1.5432
    close_val = 1.5432
    atr_val = 0.0543
    df = make_dummy_dataframe(close_val, atr_val)

    candidates = ScannerEngine.evaluate(
        df=df,
        rule_groups=sample_rule_groups,
        risk_config=risk_config,
        instrument_id="WLDUSDT.BINANCE",
        timeframe="1h",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        price_precision=4,
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.suggested_entry == Decimal("1.5432")
    assert c.metrics["price"] == 1.5432


@pytest.mark.asyncio
async def test_worker_load_instrument_precisions_fails_closed_when_pool_none():
    worker = ScannerWorker(database_url="postgresql://invalid", redis_url="redis://invalid")
    worker.pool = None
    precisions = await worker._load_instrument_precisions(["UNKNOWNUSDT.BINANCE"])
    assert precisions == {}
