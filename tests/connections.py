"""Shared Postgres/Redis connection strings for integration tests.

Reads DATABASE_URL / REDIS_URL so tests run both on the host (localhost defaults)
and inside the compose `test` service (postgres/redis hostnames).
"""

import os
from urllib.parse import urlsplit, urlunsplit

DB_URL = os.getenv(
    "DATABASE_URL", "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")


def role_db_url(user: str, password: str) -> str:
    """Same host/port/db as DB_URL, authenticated as a least-privilege role."""
    parts = urlsplit(DB_URL)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{host}"))
