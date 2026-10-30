"""Migration 052 grants the debate worker only the missing exit-decision ACLs."""

import asyncio
import os
import subprocess
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from migrated_db import REPO_ROOT, migrated_database


def _alembic(url: str, *args: str, check: bool = True):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        check=check,
        capture_output=True,
        text=True,
    )


def _as_api(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"cryptonite_api:api_dev_pass@{parts.netloc.rsplit('@', 1)[-1]}"))


def _run(url: str, sql: str, *args):
    async def go():
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(go())


def _exec(url: str, sql: str, *args):
    async def go():
        conn = await asyncpg.connect(url)
        try:
            return await conn.execute(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(go())


def _val(url: str, sql: str, *args):
    rows = _run(url, sql, *args)
    return rows[0][0] if rows else None


@pytest.fixture
def db051():
    with migrated_database("cryptonite_m052") as url:
        _alembic(url, "downgrade", "051")
        yield url


def test_upgrade_downgrade_upgrade_grants_only_052_privileges(db051):
    url = db051
    api_url = _as_api(url)
    assert _val(url, "SELECT version_num FROM alembic_version") == "051"
    assert _val(url, "SELECT pg_get_serial_sequence('public.exit_decisions', 'id')") is None

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        _exec(api_url, "INSERT INTO exit_decisions (id) VALUES ($1)", uuid.uuid4())
    _alembic(url, "upgrade", "052")

    assert _val(url, "SELECT version_num FROM alembic_version") == "052"
    for privilege in ("SELECT", "INSERT", "UPDATE"):
        assert _val(url, "SELECT has_table_privilege('cryptonite_api', 'exit_decisions', $1)", privilege)
    assert _exec(api_url, "INSERT INTO exit_decisions (id) VALUES ($1)", uuid.uuid4()) == "INSERT 0 1"

    _alembic(url, "downgrade", "051")
    assert _val(url, "SELECT version_num FROM alembic_version") == "051"
    for privilege in ("SELECT", "INSERT", "UPDATE"):
        assert _val(url, "SELECT has_table_privilege('cryptonite_api', 'exit_decisions', $1)", privilege) is False
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        _exec(api_url, "INSERT INTO exit_decisions (id) VALUES ($1)", uuid.uuid4())

    _alembic(url, "upgrade", "052")
    assert _val(url, "SELECT version_num FROM alembic_version") == "052"
    assert _exec(api_url, "INSERT INTO exit_decisions (id) VALUES ($1)", uuid.uuid4()) == "INSERT 0 1"


def test_downgrade_preserves_privileges_that_predate_052(db051):
    url = db051
    _exec(url, "GRANT SELECT ON TABLE exit_decisions TO cryptonite_api")
    _alembic(url, "upgrade", "052")
    _alembic(url, "downgrade", "051")

    assert _val(url, "SELECT has_table_privilege('cryptonite_api', 'exit_decisions', 'SELECT')") is True
    assert _val(url, "SELECT has_table_privilege('cryptonite_api', 'exit_decisions', 'INSERT')") is False
    assert _val(url, "SELECT has_table_privilege('cryptonite_api', 'exit_decisions', 'UPDATE')") is False
