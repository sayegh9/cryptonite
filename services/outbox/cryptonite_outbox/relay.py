"""Shared Transactional Outbox Relay Service for Cryptonite.

Claims pending outbox events using a two-phase SELECT ... FOR UPDATE SKIP LOCKED
transaction, publishes them reliably to Redis Streams, and marks them PUBLISHED.
"""

import asyncio
import json
import logging
import os
import socket
import time
from typing import Any

import asyncpg
import redis.asyncio as aioredis

logger = logging.getLogger("cryptonite.outbox")

DEFAULT_DB_URL = "postgresql://cryptonite_outbox:outbox_dev_pass@127.0.0.1:5432/cryptonite"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"


class OutboxRelay:
    """Consistently and reliably drains outbox_events to Redis Streams."""

    def __init__(
        self,
        database_url: str | None = None,
        redis_url: str | None = None,
        batch_size: int = 50,
        lease_seconds: int = 30,
        max_attempts: int = 10,
        poll_interval_s: float = 0.05,
    ) -> None:
        self.database_url = database_url or os.getenv("DATABASE_URL", DEFAULT_DB_URL)
        self.redis_url = redis_url or os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.poll_interval_s = poll_interval_s

        self.lease_owner = f"{socket.gethostname()}-{os.getpid()}-{time.time_ns()}"
        self.pool: asyncpg.Pool | None = None
        self.redis: aioredis.Redis | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None

        self.published_count = 0
        self.failed_count = 0

    async def start(self) -> None:
        """Connect to DB and Redis, then launch the polling loop."""
        logger.info("Starting OutboxRelay with lease_owner=%s", self.lease_owner)
        self.pool = await asyncpg.create_pool(self.database_url, min_size=2, max_size=10)
        self.redis = aioredis.from_url(self.redis_url, decode_responses=True)
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Gracefully stop the relay loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self.redis:
            await self.redis.aclose()
            self.redis = None

        if self.pool:
            await self.pool.close()
            self.pool = None
        logger.info("OutboxRelay stopped")

    async def _claim_batch(self) -> list[dict[str, Any]]:
        """Phase 1: Claim pending rows and update lease within the same transaction."""
        if not self.pool:
            return []

        query = f"""
        WITH claimed AS (
            SELECT id
            FROM outbox_events
            WHERE status = 'PENDING'
              AND next_attempt_at <= now()
              AND (lease_expires_at IS NULL OR lease_expires_at < now())
            ORDER BY id ASC
            LIMIT {self.batch_size}
            FOR UPDATE SKIP LOCKED
        )
        UPDATE outbox_events
        SET lease_owner = $1,
            lease_expires_at = now() + INTERVAL '{self.lease_seconds} seconds',
            attempt_count = attempt_count + 1
        FROM claimed
        WHERE outbox_events.id = claimed.id
        RETURNING outbox_events.id, outbox_events.event_id, outbox_events.stream_name,
                  outbox_events.event_type, outbox_events.payload, outbox_events.attempt_count;
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(query, self.lease_owner)
                return [dict(r) for r in rows]

    async def _mark_published(self, event_id_pk: int, published_stream_id: str) -> None:
        """Mark event successfully published."""
        if not self.pool:
            return
        query = """
        UPDATE outbox_events
        SET status = 'PUBLISHED',
            published_stream_id = $2,
            published_at = now(),
            lease_owner = NULL,
            lease_expires_at = NULL
        WHERE id = $1;
        """
        await self.pool.execute(query, event_id_pk, published_stream_id)

    async def _mark_failed_or_retry(self, event_id_pk: int, attempt_count: int, error_msg: str) -> None:
        """Mark row failed (dead-letter) if max attempts reached, or schedule exponential backoff."""
        if not self.pool:
            return
        if attempt_count >= self.max_attempts:
            query = """
            UPDATE outbox_events
            SET status = 'FAILED',
                last_error = $2,
                lease_owner = NULL,
                lease_expires_at = NULL
            WHERE id = $1;
            """
            await self.pool.execute(query, event_id_pk, error_msg)
            self.failed_count += 1
            logger.error("Outbox event id=%d transitioned to FAILED (dead-letter): %s", event_id_pk, error_msg)
        else:
            # Exponential backoff: 2^attempt_count seconds
            backoff_s = min(2 ** attempt_count, 300)
            query = f"""
            UPDATE outbox_events
            SET status = 'PENDING',
                last_error = $2,
                next_attempt_at = now() + INTERVAL '{backoff_s} seconds',
                lease_owner = NULL,
                lease_expires_at = NULL
            WHERE id = $1;
            """
            await self.pool.execute(query, event_id_pk, error_msg)
            logger.warning(
                "Outbox event id=%d failed attempt %d, retrying in %ds: %s",
                event_id_pk,
                attempt_count,
                backoff_s,
                error_msg,
            )

    async def process_batch_once(self) -> int:
        """Claim and publish a single batch of events. Returns count of published events."""
        claimed_rows = await self._claim_batch()
        if not claimed_rows:
            return 0

        published_this_batch = 0
        for row in claimed_rows:
            event_id_pk = row["id"]
            stream_name = row["stream_name"]
            event_id = row["event_id"]
            attempt_count = row["attempt_count"]
            payload = row["payload"]

            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception as e:
                    await self._mark_failed_or_retry(event_id_pk, self.max_attempts, f"Invalid JSON payload: {e}")
                    continue

            try:
                if not self.redis:
                    raise RuntimeError("Redis client not initialized")

                # Publish to Redis Stream
                if stream_name.startswith("nautilus:stream:bus") or (
                    isinstance(payload, dict) and "topic" in payload and "type" in payload and "payload" in payload
                ):
                    stream_payload = {
                        "topic": str(payload["topic"]),
                        "type": str(payload["type"]),
                        "payload": str(payload["payload"]),
                        "encoding": str(payload.get("encoding", "JSON")),
                    }
                else:
                    stream_payload = {
                        "event_id": event_id,
                        "event_type": row["event_type"],
                        "data": json.dumps(payload),
                    }
                stream_entry_id = await self.redis.xadd(stream_name, stream_payload)
                await self._mark_published(event_id_pk, str(stream_entry_id))
                self.published_count += 1
                published_this_batch += 1
                logger.debug("Published outbox event %s to stream %s as %s", event_id, stream_name, stream_entry_id)
            except Exception as e:
                logger.exception("Failed to publish outbox event id=%d (%s): %s", event_id_pk, event_id, e)
                await self._mark_failed_or_retry(event_id_pk, attempt_count, str(e))

        return published_this_batch

    async def _run_loop(self) -> None:
        """Continuous polling loop."""
        while self._running:
            try:
                published = await self.process_batch_once()
                if published == 0:
                    await asyncio.sleep(self.poll_interval_s)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Unexpected error in OutboxRelay loop: %s", e)
                await asyncio.sleep(1.0)

    async def check_readiness(self) -> tuple[bool, dict[str, Any]]:
        """Evaluate readiness conditions: DB ok, Redis ok, and NO FAILED dead-letter events exist."""
        details: dict[str, Any] = {
            "postgres": False,
            "redis": False,
            "dead_letter_failed_count": 0,
            "status": "unready",
        }
        if not self.pool or not self.redis:
            return False, details

        try:
            val = await self.pool.fetchval("SELECT 1")
            details["postgres"] = (val == 1)
        except Exception as e:
            details["postgres_error"] = str(e)

        try:
            r_ping = await self.redis.ping()
            details["redis"] = bool(r_ping)
        except Exception as e:
            details["redis_error"] = str(e)

        try:
            failed_count = await self.pool.fetchval(
                "SELECT count(*) FROM outbox_events WHERE status = 'FAILED'"
            )
            details["dead_letter_failed_count"] = failed_count
        except Exception as e:
            details["failed_count_error"] = str(e)

        # Readiness fails if PG or Redis is down, or if dead-letter FAILED outbox events exist
        is_ready = (
            details["postgres"]
            and details["redis"]
            and (details["dead_letter_failed_count"] == 0)
        )
        details["status"] = "ready" if is_ready else "unready"
        return is_ready, details
