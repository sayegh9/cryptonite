"""Main Ingestion Recorder Orchestrator."""

import asyncio
import logging
import time
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_schemas.identifiers import (
    to_nautilus_bar_type,
    to_nautilus_instrument_id,
    to_spot_instrument_id,
)
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.coordinator import TwoStoreCoordinator
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from nautilus_trader.model import (
    Currency,
    CurrencyPair,
    Price,
    Quantity,
)

from cryptonite_ingest.client import BinanceRestClient
from cryptonite_ingest.recovery import GapRecoveryManager
from cryptonite_ingest.stream import BinanceStreamClient

logger = logging.getLogger("cryptonite.recorder")


class IngestRecorder:
    """Orchestrates market data ingestion, recovery, and dual-store commits."""

    def __init__(
        self,
        symbols: list[str],
        timeframes: list[str],
        nautilus_catalog_path: str,
        analytics_lake_path: str,
        database_url: str | None = None,
        redis_url: str | None = None,
    ) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.timeframes = [t.lower() for t in timeframes]
        self.nautilus_catalog_path = nautilus_catalog_path
        self.analytics_lake_path = analytics_lake_path
        self.database_url = database_url
        self.redis_url = redis_url

        self.nautilus_manager = NautilusCatalogManager(self.nautilus_catalog_path)
        self.analytics_writer = AnalyticsLakeWriter(self.analytics_lake_path)
        self.rest_client = BinanceRestClient()
        self.pool: asyncpg.Pool | None = None
        self.redis: aioredis.Redis | None = None
        self.coordinator: TwoStoreCoordinator | None = None
        self.recovery_manager: GapRecoveryManager | None = None
        self.stream_client: BinanceStreamClient | None = None

        self._running = False
        self._symbol_metadata: dict[str, dict[str, Any]] = {}
        self.last_closed_bar_ts: dict[str, int] = {}

    async def initialize(self) -> None:
        """Connect database and initialize coordinator with process lock."""
        if self.database_url:
            self.pool = await asyncpg.create_pool(self.database_url, min_size=2, max_size=10)
        
        if self.redis_url:
            self.redis = aioredis.from_url(self.redis_url, decode_responses=True)

        self.coordinator = TwoStoreCoordinator(
            nautilus_manager=self.nautilus_manager,
            analytics_writer=self.analytics_writer,
            pg_pool=self.pool,
        )

        # Acquire process-exclusive lock
        await self.coordinator.acquire_process_lock()

        self.recovery_manager = GapRecoveryManager(
            rest_client=self.rest_client,
            coordinator=self.coordinator,
            nautilus_manager=self.nautilus_manager,
            pg_pool=self.pool,
        )

        # Sync symbol metadata and write instruments to Nautilus catalog
        await self.sync_instruments()

        # Reconcile any uncommitted batches from previous crashes
        for symbol in self.symbols:
            for tf in self.timeframes:
                inst_id = to_spot_instrument_id(symbol)
                bar_type = to_nautilus_bar_type(inst_id, tf)
                await self.coordinator.reconcile_pending_batches(bar_type)

    async def sync_instruments(self) -> None:
        """Fetch instrument definitions, write to Nautilus catalog and PostgreSQL."""
        logger.info("Syncing instrument definitions from Binance...")
        all_info = await self.rest_client.get_exchange_info()
        info_map = {item["symbol"]: item for item in all_info}

        instruments_to_write = []
        for sym in self.symbols:
            meta = info_map.get(sym)
            if not meta:
                logger.warning("Symbol %s not found in Binance exchangeInfo", sym)
                continue
            self._symbol_metadata[sym] = meta

            # Build Nautilus CurrencyPair
            nautilus_inst_id = to_nautilus_instrument_id(to_spot_instrument_id(sym))
            instrument = CurrencyPair(
                instrument_id=nautilus_inst_id,
                raw_symbol=nautilus_inst_id.symbol,
                base_currency=Currency.from_str(meta["base_asset"]),
                quote_currency=Currency.from_str(meta["quote_asset"]),
                price_precision=meta["price_precision"],
                size_precision=meta["size_precision"],
                price_increment=Price(meta["price_increment"], meta["price_precision"]),
                size_increment=Quantity(meta["size_increment"], meta["size_precision"]),
                min_quantity=Quantity(meta["min_qty"], meta["size_precision"]),
                max_quantity=Quantity(meta["max_qty"], meta["size_precision"]),
                min_price=Price(meta["min_price"], meta["price_precision"]),
                max_price=Price(meta["max_price"], meta["price_precision"]),
                ts_event=time.time_ns(),
                ts_init=time.time_ns(),
            )
            instruments_to_write.append(instrument)

            # Insert/update in PostgreSQL
            if self.pool:
                await self._persist_instrument_db(meta)

        if instruments_to_write:
            self.nautilus_manager.write_instruments(instruments_to_write)
            logger.info("Wrote %d instruments to Nautilus catalog", len(instruments_to_write))

    async def _persist_instrument_db(self, meta: dict[str, Any]) -> None:
        if not self.pool:
            return
        query = """
        INSERT INTO instruments (
            instrument_id, raw_symbol, venue, market_type,
            base_currency, quote_currency, price_precision, size_precision,
            price_increment, size_increment, min_notional, status, is_active, updated_at
        ) VALUES ($1, $2, 'BINANCE', 'SPOT', $3, $4, $5, $6, $7, $8, $9, $10, $11, now())
        ON CONFLICT (instrument_id) DO UPDATE SET
            price_precision = EXCLUDED.price_precision,
            size_precision = EXCLUDED.size_precision,
            price_increment = EXCLUDED.price_increment,
            size_increment = EXCLUDED.size_increment,
            min_notional = EXCLUDED.min_notional,
            status = EXCLUDED.status,
            is_active = EXCLUDED.is_active,
            updated_at = now();
        """
        inst_id = to_spot_instrument_id(meta["symbol"])
        await self.pool.execute(
            query,
            inst_id,
            meta["symbol"],
            meta["base_asset"],
            meta["quote_asset"],
            meta["price_precision"],
            meta["size_precision"],
            meta["price_increment"],
            meta["size_increment"],
            meta.get("min_notional"),
            meta["status"],
            meta["is_trading"],
        )

    async def _on_closed_bar_event(
        self, symbol: str, timeframe: str, raw: RawBinanceKline, arrival_ns: int
    ) -> None:
        """Callback invoked whenever the WebSocket stream yields a closed kline."""
        if not self.coordinator:
            return
        meta = self._symbol_metadata.get(symbol, {})
        p_prec = meta.get("price_precision", 2)
        s_prec = meta.get("size_precision", 6)

        inst_id = to_spot_instrument_id(symbol)
        bar_type = to_nautilus_bar_type(inst_id, timeframe)

        bar = EnrichedBar.from_binance_raw(
            raw=raw,
            instrument_id=inst_id,
            bar_type=str(bar_type),
            price_precision=p_prec,
            size_precision=s_prec,
            received_at_ns=arrival_ns,
        )

        await self.coordinator.commit_batch(
            bars=[bar],
            bar_type=bar_type,
            venue="BINANCE",
            market_type="SPOT",
            symbol=symbol,
            timeframe=timeframe,
        )
        self.last_closed_bar_ts[f"{symbol}:{timeframe}"] = bar.ts_event_ns

    async def start(self) -> None:
        """Start the ingestion pipeline."""
        self._running = True
        await self.initialize()

        self.stream_client = BinanceStreamClient(
            symbols=self.symbols,
            timeframes=self.timeframes,
            on_closed_bar=self._on_closed_bar_event,
            redis=self.redis,
        )

        # Launch WebSocket client in background
        ws_task = asyncio.create_task(self.stream_client.start())

        # Give socket 1s to open connection, then run subscribe-before-backfill gap recovery
        await asyncio.sleep(1.0)
        
        # Subscribe to new instrument onboarding events
        if self.redis:
            async def _listen_for_new_instruments():
                pubsub = self.redis.pubsub()
                await pubsub.subscribe("CHANNEL_INSTRUMENTS_UPDATED")
                logger.info("Listening for new instruments on CHANNEL_INSTRUMENTS_UPDATED...")
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        new_sym = message["data"]
                        logger.info(f"Received onboarding event for {new_sym}")
                        if new_sym not in self.symbols:
                            self.symbols.append(new_sym)
                        if self.stream_client:
                            self.stream_client.add_symbol(new_sym)
            asyncio.create_task(_listen_for_new_instruments())

        if self.recovery_manager:
            for s in self.symbols:
                for tf in self.timeframes:
                    meta = self._symbol_metadata.get(s, {})
                    bar_type = to_nautilus_bar_type(to_spot_instrument_id(s), tf)
                    await self.recovery_manager.recover_gaps_for_stream(
                        symbol=s,
                        timeframe=tf,
                        bar_type=bar_type,
                        price_precision=meta.get("price_precision", 2),
                        size_precision=meta.get("size_precision", 6),
                        stream_client=self.stream_client,
                    )

        await ws_task

    async def stop(self) -> None:
        """Stop ingestion and release resources."""
        self._running = False
        if self.stream_client:
            await self.stream_client.stop()
        if self.coordinator:
            await self.coordinator.release_process_lock()
        if self.rest_client:
            await self.rest_client.__aexit__(None, None, None)
        if self.redis:
            await self.redis.aclose()
        if self.pool:
            await self.pool.close()
        logger.info("IngestRecorder stopped successfully")
