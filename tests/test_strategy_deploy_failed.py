"""A strategy instance the engine cannot start is marked FAILED with an alert, never left RUNNING (2026-10-21:
INST-77DF31B7 on a venue-less XRPUSDT sat RUNNING with no strategy behind it)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from cryptonite_orchestrator.server import mark_deploy_failed


class FakeConn:
    def __init__(self):
        self.executed: list[tuple] = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    def transaction(self):
        conn = self

        class T:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_):
                return False
        return T()


def pool_for(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    return pool


def test_a_failed_deploy_is_marked_failed_and_alerted():
    conn, redis = FakeConn(), MagicMock()
    redis.publish = AsyncMock()
    asyncio.run(mark_deploy_failed(pool_for(conn), redis, "INST-1", "XRPUSDT", "invalid `InstrumentId` value 'XRPUSDT'"))
    update = [a for sql, a in conn.executed if "UPDATE paper_strategy_instances" in sql]
    assert update == [("INST-1",)]
    assert "status = 'FAILED'" in conn.executed[0][0] and "status = 'RUNNING'" in conn.executed[0][0]
    outbox = [a for sql, a in conn.executed if "notifications_outbox" in sql]
    assert outbox and outbox[0][0] == "STRATEGY_DEPLOY_FAILED" and outbox[0][4] == "deploy-failed:INST-1"
    published = json.loads(redis.publish.await_args.args[1])
    assert published["kind"] == "STRATEGY_DEPLOY_FAILED" and published["instance_id"] == "INST-1"


def test_a_db_error_while_marking_does_not_raise():
    pool = MagicMock()
    pool.acquire.side_effect = RuntimeError("db down")
    asyncio.run(mark_deploy_failed(pool, None, "INST-2", "XRPUSDT", "boom"))  # logged, the sync loop keeps going
