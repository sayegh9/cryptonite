"""Throwaway Postgres database migrated to Alembic head, for tests needing the latest schema.

The shared dev DB (DATABASE_URL) is not guaranteed to be at head, so these tests create their
own database, run `alembic upgrade head` against it, and drop it afterwards.
"""

import asyncio
import contextlib
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from connections import DB_URL

REPO_ROOT = Path(__file__).resolve().parents[1]


async def _admin(sql: str) -> None:
    conn = await asyncpg.connect(DB_URL)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@contextlib.contextmanager
def migrated_database(prefix: str = "cryptonite_test") -> Iterator[str]:
    """Yield the URL of a fresh database at Alembic head; drop it on exit."""
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    url = urlunsplit(urlsplit(DB_URL)._replace(path=f"/{name}"))
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", "upgrade", "head"],
            cwd=REPO_ROOT, env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True,
        )
        yield url
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
