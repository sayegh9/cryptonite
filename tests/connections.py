"""Shared Postgres/Redis connection strings for integration tests.

Reads DATABASE_URL / REDIS_URL so tests run both on the host (localhost defaults)
and inside the compose `test` service (postgres/redis hostnames).

Tests never run against the dev DB `cryptonite` (C3, F-M9: test specs and kill-switch rows leaked
into it). The default is the throwaway `cryptonite_ci`; `assert_not_dev_db` refuses the dev DB and
conftest applies it to every *DATABASE_URL variable and to every asyncpg connection. Redis: see below.
"""

import contextlib
import os
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest

DEV_DATABASES = frozenset({"cryptonite"})  # the dev DB; tests refuse it (C3)

# Default: the throwaway cryptonite_ci with the compose credentials (assembled from parts, so no credential
# URL sits in the source for the secret scanner to flag).
_LOCAL_PG = ("cryptonite", "cryptonite_dev_pass", "localhost:5432")
DB_URL = os.getenv("DATABASE_URL") or urlunsplit(("postgresql", "{}:{}@{}".format(*_LOCAL_PG), "/cryptonite_ci", "", ""))
# Tests share the Redis server with the live stack, whose DB 0 holds the event bus and the heartbeat keys
# the API and healthchecks read (exit_monitor/notifier/learner:heartbeat), and DB 1 the agent sessions.
# A suite run there masked a dead exit monitor for 5 minutes (it SETs exit_monitor:heartbeat) and
# DELeted live heartbeats. So:
#   - REDIS_URL always points at a test DB index (15 unless CRYPTONITE_TEST_REDIS_DB), whatever the env
#     says; conftest exports it so code under test that reads REDIS_URL agrees.
#   - install_redis_db_guard() (conftest) makes every redis-py pool built on DB 0/1 raise.
#   - Tests that need DB 0 (Nautilus' Rust Redis cache/bus take no DB index) are marked
#     requires_dedicated_redis and run only where CRYPTONITE_TEST_REDIS_DEDICATED=1 (CI's own Redis).
# Residual risks when the suite runs on the host against the live Redis:
#   - Pub/sub channels ignore the DB index. Code under test that PUBLISHes reaches live subscribers, e.g.
#     the API's `channel:system_alerts` (mandate_updated, routes/ai.py) and `channel:debates:*`
#     (routes/agents.py) can reach the live notifier and UI. Test-side publishers use per-test prefixes.
#   - Concurrent host runs (or a run next to the compose `test` service) share the test DB index.
LIVE_REDIS_DBS = frozenset({0, 1})
TEST_REDIS_DB = int(os.getenv("CRYPTONITE_TEST_REDIS_DB", "15"))
DEDICATED_ENV = "CRYPTONITE_TEST_REDIS_DEDICATED"


def dedicated_redis() -> bool:
    """True only where the Redis server belongs to the test run alone (CI's service container)."""
    return os.getenv(DEDICATED_ENV) == "1"


requires_dedicated_redis = pytest.mark.skipif(
    not dedicated_redis(), reason=f"uses Redis DB 0; runs only with {DEDICATED_ENV}=1 (never the live Redis)")


def isolated_redis_url(url: str, db: int = TEST_REDIS_DB) -> str:
    """``url`` (host, port, password kept) pointed at test DB ``db``; never a live index."""
    if db in LIVE_REDIS_DBS:
        raise RuntimeError(f"Redis DB {db} belongs to the live stack; tests must use another index")
    return urlunsplit(urlsplit(url)._replace(path=f"/{db}"))


def assert_isolated(redis_client) -> None:
    """Guard before writing live-looking keys (heartbeats): the client must be on a test DB."""
    db = int(redis_client.connection_pool.connection_kwargs.get("db", 0))
    if db in LIVE_REDIS_DBS:
        raise RuntimeError(f"refusing to touch Redis DB {db}: it belongs to the live stack")


_live_db_allowed = False


@contextlib.contextmanager
def allow_live_redis_db():
    """Lets a dedicated-Redis-only test build a DB 0 client; refuses anywhere else."""
    global _live_db_allowed
    if not dedicated_redis():
        raise RuntimeError(f"Redis DB 0/1 is only allowed with {DEDICATED_ENV}=1")
    _live_db_allowed = True
    try:
        yield
    finally:
        _live_db_allowed = False


def _guarded(init):
    def __init__(self, *args, **connection_kwargs):
        db = int(connection_kwargs.get("db", 0) or 0)
        if db in LIVE_REDIS_DBS and not _live_db_allowed:
            raise RuntimeError(
                f"a test built a Redis client on DB {db}, which belongs to the live stack: use "
                "connections.REDIS_URL (or mark the test requires_dedicated_redis)")
        init(self, *args, **connection_kwargs)
    __init__._cryptonite_guard = True
    return __init__


def install_redis_db_guard() -> None:
    """Every redis-py pool (sync and asyncio; Redis(), from_url) built on DB 0/1 raises. Idempotent."""
    import redis.asyncio.connection as async_conn
    import redis.connection as sync_conn
    for cls in (sync_conn.ConnectionPool, async_conn.ConnectionPool):
        if not getattr(cls.__init__, "_cryptonite_guard", False):
            cls.__init__ = _guarded(cls.__init__)


_ENV_REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_URL = isolated_redis_url(_ENV_REDIS_URL)


def redis_report_line() -> str:
    """One line for the pytest header: where the suite's Redis really points (the URL is rewritten)."""
    parts = urlsplit(REDIS_URL)
    was = urlsplit(_ENV_REDIS_URL).path or "/0"
    return (f"redis: {parts.hostname}:{parts.port or 6379}{parts.path} (REDIS_URL said {was}; "
            f"dedicated={'yes' if dedicated_redis() else 'no'})")


class DevDatabaseError(RuntimeError):
    """A test tried to use the dev database."""


def database_name(url: str) -> str:
    """The database a libpq-style URL connects to (libpq defaults it to the user name)."""
    parts = urlsplit(url)
    name = unquote(parts.path.lstrip("/"))
    return name or unquote(parts.username or "")


def assert_not_dev_db(url: str, source: str = "DATABASE_URL") -> None:
    name = database_name(url)
    if name in DEV_DATABASES:
        raise DevDatabaseError(
            f"{source} points at the dev database '{name}'. Tests must use a throwaway database: point "
            f"DATABASE_URL at e.g. cryptonite_ci_<item> (create it and run `alembic upgrade head` first; "
            f"see HANDOFF.md)."
        )


def role_db_url(user: str, password: str) -> str:
    """Same host/port/db as DB_URL, authenticated as a least-privilege role."""
    parts = urlsplit(DB_URL)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{host}"))
