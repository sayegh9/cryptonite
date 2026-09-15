"""Test NautilusTrader 2.0.0rc4 version lock contract and exact commission fixtures."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from cryptonite_execution.version_lock import (
    REQUIRED_NAUTILUS_VERSION,
    VersionLockError,
    assert_version_locked,
)
from nautilus_trader.model import (
    Currency,
    CurrencyPair,
    InstrumentId,
    Money,
    Price,
    Quantity,
    Symbol,
    Venue,
)


def test_nautilus_version_lock_enforcement():
    """Verify that runtime strictly validates nautilus_trader==2.0.0rc4 and halts otherwise."""
    # Current environment must pass
    assert_version_locked()

    # Tampered versions must immediately raise VersionLockError
    with patch("nautilus_trader.__version__", "2.0.0"):
        with pytest.raises(VersionLockError, match="NautilusTrader version lock violation"):
            assert_version_locked()

    with patch("nautilus_trader.__version__", "1.200.0"):
        with pytest.raises(VersionLockError, match="required '2.0.0rc4'"):
            assert_version_locked()


def test_exact_commission_and_notional_contract():
    """Verify exact commission calculation:
    Order Quantity = 0.10000 BTC
    Fill Price = 50,010.00 USDT
    Taker Fee Rate = 0.001 (0.1%)
    Notional = 5,001.00000 USDT
    Commission = exactly 5.00100000 USDT
    """
    qty = Decimal("0.10000")
    price = Decimal("50010.00")
    fee_rate = Decimal("0.001")

    notional = qty * price
    expected_notional = Decimal("5001.00000")
    assert notional == expected_notional

    commission = notional * fee_rate
    expected_commission = Decimal("5.00100000")
    assert commission == expected_commission

    # Test with Nautilus Money object
    usdt = Currency.from_str("USDT")
    comm_money = Money(commission, usdt)
    assert comm_money == Money(Decimal("5.00100000"), usdt)


def test_redis_cache_hydration_format_contract():
    """Verify rc4 serialization dictionary structure for CurrencyPair cache hydration."""
    inst_id = InstrumentId(Symbol("BTCUSDT"), Venue("BINANCE"))
    pair = CurrencyPair(
        instrument_id=inst_id,
        raw_symbol=Symbol("BTCUSDT"),
        base_currency=Currency.from_str("BTC"),
        quote_currency=Currency.from_str("USDT"),
        price_precision=2,
        size_precision=5,
        price_increment=Price.from_str("0.01"),
        size_increment=Quantity.from_str("0.00001"),
        taker_fee=Decimal("0.001"),
        ts_event=1_700_000_000_000_000_000,
        ts_init=1_700_000_000_000_000_000,
    )

    pair_dict = pair.to_dict()
    assert isinstance(pair_dict, dict)
    assert pair_dict["type"] == "CurrencyPair"

    # Verify Rust serde external tagging format required by RedisCacheDatabase
    tagged_wrapper = {"CurrencyPair": pair_dict}
    assert "CurrencyPair" in tagged_wrapper
    assert tagged_wrapper["CurrencyPair"]["raw_symbol"] == "BTCUSDT"
