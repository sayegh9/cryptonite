"""Tests for Nautilus ParquetDataCatalog integration and exact 2.0.0rc4 APIs."""

from cryptonite_schemas.bar import bar_to_nautilus
from cryptonite_schemas.identifiers import to_nautilus_bar_type
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from nautilus_trader.model import Bar


def test_nautilus_catalog_lifecycle(temp_nautilus_dir, sample_currency_pair, sample_enriched_bars):
    mgr = NautilusCatalogManager(temp_nautilus_dir)

    # 1. Invariant: Write instrument definition first!
    files = mgr.write_instruments([sample_currency_pair])
    assert len(files) > 0

    insts = mgr.instruments()
    assert len(insts) == 1
    assert str(insts[0].id) == "BTCUSDT.BINANCE"
    assert insts[0].price_precision == 2
    assert insts[0].size_precision == 5

    # 2. Write bars using exact API
    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    bar_identifier = str(bar_type)

    n_bars = [bar_to_nautilus(b, bar_type) for b in sample_enriched_bars]
    bar_files, checksum = mgr.write_bars(n_bars)
    assert len(bar_files) > 0
    assert len(checksum) == 64

    # 3. Query bars using exact API
    read_bars = mgr.query_bars(identifiers=[bar_identifier])
    assert len(read_bars) == 3
    assert read_bars[0].bar_type == bar_type
    assert float(read_bars[0].open) == 54000.0

    # 4. Get intervals using data_cls=Bar and identifier=bar_identifier
    intervals = mgr.get_intervals(data_cls=Bar, identifier=bar_identifier)
    assert len(intervals) >= 1
    start_ns, end_ns = intervals[0]
    assert start_ns <= end_ns

    # 5. Get missing intervals for request
    # Request range after existing data -> should report missing gap
    future_start = end_ns + 10_000_000_000
    future_end = future_start + 60_000_000_000
    missing = mgr.get_missing_intervals_for_request(
        start=future_start,
        end=future_end,
        data_cls=Bar,
        identifier=bar_identifier,
    )
    assert len(missing) == 1
    assert missing[0] == (future_start, future_end)
