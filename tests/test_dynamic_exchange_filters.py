"""Tests for DynamicExchangeFilterValidator."""

from decimal import Decimal
import pytest
from cryptonite_execution.filters import (
    DynamicExchangeFilterValidator,
    ExchangeFilterValidationError,
)

SAMPLE_BTCUSDT_SYMBOL_INFO = {
    "symbol": "BTCUSDT",
    "status": "TRADING",
    "baseAsset": "BTC",
    "quoteAsset": "USDT",
    "filters": [
        {
            "filterType": "PRICE_FILTER",
            "minPrice": "0.01000000",
            "maxPrice": "1000000.00000000",
            "tickSize": "0.01000000",
        },
        {
            "filterType": "LOT_SIZE",
            "minQty": "0.00001000",
            "maxQty": "9000.00000000",
            "stepSize": "0.00001000",
        },
        {
            "filterType": "NOTIONAL",
            "minNotional": "10.00000000",
            "applyMinToMarket": True,
            "maxNotional": "9000000.00000000",
            "applyMaxToMarket": False,
            "avgPriceMins": 5,
        },
        {
            "filterType": "PERCENT_PRICE_BY_SIDE",
            "bidMultiplierUp": "1.2",
            "bidMultiplierDown": "0.8",
            "askMultiplierUp": "1.2",
            "askMultiplierDown": "0.8",
            "avgPriceMins": 5,
        },
    ],
}


def test_validator_accepts_compliant_order():
    """Verify standard valid order passes all exchange filters."""
    validator = DynamicExchangeFilterValidator(SAMPLE_BTCUSDT_SYMBOL_INFO)
    # 0.001 BTC @ 50,000 USDT = $50 notional (> $10 minNotional)
    # price aligns with 0.01 tickSize, qty aligns with 0.00001 stepSize
    validator.validate_all(
        side="BUY",
        price=Decimal("50000.00"),
        quantity=Decimal("0.001"),
        avg_price=Decimal("50100.00"),
    )


def test_validator_rejects_non_trading_status():
    """Verify non-TRADING symbol status is rejected."""
    info = dict(SAMPLE_BTCUSDT_SYMBOL_INFO, status="BREAK")
    validator = DynamicExchangeFilterValidator(info)
    with pytest.raises(ExchangeFilterValidationError) as exc:
        validator.validate_trading_status()
    assert "not trading" in str(exc.value)


def test_validator_rejects_tick_size_and_step_size_violations():
    """Verify misaligned prices and quantities fail alignment checks."""
    validator = DynamicExchangeFilterValidator(SAMPLE_BTCUSDT_SYMBOL_INFO)

    # Price 50000.005 does not align with 0.01 tickSize
    with pytest.raises(ExchangeFilterValidationError) as exc:
        validator.validate_price(Decimal("50000.005"))
    assert "tickSize" in str(exc.value)

    # Quantity 0.000015 does not align with 0.000010 stepSize
    with pytest.raises(ExchangeFilterValidationError) as exc:
        validator.validate_lot_size(Decimal("0.000015"))
    assert "stepSize" in str(exc.value)


def test_validator_rejects_min_notional_and_price_collars():
    """Verify minNotional violations and price collar excursions are rejected."""
    validator = DynamicExchangeFilterValidator(SAMPLE_BTCUSDT_SYMBOL_INFO)

    # 0.0001 BTC @ 50,000 = $5 (< $10 minNotional)
    with pytest.raises(ExchangeFilterValidationError) as exc:
        validator.validate_notional(quantity=Decimal("0.0001"), price=Decimal("50000.00"))
    assert "minNotional" in str(exc.value)

    # BUY price collar: avg_price=50000, max allowed = 50000 * 1.2 = 60000. Price 65000 exceeds collar
    with pytest.raises(ExchangeFilterValidationError) as exc:
        validator.validate_price_collar(
            price=Decimal("65000.00"),
            side="BUY",
            avg_price=Decimal("50000.00"),
        )
    assert "exceeds upper price collar" in str(exc.value)
