"""Tests for conflicting duplicate bar detection and quarantine handling."""

from decimal import Decimal

import pytest
from cryptonite_schemas.bar import EnrichedBar
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.coordinator import TwoStoreCoordinator
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager


@pytest.mark.asyncio
async def test_conflicting_bar_quarantine(temp_nautilus_dir, temp_analytics_dir, sample_enriched_bars):
    nautilus_mgr = NautilusCatalogManager(temp_nautilus_dir)
    analytics_writer = AnalyticsLakeWriter(temp_analytics_dir)
    coordinator = TwoStoreCoordinator(
        nautilus_manager=nautilus_mgr,
        analytics_writer=analytics_writer,
        pg_pool=None,
    )

    base_bar = sample_enriched_bars[0]

    # Create a conflicting bar with same timestamp but different close price
    conflicting_bar = EnrichedBar(
        instrument_id=base_bar.instrument_id,
        bar_type=base_bar.bar_type,
        schema_version=1,
        source="BINANCE_SPOT_KLINE",
        payload_checksum="conflict_hash",
        ingest_batch_id="batch_conflict",
        ts_event_ns=base_bar.ts_event_ns,
        ts_init_ns=base_bar.ts_init_ns,
        received_at_ns=base_bar.received_at_ns + 1000,
        price_precision=base_bar.price_precision,
        size_precision=base_bar.size_precision,
        open=base_bar.open,
        high=base_bar.high,
        low=base_bar.low,
        close=base_bar.close + Decimal("500.00"),  # Conflict!
        volume=base_bar.volume,
        quote_volume=base_bar.quote_volume,
        taker_buy_base_volume=base_bar.taker_buy_base_volume,
        taker_buy_quote_volume=base_bar.taker_buy_quote_volume,
        trades_count=base_bar.trades_count,
        open_raw=base_bar.open_raw,
        high_raw=base_bar.high_raw,
        low_raw=base_bar.low_raw,
        close_raw="54550.00",
        volume_raw=base_bar.volume_raw,
    )

    # Calling quarantine should complete without throwing unhandled exceptions
    await coordinator.quarantine_conflicting_bar(
        bar=conflicting_bar,
        existing_data=base_bar.model_dump(mode="json"),
        reason="Conflicting close price for identical timestamp",
    )
