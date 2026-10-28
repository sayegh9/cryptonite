"""Tests for shared outbox relay: lease claiming, Redis publishing, and dead-letter readiness."""

import json
from uuid import uuid4

import asyncpg
import pytest
import redis.asyncio as aioredis
from connections import REDIS_URL
from cryptonite_outbox.relay import OutboxRelay
from migrated_db import migrated_database


@pytest.fixture
def db_url():
    with migrated_database("cryptonite_outbox_relay") as url:
        yield url

@pytest.mark.asyncio
async def test_outbox_relay_lifecycle_and_redis_stream(db_url):
    pool = await asyncpg.create_pool(db_url)
    r = aioredis.from_url(REDIS_URL, decode_responses=True)

    try:
        # 1. Insert pending outbox event
        event_id = str(uuid4())
        stream_name = f"test:stream:{uuid4().hex[:6]}"
        payload = {
            "schema_version": 1,
            "producer": "cryptonite-test",
            "event_id": event_id,
            "event_type": "test_event",
            "sample_data": 12345,
        }

        await pool.execute(
            """
            INSERT INTO outbox_events (
                event_id, stream_name, event_type, producer, payload,
                status, attempt_count, next_attempt_at, created_at
            ) VALUES ($1, $2, $3, $4, $5::jsonb, 'PENDING', 0, now(), now());
            """,
            event_id,
            stream_name,
            "test_event",
            "cryptonite-test",
            json.dumps(payload),
        )

        relay = OutboxRelay(database_url=db_url, redis_url=REDIS_URL, max_attempts=3)
        relay.pool = pool
        relay.redis = r

        # 2. Process batch
        published = await relay.process_batch_once()
        assert published >= 1

        # 3. Verify row in DB is marked PUBLISHED with published_stream_id
        row = await pool.fetchrow("SELECT * FROM outbox_events WHERE event_id = $1;", event_id)
        assert row is not None
        assert row["status"] == "PUBLISHED"
        assert row["published_stream_id"] is not None
        assert row["published_at"] is not None
        assert row["lease_owner"] is None

        # 4. Verify message in Redis Stream
        stream_entries = await r.xread({stream_name: "0-0"}, count=5)
        assert stream_entries is not None
        assert len(stream_entries) > 0
        _, messages = stream_entries[0]
        assert len(messages) >= 1
        msg_id, msg_data = messages[0]
        assert msg_data["event_id"] == event_id
        decoded_payload = json.loads(msg_data["data"])
        assert decoded_payload["sample_data"] == 12345

        # 5. Readiness check passes when no FAILED events exist
        is_ready, details = await relay.check_readiness()
        assert is_ready is True
        assert details["dead_letter_failed_count"] == 0

        # 6. Test dead-letter failure check: insert FAILED event
        failed_event_id = str(uuid4())
        await pool.execute(
            """
            INSERT INTO outbox_events (
                event_id, stream_name, event_type, producer, payload,
                status, attempt_count, last_error, created_at
            ) VALUES ($1, 'test:failed', 'err', 'test', '{}'::jsonb, 'FAILED', 10, 'Fatal error', now());
            """,
            failed_event_id,
        )

        is_ready_failed, details_failed = await relay.check_readiness()
        assert is_ready_failed is False, "Readiness must fail if dead-letter FAILED outbox events exist"
        assert details_failed["dead_letter_failed_count"] >= 1

        # Clean up failed row
        await pool.execute("DELETE FROM outbox_events WHERE event_id = $1;", failed_event_id)

    finally:
        await r.aclose()
        await pool.close()
