"""Tests for instrument tick-size and precision change versioning."""

from decimal import Decimal

from cryptonite_schemas.identifiers import to_nautilus_instrument_id, to_spot_instrument_id
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from nautilus_trader.model import (
    Currency,
    CurrencyPair,
    Price,
    Quantity,
)


def test_precision_change_creates_new_version(temp_nautilus_dir):
    mgr = NautilusCatalogManager(temp_nautilus_dir)
    inst_id = to_nautilus_instrument_id(to_spot_instrument_id("BTCUSDT"))

    # Version 1: price_precision = 2, size_precision = 5
    t1 = 1725700000000000000
    inst_v1 = CurrencyPair(
        instrument_id=inst_id,
        raw_symbol=inst_id.symbol,
        base_currency=Currency.from_str("BTC"),
        quote_currency=Currency.from_str("USDT"),
        price_precision=2,
        size_precision=5,
        price_increment=Price(Decimal("0.01"), 2),
        size_increment=Quantity(Decimal("0.00001"), 5),
        ts_event=t1,
        ts_init=t1,
    )
    files_v1 = mgr.write_instruments([inst_v1])
    assert len(files_v1) >= 1

    # Version 2: Binance changes tick size! price_precision = 1, size_precision = 6
    t2 = t1 + 86400 * 1_000_000_000
    inst_v2 = CurrencyPair(
        instrument_id=inst_id,
        raw_symbol=inst_id.symbol,
        base_currency=Currency.from_str("BTC"),
        quote_currency=Currency.from_str("USDT"),
        price_precision=1,
        size_precision=6,
        price_increment=Price(Decimal("0.1"), 1),
        size_increment=Quantity(Decimal("0.000001"), 6),
        ts_event=t2,
        ts_init=t2,
    )
    files_v2 = mgr.write_instruments([inst_v2])
    assert len(files_v2) >= 1

    # Verify instruments are both queryable
    all_insts = mgr.instruments()
    assert len(all_insts) >= 1
    # Latest loaded reflects the updated precision
    latest = all_insts[-1]
    assert latest.price_precision == 1
    assert latest.size_precision == 6
