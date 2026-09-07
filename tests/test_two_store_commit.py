"""Tests for Two-Store Commit Coordinator, serialization, and durability."""

import asyncio

import pytest
from cryptonite_schemas.identifiers import to_nautilus_bar_type
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.coordinator import TwoStoreCoordinator
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from cryptonite_storage.queries import AnalyticsQueryEngine


@pytest.mark.asyncio
async def test_two_store_commit_lifecycle(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair, sample_enriched_bars
):
    nautilus_mgr = NautilusCatalogManager(temp_nautilus_dir)
    nautilus_mgr.write_instruments([sample_currency_pair])

    analytics_writer = AnalyticsLakeWriter(temp_analytics_dir)
    coordinator = TwoStoreCoordinator(
        nautilus_manager=nautilus_mgr,
        analytics_writer=analytics_writer,
        pg_pool=None,  # offline unit test mode
    )

    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    batch_id = await coordinator.commit_batch(
        bars=sample_enriched_bars,
        bar_type=bar_type,
        venue="BINANCE",
        market_type="SPOT",
        symbol="BTCUSDT",
        timeframe="1m",
    )
    assert batch_id != ""

    # Verify data in Nautilus Catalog
    n_bars = nautilus_mgr.query_bars(identifiers=[str(bar_type)])
    assert len(n_bars) == 3

    # Verify data in Analytics Lake
    query_engine = AnalyticsQueryEngine(temp_analytics_dir)
    count = query_engine.count_bars("BTCUSDT", "1m")
    assert count == 3


@pytest.mark.asyncio
async def test_single_writer_serialization(
    temp_nautilus_dir, temp_analytics_dir, sample_currency_pair, sample_enriched_bars
):
    nautilus_mgr = NautilusCatalogManager(temp_nautilus_dir)
    nautilus_mgr.write_instruments([sample_currency_pair])

    analytics_writer = AnalyticsLakeWriter(temp_analytics_dir)
    coordinator = TwoStoreCoordinator(
        nautilus_manager=nautilus_mgr,
        analytics_writer=analytics_writer,
        pg_pool=None,
    )

    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")

    # Launch two concurrent commit tasks
    task1 = asyncio.create_task(
        coordinator.commit_batch(
            bars=sample_enriched_bars[:2],
            bar_type=bar_type,
            symbol="BTCUSDT",
        )
    )
    task2 = asyncio.create_task(
        coordinator.commit_batch(
            bars=sample_enriched_bars[2:],
            bar_type=bar_type,
            symbol="BTCUSDT",
        )
    )

    batch1, batch2 = await asyncio.gather(task1, task2)
    assert batch1 != batch2
