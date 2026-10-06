"""The suite never touches the live stack's Redis DBs (0: event bus and heartbeats, 1: agent sessions).

ai_api_harness used to SET exit_monitor:heartbeat (ex=300) and DEL the exit_monitor/notifier heartbeats
on DB 0, masking a dead exit monitor and blipping the A4 healthchecks. No Redis connection is opened
here: redis clients connect lazily."""

import os
import re
from pathlib import Path

import pytest
import redis.asyncio as aioredis
from connections import (
    LIVE_REDIS_DBS,
    REDIS_URL,
    TEST_REDIS_DB,
    assert_isolated,
    isolated_redis_url,
)

TESTS = Path(__file__).resolve().parent


def _db(url: str) -> int:
    return int(aioredis.from_url(url).connection_pool.connection_kwargs.get("db", 0))


def test_the_suite_redis_url_is_not_a_live_db():
    assert TEST_REDIS_DB not in LIVE_REDIS_DBS
    assert _db(REDIS_URL) == TEST_REDIS_DB


def test_code_under_test_reading_the_env_gets_the_same_db():
    assert os.environ["REDIS_URL"] == REDIS_URL


@pytest.mark.parametrize(("url", "expected"), [
    ("redis://:redis_dev_pass@redis:6379/0", "redis://:redis_dev_pass@redis:6379/15"),
    ("redis://127.0.0.1:6379/1", "redis://127.0.0.1:6379/15"),
    ("redis://localhost:6379", "redis://localhost:6379/15"),
])
def test_isolated_url_keeps_host_and_credentials(url, expected):
    assert isolated_redis_url(url, 15) == expected


@pytest.mark.parametrize("db", sorted(LIVE_REDIS_DBS))
def test_a_live_db_is_refused(db):
    with pytest.raises(RuntimeError):
        isolated_redis_url("redis://localhost:6379/9", db)
    with pytest.raises(RuntimeError):
        assert_isolated(aioredis.from_url(f"redis://localhost:6379/{db}"))


def test_the_api_harness_writes_heartbeats_only_through_the_guard():
    src = (TESTS / "ai_api_harness.py").read_text()
    assert "from connections import REDIS_URL, assert_isolated" in src
    make = src[src.index("async def make_harness"):src.index("async def close_harness")]
    close = src[src.index("async def close_harness"):]
    assert make.index("assert_isolated(redis)") < make.index('"exit_monitor:heartbeat"')
    assert close.index("assert_isolated(h.redis)") < close.index(".delete(")


def test_no_test_hardcodes_a_live_redis_db():
    pattern = re.compile(r"redis://[^\s\"']*:6379/[01]\b")
    offenders = [p.name for p in TESTS.glob("*.py")
                 if p.name not in ("connections.py", "test_test_isolation.py") and pattern.search(p.read_text())]
    assert offenders == []
