"""Tests for Analytics Lake date-partitioned storage and DuckDB analytical queries."""

from pathlib import Path

from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.queries import AnalyticsQueryEngine


def test_analytics_lake_write_and_duckdb_query(temp_analytics_dir, sample_enriched_bars):
    writer = AnalyticsLakeWriter(temp_analytics_dir)

    target_file, checksum = writer.write_batch(
        bars=sample_enriched_bars,
        venue="binance",
        market_type="spot",
        timeframe="1m",
        symbol="BTCUSDT",
        batch_id="test-batch-1234",
    )

    p = Path(target_file)
    assert p.exists()
    assert "date=" in str(p.parent)
    assert p.name == "part-test-batch-1234.parquet"
    assert len(checksum) == 64

    # Query with DuckDB
    engine = AnalyticsQueryEngine(temp_analytics_dir)
    count = engine.count_bars("BTCUSDT", "1m")
    assert count == 3

    min_ts, max_ts = engine.get_timestamp_range("BTCUSDT", "1m")
    assert min_ts is not None and max_ts is not None
    assert min_ts < max_ts

    df = engine.query_bars_df("BTCUSDT", "1m")
    assert len(df) == 3
    assert "open" in df.columns
    assert "payload_checksum" in df.columns
    assert df["open_raw"][0] == "54000.00"
