"""Shared Postgres/Redis connection strings for integration tests.

Reads DATABASE_URL / REDIS_URL so tests run both on the host (localhost defaults)
and inside the compose `test` service (postgres/redis hostnames).
"""

import os
from urllib.parse import urlsplit, urlunsplit

DB_URL = os.getenv(
    "DATABASE_URL", "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"
)
# Tests share the Redis server with the live stack, whose DB 0 holds the event bus and the heartbeat keys
# the API and healthchecks read (exit_monitor/notifier/learner:heartbeat), and DB 1 the agent sessions.
# A suite run there masked a dead exit monitor for 5 minutes (it SETs exit_monitor:heartbeat) and
# DELeted live heartbeats. Tests therefore always use their own DB index, whatever REDIS_URL says.
LIVE_REDIS_DBS = frozenset({0, 1})
TEST_REDIS_DB = int(os.getenv("CRYPTONITE_TEST_REDIS_DB", "15"))


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


REDIS_URL = isolated_redis_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))


def role_db_url(user: str, password: str) -> str:
    """Same host/port/db as DB_URL, authenticated as a least-privilege role."""
    parts = urlsplit(DB_URL)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{host}"))
