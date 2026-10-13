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
from cryptonite_schemas.universe import (
    INGEST_UNIVERSE_KEY,
    UNIVERSE_REFRESH_S,
    UNIVERSE_TTL_S,
    IngestUniverse,
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
from cryptonite_ingest.stream import (
    BINANCE_FUTURES_WS_QUOTE_STREAM_URL,
    BINANCE_FUTURES_WS_STREAM_URL,
    BinanceStreamClient,
)

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
        perp_quote_symbols: list[str] | None = None,
        perp_quote_hz: float = 50.0,
        spot_quote_hz: float = 0.0,
    ) -> None:
        self.symbols = [s.upper() for s in symbols]
        # USD-M perpetual bookTicker quotes (no bars), published as <SYM>-PERP.BINANCE
        self.perp_quote_symbols = [s.upper() for s in perp_quote_symbols or []]
        self.perp_quote_client: BinanceStreamClient | None = None
        self.perp_quote_hz = perp_quote_hz
        self.spot_quote_hz = spot_quote_hz  # 0 = publish every spot bookTicker update
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
        self._universe_task: asyncio.Task | None = None
        self._symbol_metadata: dict[str, dict[str, Any]] = {}
        self.last_closed_bar_ts: dict[str, int] = {}
        self._recovery_lock = asyncio.Lock()
        self._pending_recovery_symbols: set[str] = set()

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
        try:  # empty instrument files from a killed writer; sync_instruments below writes fresh ones
            if moved := self.nautilus_manager.quarantine_empty_instrument_files():
                logger.warning("Quarantined %d empty instrument file(s): %s", len(moved), moved)
        except Exception as e:  # never block start-up
            logger.error("Empty instrument file check failed: %s", e)
        await self.sync_instruments()

        # Reconcile any uncommitted batches from previous crashes
        for symbol in self.symbols:
            for tf in self.timeframes:
                inst_id = to_spot_instrument_id(symbol)
                bar_type = to_nautilus_bar_type(inst_id, tf)
                await self.coordinator.reconcile_pending_batches(bar_type)

    async def sync_instruments(self, symbols: list[str] | None = None) -> None:
        """Fetch instrument definitions, write to Nautilus catalog and PostgreSQL."""
        sym_list = [s.upper() for s in symbols] if symbols is not None else self.symbols
        logger.info("Syncing instrument definitions from Binance for %s...", sym_list)
        all_info = await self.rest_client.get_exchange_info()
        info_map = {item["symbol"]: item for item in all_info}

        instruments_to_write = []
        for sym in sym_list:
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

        async with self._recovery_lock:
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

    async def _on_stream_reconnect(self) -> None:
        """Callback invoked when BinanceStreamClient reconnects after a drop."""
        logger.warning(
            "Stream reconnected (reconnect_count=%d)! Triggering full gap recovery for all fed symbols: %s",
            self.stream_client.reconnect_count if self.stream_client else 0,
            self.symbols,
        )
        await self.trigger_recovery(self.symbols)

    async def trigger_recovery(self, symbols: list[str] | None = None) -> None:
        """Trigger bounded, idempotent gap recovery for specified symbols (or all fed symbols)."""
        if not self.recovery_manager or not self.stream_client:
            return

        target_syms = set(s.upper() for s in (symbols or self.symbols))
        self._pending_recovery_symbols.update(target_syms)

        if self._recovery_lock.locked():
            logger.info("Gap recovery already in progress; queued symbols: %s", target_syms)
            return

        async with self._recovery_lock:
            recovered_in_session: set[str] = set()
            while self._pending_recovery_symbols:
                batch = sorted(self._pending_recovery_symbols - recovered_in_session)
                self._pending_recovery_symbols.clear()
                if not batch:
                    break
                recovered_in_session.update(batch)
                logger.info(
                    "Running gap recovery for symbols: %s across timeframes: %s",
                    batch,
                    self.timeframes,
                )
                for s in batch:
                    if s not in self._symbol_metadata:
                        await self.sync_instruments(symbols=[s])
                    meta = self._symbol_metadata.get(s, {})
                    p_prec = meta.get("price_precision", 2)
                    s_prec = meta.get("size_precision", 6)

                    for tf in self.timeframes:
                        inst_id = to_spot_instrument_id(s)
                        bar_type = to_nautilus_bar_type(inst_id, tf)
                        try:
                            await self.recovery_manager.heal_empty_files(
                                symbol=s,
                                timeframe=tf,
                                bar_type=bar_type,
                                price_precision=p_prec,
                                size_precision=s_prec,
                            )
                        except Exception as e:
                            logger.error(
                                "Empty-file healing failed during recovery for %s %s: %s", s, tf, e
                            )
                        try:
                            await self.recovery_manager.recover_gaps_for_stream(
                                symbol=s,
                                timeframe=tf,
                                bar_type=bar_type,
                                price_precision=p_prec,
                                size_precision=s_prec,
                                stream_client=self.stream_client,
                            )
                        except Exception as e:
                            logger.error("Gap recovery failed for %s %s: %s", s, tf, e)

    async def add_symbol(self, symbol: str) -> None:
        """Dynamically add a new symbol to the live ingestion recorder."""
        sym = symbol.upper()
        if sym not in self.symbols:
            self.symbols.append(sym)
        # Ensure metadata and instruments are persisted
        await self.sync_instruments(symbols=[sym])

        dynamic_subscribed = False
        if self.stream_client:
            dynamic_subscribed = await self.stream_client.add_symbol(sym)

        await self.publish_universe()

        # If dynamic SUBSCRIBE succeeded on an open socket without reconnect,
        # run gap recovery specifically for this new symbol.
        # If dynamic SUBSCRIBE failed and fell back to closing the socket for a full reconnect,
        # self._on_stream_reconnect will be called upon connection and recover all symbols.
        if dynamic_subscribed:
            await self.trigger_recovery([sym])

    def universe(self) -> IngestUniverse:
        """What this recorder streams now, runtime-onboarded symbols included (D4)."""
        return IngestUniverse.build(self.symbols, self.timeframes, self.perp_quote_symbols)

    async def publish_universe(self) -> None:
        """Publish the ingested universe for the API's creation checks; never raises."""
        if not self.redis:
            return
        try:
            await self.redis.set(
                INGEST_UNIVERSE_KEY, self.universe().model_dump_json(), ex=UNIVERSE_TTL_S
            )
        except (
            Exception
        ) as e:  # the key expiring makes creation fail closed, so a miss here is safe
            logger.warning("Could not publish the ingest universe: %s", e)

    async def _universe_publisher(self) -> None:
        while self._running:
            await self.publish_universe()
            await asyncio.sleep(UNIVERSE_REFRESH_S)

    async def start(self) -> None:
        """Start the ingestion pipeline."""
        self._running = True
        await self.initialize()
        self._universe_task = asyncio.create_task(self._universe_publisher())

        self.stream_client = BinanceStreamClient(
            symbols=self.symbols,
            timeframes=self.timeframes,
            on_closed_bar=self._on_closed_bar_event,
            on_reconnect=self._on_stream_reconnect,
            redis=self.redis,
            min_quote_interval_ms=1000.0 / self.spot_quote_hz if self.spot_quote_hz > 0 else 0,
        )

        # Launch WebSocket client in background
        ws_task = asyncio.create_task(self.stream_client.start())

        if self.perp_quote_symbols:
            self.perp_quote_client = BinanceStreamClient(
                symbols=self.perp_quote_symbols,
                timeframes=[],
                redis=self.redis,
                base_ws_url=BINANCE_FUTURES_WS_STREAM_URL,
                quote_ws_url=BINANCE_FUTURES_WS_QUOTE_STREAM_URL,
                instrument_suffix="-PERP",
                min_quote_interval_ms=1000.0 / self.perp_quote_hz if self.perp_quote_hz > 0 else 0,
            )
            asyncio.create_task(self.perp_quote_client.start())

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
                        logger.info("Received onboarding event for %s", new_sym)
                        await self.add_symbol(new_sym)

            asyncio.create_task(_listen_for_new_instruments())

        if self.recovery_manager:
            await self.trigger_recovery(self.symbols)

        await ws_task

    async def stop(self) -> None:
        """Stop ingestion and release resources."""
        self._running = False
        if self._universe_task:
            self._universe_task.cancel()
        if self.stream_client:
            await self.stream_client.stop()
        if self.perp_quote_client:
            await self.perp_quote_client.stop()
        if self.coordinator:
            await self.coordinator.release_process_lock()
        if self.rest_client:
            await self.rest_client.__aexit__(None, None, None)
        if self.redis:
            await self.redis.aclose()
        if self.pool:
            await self.pool.close()
        logger.info("IngestRecorder stopped successfully")
