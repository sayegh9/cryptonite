"""The suite never touches the live stack's Redis DBs (0: event bus and heartbeats, 1: agent sessions).

ai_api_harness used to SET exit_monitor:heartbeat (ex=300) and DEL the exit_monitor/notifier heartbeats
on DB 0, masking a dead exit monitor and blipping the A4 healthchecks. No Redis connection is opened
here: redis clients connect lazily."""

import asyncio
import os
import re
from pathlib import Path
from types import SimpleNamespace

import ai_api_harness
import connections
import pytest
import redis
import redis.asyncio as aioredis
from connections import (
    LIVE_REDIS_DBS,
    REDIS_URL,
    TEST_REDIS_DB,
    allow_live_redis_db,
    assert_isolated,
    isolated_redis_url,
)

TESTS = Path(__file__).resolve().parent


def _db(url: str) -> int:
    return int(aioredis.from_url(url).connection_pool.connection_kwargs.get("db", 0))


def _client_on(db: int):
    """A stand-in client on ``db`` that records commands (no redis-py pool, so the pool guard is not hit)."""
    calls = []

    async def record(*args, **kwargs):
        calls.append(args)

    return SimpleNamespace(connection_pool=SimpleNamespace(connection_kwargs={"db": db}),
                           set=record, delete=record, aclose=record, calls=calls)


def test_the_suite_redis_url_is_not_a_live_db():
    assert TEST_REDIS_DB not in LIVE_REDIS_DBS
    assert _db(REDIS_URL) == TEST_REDIS_DB


def test_code_under_test_reading_the_env_gets_the_same_db():
    assert os.environ["REDIS_URL"] == REDIS_URL


def test_the_rewrite_is_reported_once_in_the_header():
    line = connections.redis_report_line()
    assert line.startswith("redis: ") and f"/{TEST_REDIS_DB} " in line and "REDIS_URL said" in line


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
        assert_isolated(_client_on(db))
    assert_isolated(_client_on(TEST_REDIS_DB))


# --- behavioural: any redis-py client on a live DB raises (conftest installs the guard) ---------------

@pytest.mark.parametrize("make", [
    lambda: redis.Redis(host="localhost"),  # no db= at all means DB 0
    lambda: redis.Redis(host="localhost", db=1),
    lambda: redis.Redis.from_url("redis://localhost:6379/0"),
    lambda: aioredis.Redis(host="localhost"),
    lambda: aioredis.from_url("redis://:pw@localhost:6379/1"),
    lambda: redis.ConnectionPool(host="localhost"),
])
def test_a_redis_client_on_a_live_db_raises(make):
    with pytest.raises(RuntimeError, match="belongs to the live stack"):
        make()


def test_clients_on_the_test_db_are_allowed():
    redis.Redis(host="localhost", db=TEST_REDIS_DB)
    aioredis.from_url(REDIS_URL)


def test_live_db_escape_hatch_needs_a_dedicated_redis(monkeypatch):
    monkeypatch.delenv(connections.DEDICATED_ENV, raising=False)
    with pytest.raises(RuntimeError, match=connections.DEDICATED_ENV), allow_live_redis_db():
        pass
    monkeypatch.setenv(connections.DEDICATED_ENV, "1")
    with allow_live_redis_db():
        redis.Redis(host="localhost", db=0)
    with pytest.raises(RuntimeError):  # only inside the block
        redis.Redis(host="localhost", db=0)


def test_nautilus_redis_users_are_gated_on_a_dedicated_redis():
    # Nautilus' Rust Redis cache/bus clients bypass redis-py (no DB index, so DB 0): the pool guard cannot
    # see them, so every test file that builds one must carry requires_dedicated_redis.
    nautilus = re.compile(r"\b(RedisCacheDatabase|RedisCacheConfig|RedisMessageBusConfig)\(")
    for path in TESTS.glob("*.py"):
        text = path.read_text()
        if path.name != Path(__file__).name and nautilus.search(text):
            assert "@requires_dedicated_redis" in text, path.name


def test_no_test_hardcodes_a_live_redis_db():
    pattern = re.compile(r"redis://[^\s\"']*:6379/[01]\b")
    offenders = [p.name for p in TESTS.glob("*.py")
                 if p.name not in ("connections.py", "test_test_isolation.py") and pattern.search(p.read_text())]
    assert offenders == []


# --- behavioural: the API harness refuses a live DB before any Redis command -----------------------------

def test_make_harness_refuses_a_live_db_before_any_redis_command(monkeypatch):
    live = _client_on(0)

    async def fake_pool(*args, **kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(ai_api_harness.asyncpg, "create_pool", fake_pool)
    monkeypatch.setattr(ai_api_harness.aioredis, "from_url", lambda *a, **k: live)
    with pytest.raises(RuntimeError, match="live stack"):
        asyncio.run(ai_api_harness.make_harness("postgresql://unused/db"))
    assert live.calls == []


def test_close_harness_refuses_a_live_db_before_deleting(monkeypatch):
    live = _client_on(0)
    with pytest.raises(RuntimeError, match="live stack"):
        asyncio.run(ai_api_harness.close_harness(SimpleNamespace(redis=live, pool=None)))
    assert live.calls == []
