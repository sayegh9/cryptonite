"""Tests for domain schemas, symbology, and exact decimal Arrow representations."""

from decimal import Decimal

import pyarrow as pa
from cryptonite_schemas.bar import (
    ANALYTICS_PYARROW_SCHEMA,
    bar_to_nautilus,
    bars_to_arrow_table,
    compute_payload_checksum,
)
from cryptonite_schemas.identifiers import (
    parse_instrument_id,
    to_nautilus_bar_type,
    to_nautilus_instrument_id,
    to_perp_instrument_id,
    to_spot_instrument_id,
)


def test_spot_and_perp_symbology():
    spot_id = to_spot_instrument_id("BTCUSDT")
    assert spot_id == "BTCUSDT.BINANCE"

    perp_id = to_perp_instrument_id("BTCUSDT")
    assert perp_id == "BTCUSDT-PERP.BINANCE"

    sym, venue, is_perp = parse_instrument_id(spot_id)
    assert sym == "BTCUSDT"
    assert venue == "BINANCE"
    assert not is_perp

    sym_p, venue_p, is_perp_p = parse_instrument_id(perp_id)
    assert sym_p == "BTCUSDT"
    assert venue_p == "BINANCE"
    assert is_perp_p


def test_nautilus_identifier_conversion():
    inst_id = to_nautilus_instrument_id("BTCUSDT.BINANCE")
    assert str(inst_id) == "BTCUSDT.BINANCE"
    assert str(inst_id.symbol) == "BTCUSDT"
    assert str(inst_id.venue) == "BINANCE"

    bar_type_1m = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    assert str(bar_type_1m) == "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL"

    bar_type_1h = to_nautilus_bar_type("BTCUSDT.BINANCE", "1h")
    assert str(bar_type_1h) == "BTCUSDT.BINANCE-1-HOUR-LAST-EXTERNAL"


def test_enriched_bar_to_arrow_exact_decimals(sample_enriched_bars):
    table = bars_to_arrow_table(sample_enriched_bars)
    assert isinstance(table, pa.Table)
    assert len(table) == 3
    assert table.schema == ANALYTICS_PYARROW_SCHEMA

    # Validate exact decimal type: decimal128(38, 18)
    open_field = table.schema.field("open")
    assert open_field.type == pa.decimal128(38, 18)

    # Validate exact values retained
    p_open = table.column("open")[0].as_py()
    assert isinstance(p_open, Decimal)
    assert p_open == Decimal("54000.00")

    vol = table.column("volume")[0].as_py()
    assert vol == Decimal("12.345")


def test_bar_to_nautilus_conversion(sample_enriched_bars):
    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    n_bar = bar_to_nautilus(sample_enriched_bars[0], bar_type)
    assert n_bar.bar_type == bar_type
    assert float(n_bar.open) == 54000.0
    assert float(n_bar.high) == 54100.0
    assert float(n_bar.low) == 53950.0
    assert float(n_bar.close) == 54050.0
    assert float(n_bar.volume) == 12.345


def test_deterministic_payload_checksum(sample_enriched_bars):
    cs1 = compute_payload_checksum(sample_enriched_bars)
    cs2 = compute_payload_checksum(sample_enriched_bars)
    assert cs1 == cs2
    assert len(cs1) == 64
