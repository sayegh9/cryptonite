"""Single-writer Two-Store Crash-Recoverable Idempotent Commit Coordinator."""

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import asyncpg
from cryptonite_schemas.bar import (
    EnrichedBar,
    bar_to_nautilus,
    compute_payload_checksum,
)
from nautilus_trader.model import BarType

from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from cryptonite_storage.queries import timeframe_ns

logger = logging.getLogger("cryptonite.coordinator")

# Dedicated advisory lock key for Cryptonite Ingest process exclusivity
INGEST_ADVISORY_LOCK_ID = 82491735


class IngestionConflictError(Exception):
    """Raised when another recorder process already owns the advisory lock."""


class TwoStoreCoordinator:
    """Coordinates writes between Nautilus ParquetDataCatalog and Enriched Analytics Lake."""

    def __init__(
        self,
        nautilus_manager: NautilusCatalogManager,
        analytics_writer: AnalyticsLakeWriter,
        pg_pool: asyncpg.Pool | None = None,
    ) -> None:
        self.nautilus = nautilus_manager
        self.analytics = analytics_writer
        self.pool = pg_pool
        self._lock = asyncio.Lock()
        self._batch_lock = asyncio.Lock()
        self._advisory_lock_held = False
        self._advisory_conn: asyncpg.Connection | None = None
        self._pending_bars: dict[tuple, list[EnrichedBar]] = {}
        self._last_flush_time: dict[tuple, float] = {}

    async def acquire_process_lock(self) -> bool:
        """Acquire PostgreSQL advisory lock for process-exclusive single writer."""
        if not self.pool:
            return True
        conn = await self.pool.acquire()
        locked = await conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", INGEST_ADVISORY_LOCK_ID
        )
        if not locked:
            await self.pool.release(conn)
            self._advisory_lock_held = False
            raise IngestionConflictError(
                "Another Cryptonite ingest process currently owns the advisory lock."
            )
        self._advisory_conn = conn
        self._advisory_lock_held = True
        logger.info("Acquired process-exclusive PostgreSQL advisory lock %d", INGEST_ADVISORY_LOCK_ID)
        return True

    async def release_process_lock(self) -> None:
        """Release PostgreSQL advisory lock."""
        if self._advisory_conn and self.pool:
            try:
                await self._advisory_conn.execute(
                    "SELECT pg_advisory_unlock($1)", INGEST_ADVISORY_LOCK_ID
                )
                await self.pool.release(self._advisory_conn)
            except Exception as e:
                logger.warning("Error releasing advisory lock: %s", e)
            finally:
                self._advisory_conn = None
                self._advisory_lock_held = False

    @property
    def is_leader(self) -> bool:
        return self._advisory_lock_held or (self.pool is None)

    async def commit_batch(
        self,
        bars: list[EnrichedBar],
        bar_type: BarType,
        venue: str = "BINANCE",
        market_type: str = "SPOT",
        symbol: str = "BTCUSDT",
        timeframe: str = "1m",
        force: bool = True,
    ) -> str:
        if not bars:
            return ""

        key = (bar_type, venue, market_type, symbol, timeframe)

        if force:
            async with self._batch_lock:
                pending = self._pending_bars.pop(key, [])
            all_bars = pending + bars
            return await self._execute_commit(all_bars, bar_type, venue, market_type, symbol, timeframe)

        flush_bars = None
        async with self._batch_lock:
            if key not in self._pending_bars:
                self._pending_bars[key] = []
                self._last_flush_time[key] = time.time()
            
            self._pending_bars[key].extend(bars)
            
            now = time.time()
            # Flush every 1000 bars or 300 seconds (5 mins)
            if len(self._pending_bars[key]) >= 1000 or (now - self._last_flush_time[key]) >= 300:
                flush_bars = self._pending_bars[key]
                self._pending_bars[key] = []
                self._last_flush_time[key] = now

        if flush_bars:
            return await self._execute_commit(flush_bars, bar_type, venue, market_type, symbol, timeframe)
        return ""

    async def flush_pending(self) -> list[str]:
        """Flush all buffered bars across all streams."""
        batch_ids = []
        async with self._batch_lock:
            keys = list(self._pending_bars.keys())
        for key in keys:
            async with self._batch_lock:
                bars = self._pending_bars.pop(key, [])
            if bars:
                bar_type, venue, market_type, symbol, timeframe = key
                b_id = await self._execute_commit(bars, bar_type, venue, market_type, symbol, timeframe)
                if b_id:
                    batch_ids.append(b_id)
        return batch_ids

    async def _execute_commit(
        self,
        bars: list[EnrichedBar],
        bar_type: BarType,
        venue: str,
        market_type: str,
        symbol: str,
        timeframe: str,
    ) -> str:
        """Execute the crash-recoverable two-store commit protocol."""
        async with self._lock:
            # Sort bars strictly by timestamp
            sorted_bars = sorted(bars, key=lambda b: b.ts_event_ns)
            batch_id = str(uuid.uuid4())
            for b in sorted_bars:
                b.ingest_batch_id = batch_id

            # 1. Build normalized logical-bar payload & compute payload_checksum
            payload_json = [b.model_dump(mode="json") for b in sorted_bars]
            payload_checksum = compute_payload_checksum(sorted_bars)
            for b in sorted_bars:
                b.payload_checksum = payload_checksum

            start_ns = sorted_bars[0].ts_event_ns
            end_ns = sorted_bars[-1].ts_event_ns
            bar_count = len(sorted_bars)
            bar_type_str = str(bar_type)

            # 2. Content-level cadence check BEFORE insert
            self._verify_cadence(sorted_bars, timeframe)

            # 2.5 Load and validate checkpoint boundary
            if self.pool:
                checkpoint_row = await self.pool.fetchrow(
                    "SELECT last_timestamp_ns FROM ingest_checkpoints WHERE venue = $1 AND market_type = $2 AND symbol = $3 AND timeframe = $4",
                    venue, market_type, symbol, timeframe
                )
                if checkpoint_row:
                    last_ts = checkpoint_row["last_timestamp_ns"]
                    if start_ns <= last_ts:
                        logger.warning("Batch starts at %d which is older than checkpoint %d. Skipping.", start_ns, last_ts)
                        return ""

            # 3. Insert ingest_batches row with status='pending'
            if self.pool:
                actual_batch_id = await self._insert_pending_batch(
                    batch_id=batch_id,
                    venue=venue,
                    market_type=market_type,
                    symbol=symbol,
                    bar_type=bar_type_str,
                    start_ns=start_ns,
                    end_ns=end_ns,
                    bar_count=bar_count,
                    payload_checksum=payload_checksum,
                    canonical_payload_json=payload_json,
                )
                if actual_batch_id:
                    batch_id = actual_batch_id

            # 3. Write to Nautilus Catalog
            nautilus_bars = [bar_to_nautilus(b, bar_type) for b in sorted_bars]
            nautilus_paths, nautilus_file_checksum = self.nautilus.write_bars(nautilus_bars)

            # 4. Write to Analytics Lake
            analytics_path, analytics_file_checksum = self.analytics.write_batch(
                bars=sorted_bars,
                venue=venue,
                market_type=market_type,
                timeframe=timeframe,
                symbol=symbol,
                batch_id=batch_id,
            )

            # 6. Single Postgres transaction: commit batch & advance checkpoint
            if self.pool:
                await self._commit_batch_and_checkpoint(
                    batch_id=batch_id,
                    venue=venue,
                    market_type=market_type,
                    symbol=symbol,
                    timeframe=timeframe,
                    bar_type=bar_type_str,
                    start_ns=start_ns,
                    end_ns=end_ns,
                    bar_count=bar_count,
                    nautilus_paths=nautilus_paths,
                    analytics_paths=[analytics_path] if analytics_path else [],
                    nautilus_file_checksum=nautilus_file_checksum,
                    analytics_file_checksum=analytics_file_checksum,
                    payload_checksum=payload_checksum,
                    bars=sorted_bars,
                )

            logger.info(
                "Committed batch %s for %s (%d bars, start=%d, end=%d)",
                batch_id,
                bar_type_str,
                bar_count,
                start_ns,
                end_ns,
            )
            return batch_id

    def _verify_cadence(self, bars: list[EnrichedBar], timeframe: str) -> None:
        """Verify content-level contiguous cadence between consecutive bars."""
        if len(bars) <= 1:
            return
        step_ns = self._timeframe_to_ns(timeframe)
        for i in range(1, len(bars)):
            diff = bars[i].ts_event_ns - bars[i - 1].ts_event_ns
            if diff < step_ns:
                logger.warning(
                    "Duplicate or sub-cadence bar detected at %d (diff=%d ns)",
                    bars[i].ts_event_ns,
                    diff,
                )

    def _timeframe_to_ns(self, timeframe: str) -> int:
        # Raises on an unknown or month (1M) timeframe instead of silently taking one minute: the
        # closed_bar_committed open times are derived from it.
        return timeframe_ns(timeframe)

    async def _insert_pending_batch(
        self,
        batch_id: str,
        venue: str,
        market_type: str,
        symbol: str,
        bar_type: str,
        start_ns: int,
        end_ns: int,
        bar_count: int,
        payload_checksum: str,
        canonical_payload_json: list[dict[str, Any]],
    ) -> None:
        if not self.pool:
            return
        query = """
        INSERT INTO ingest_batches (
            batch_id, venue, market_type, symbol, bar_type,
            start_ns, end_ns, bar_count, payload_checksum,
            canonical_payload_json, status
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'pending')
        ON CONFLICT (venue, market_type, bar_type, start_ns, end_ns, payload_checksum)
        DO UPDATE SET status = 'pending', canonical_payload_json = EXCLUDED.canonical_payload_json
        RETURNING batch_id;
        """
        row = await self.pool.fetchval(
            query,
            uuid.UUID(batch_id),
            venue,
            market_type,
            symbol,
            bar_type,
            start_ns,
            end_ns,
            bar_count,
            payload_checksum,
            json.dumps(canonical_payload_json),
        )
        return str(row) if row else batch_id

    async def _commit_batch_and_checkpoint(
        self,
        batch_id: str,
        venue: str,
        market_type: str,
        symbol: str,
        timeframe: str,
        bar_type: str,
        start_ns: int,
        end_ns: int,
        bar_count: int,
        nautilus_paths: list[str],
        analytics_paths: list[str],
        nautilus_file_checksum: str,
        analytics_file_checksum: str,
        payload_checksum: str = "",
        bars: list[EnrichedBar] | None = None,
    ) -> None:
        if not self.pool:
            return
        outbox_event_id = str(uuid.uuid4())
        step_ns = self._timeframe_to_ns(timeframe)
        # Bars are stamped at their CLOSE: start_ns / end_ns are the first and last bar's close. The event
        # names the batch by the resampler's contract (and the open_kline ticks'): the first bar's OPEN,
        # the last bar's close as the exclusive end. It used to send the first close as bar_open_ns and
        # end one step late, so the chart never matched its provisional candle (B1 / F-C1 follow-up).
        # schema_version 2 marks the corrected meaning, so consumers can ignore version-1 events from a
        # producer not yet redeployed instead of misreading their times.
        last = bars[-1] if bars else None
        event_payload = {
            "schema_version": 2,
            "producer": "cryptonite-ingest",
            "occurred_at_ns": time.time_ns(),
            "event_id": outbox_event_id,
            "event_type": "closed_bar_committed",
            "batch_id": batch_id,
            "venue": venue,
            "market_type": market_type,
            "symbol": symbol,
            "instrument_id": f"{symbol.upper()}.{venue.upper()}",
            "timeframe": timeframe,
            "bar_type": bar_type,
            "bar_open_ns": start_ns - step_ns,
            "bar_close_inclusive_ns": end_ns - 1_000_000,
            "interval_end_exclusive_ns": end_ns,
            "last_bar_open_ns": end_ns - step_ns,
            "bar_count": bar_count,
            # final values of the last bar, so a consumer never depends on having seen its last tick
            **({f: str(getattr(last, f)) for f in ("open", "high", "low", "close", "volume")} if last else {}),
            "payload_checksum": payload_checksum,
            "analytics_file_checksum": analytics_file_checksum,
            "nautilus_file_checksum": nautilus_file_checksum,
        }

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. Update batch status
                await conn.execute(
                    """
                    UPDATE ingest_batches
                    SET status = 'committed',
                        nautilus_paths = $2,
                        analytics_paths = $3,
                        nautilus_file_checksum = $4,
                        analytics_file_checksum = $5,
                        committed_at = now()
                    WHERE batch_id = $1;
                    """,
                    uuid.UUID(batch_id),
                    nautilus_paths,
                    analytics_paths,
                    nautilus_file_checksum,
                    analytics_file_checksum,
                )

                # 2. Advance checkpoint
                await conn.execute(
                    """
                    INSERT INTO ingest_checkpoints (
                        venue, market_type, symbol, bar_type, timeframe,
                        first_timestamp_ns, last_timestamp_ns, total_bars,
                        last_batch_id, last_updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
                    ON CONFLICT (venue, market_type, symbol, timeframe)
                    DO UPDATE SET
                        last_timestamp_ns = GREATEST(ingest_checkpoints.last_timestamp_ns, EXCLUDED.last_timestamp_ns),
                        first_timestamp_ns = LEAST(ingest_checkpoints.first_timestamp_ns, EXCLUDED.first_timestamp_ns),
                        total_bars = ingest_checkpoints.total_bars + EXCLUDED.total_bars,
                        last_batch_id = EXCLUDED.last_batch_id,
                        last_updated_at = now();
                    """,
                    venue,
                    market_type,
                    symbol,
                    bar_type,
                    timeframe,
                    start_ns,
                    end_ns,
                    bar_count,
                    uuid.UUID(batch_id),
                )

                # 3. Insert outbox event for closed_bar_committed
                await conn.execute(
                    """
                    INSERT INTO outbox_events (
                        event_id, stream_name, event_type, producer, payload,
                        status, attempt_count, next_attempt_at, created_at
                    ) VALUES ($1, $2, $3, $4, $5::jsonb, 'PENDING', 0, now(), now());
                    """,
                    outbox_event_id,
                    "cryptonite:stream:closed_bar_committed",
                    "closed_bar_committed",
                    "cryptonite-ingest",
                    json.dumps(event_payload),
                )

                # 4. Insert native Bar outbox events for nautilus:stream:bus
                if bars:
                    parsed_bt = BarType.from_str(bar_type)
                    for b in bars:
                        naut_bar = bar_to_nautilus(b, parsed_bt)
                        bar_payload = {
                            "topic": f"data.bars.{bar_type}",
                            "type": "Bar",
                            "payload": naut_bar.to_json_bytes().decode("utf-8"),
                            "encoding": "JSON",
                        }
                        await conn.execute(
                            """
                            INSERT INTO outbox_events (
                                event_id, stream_name, event_type, producer, payload,
                                status, attempt_count, next_attempt_at, created_at
                            ) VALUES ($1, $2, $3, $4, $5::jsonb, 'PENDING', 0, now(), now());
                            """,
                            str(uuid.uuid4()),
                            "nautilus:stream:bus",
                            "Bar",
                            "cryptonite-ingest",
                            json.dumps(bar_payload),
                        )

    async def reconcile_pending_batches(self, bar_type: BarType) -> int:
        """Scan and reconcile uncommitted batches on startup."""
        if not self.pool:
            return 0

        # Remove incomplete artifacts
        try:
            for tmp_file in self.analytics.base_dir.rglob(".tmp.*.parquet"):
                try:
                    tmp_file.unlink()
                except OSError:
                    pass
        except Exception as e:
            logger.warning("Failed to clean up temporary artifacts: %s", e)

        rows = await self.pool.fetch(
            "SELECT batch_id, venue, market_type, symbol, bar_type, canonical_payload_json "
            "FROM ingest_batches WHERE status = 'pending';"
        )
        reconciled = 0
        for row in rows:
            raw_payload = row["canonical_payload_json"]
            if isinstance(raw_payload, str):
                raw_payload = json.loads(raw_payload)
            bars = [EnrichedBar.model_validate(item) for item in raw_payload]
            logger.info("Reconciling uncommitted batch %s (%d bars)", row["batch_id"], len(bars))
            # Replay commit sequence
            await self._execute_commit(
                bars=bars,
                bar_type=bar_type,
                venue=row["venue"],
                market_type=row["market_type"],
                symbol=row["symbol"],
                timeframe=row.get("timeframe", "1m"),  # fallback if timeframe missing
            )
            reconciled += 1
        return reconciled

    async def quarantine_conflicting_bar(
        self,
        bar: EnrichedBar,
        existing_data: dict[str, Any] | None,
        reason: str,
    ) -> None:
        """Store conflicting duplicate bar in quarantine_bars table."""
        if not self.pool:
            logger.warning("Quarantining conflicting bar (no DB): %s - %s", bar.ts_event_ns, reason)
            return
        await self.pool.execute(
            """
            INSERT INTO quarantine_bars (
                venue, symbol, bar_type, ts_event_ns,
                conflicting_data, existing_data, reason
            ) VALUES ($1, $2, $3, $4, $5, $6, $7);
            """,
            bar.source,
            bar.instrument_id,
            bar.bar_type,
            bar.ts_event_ns,
            json.dumps(bar.model_dump(mode="json")),
            json.dumps(existing_data) if existing_data else None,
            reason,
        )
        logger.warning(
            "Quarantined conflicting bar for %s at ts=%d: %s",
            bar.bar_type,
            bar.ts_event_ns,
            reason,
        )
