"""Migration 051: isolated margin persistence, exact interest, grants and downgrade safety."""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg
import pytest
from migrated_db import REPO_ROOT, migrated_database, seed_paper_account


def _alembic(url: str, *args: str, check: bool = True):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        check=check,
        capture_output=True,
        text=True,
    )


def _out(proc) -> str:
    return proc.stderr + proc.stdout


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


def _seed_account(url: str):
    async def go():
        conn = await asyncpg.connect(url)
        try:
            await seed_paper_account(conn)
        finally:
            await conn.close()
    asyncio.run(go())


@pytest.fixture
def db050():
    with migrated_database("cryptonite_m051") as url:
        _alembic(url, "downgrade", "050")
        yield url


def test_upgrade_schema_constraints_grants_and_down_up_round_trip(db050):
    url = db050
    _alembic(url, "upgrade", "051")

    assert _val(url, "SELECT version_num FROM alembic_version") == "051"
    columns = {row["column_name"]: row for row in _run(
        url,
        "SELECT column_name, data_type, numeric_precision, numeric_scale "
        "FROM information_schema.columns WHERE table_name='paper_margin_accounts'",
    )}
    for name in ("base_qty", "quote_qty", "borrowed_base", "borrowed_quote", "interest_base", "interest_quote"):
        assert columns[name]["data_type"] == "numeric", name
        assert columns[name]["numeric_precision"] is None, name
        assert columns[name]["numeric_scale"] is None, name
    interest_columns = {row["column_name"]: row for row in _run(
        url,
        "SELECT column_name, data_type, numeric_precision, numeric_scale "
        "FROM information_schema.columns WHERE table_name='paper_margin_interest'",
    )}
    for name in ("principal", "hourly_rate", "amount"):
        assert interest_columns[name]["data_type"] == "numeric", name
        assert interest_columns[name]["numeric_precision"] is None, name
        assert interest_columns[name]["numeric_scale"] is None, name
    index_defs = [row["indexdef"] for row in _run(
        url, "SELECT indexdef FROM pg_indexes WHERE tablename IN ('paper_margin_accounts','paper_margin_interest')",
    )]
    assert any("UNIQUE" in definition and "account_id, pair" in definition and "WHERE" in definition
               and "'OPEN'" in definition
               for definition in index_defs)
    assert any("UNIQUE" in definition and "margin_account_id, period_end, asset" in definition
               for definition in index_defs)
    assert _val(url, "SELECT has_table_privilege('cryptonite_scanner', 'ai_mandates', 'SELECT')") is True
    assert _val(url, "SELECT has_table_privilege('cryptonite_scanner', 'ai_profile_coin_locks', 'SELECT')") is True
    assert _val(url, "SELECT has_table_privilege('cryptonite_scanner', 'instruments', 'SELECT')") is True
    assert _val(url, "SELECT has_table_privilege('cryptonite_scanner', 'ai_profile_decisions', 'INSERT')") is True
    assert _val(url, "SELECT has_sequence_privilege('cryptonite_scanner', 'ai_profile_decisions_id_seq', 'USAGE')") is True

    # A clean schema can move down and up again.
    _alembic(url, "downgrade", "050")
    assert _val(url, "SELECT has_table_privilege('cryptonite_scanner', 'instruments', 'SELECT')") is True
    assert _val(url, "SELECT to_regclass('paper_margin_accounts')") is None
    _alembic(url, "upgrade", "051")
    assert _val(url, "SELECT to_regclass('paper_margin_interest')") == "paper_margin_interest"


def test_decimal_balances_open_pair_uniqueness_and_interest_idempotency(db050):
    url = db050
    _alembic(url, "upgrade", "051")
    _seed_account(url)
    margin_id = _val(url, """
        INSERT INTO paper_margin_accounts
            (account_id, pair, collateral_usdt, base_qty, quote_qty, borrowed_base, borrowed_quote,
             interest_base, interest_quote)
        VALUES ('PAPER-ACCOUNT-001', 'BTCUSDT', 10.000000000000000001, 0.123456789012345678,
                12.345678901234567890, 0.01, 0.02, 0.000000000000000001, 0.000000000000000002)
        RETURNING id
    """)
    row = _run(url, "SELECT * FROM paper_margin_accounts WHERE id=$1", margin_id)[0]
    assert row["collateral_usdt"].as_tuple().exponent == -18
    assert str(row["base_qty"]) == "0.123456789012345678"
    assert str(row["interest_quote"]) == "2E-18"

    with pytest.raises(asyncpg.UniqueViolationError):
        _run(url, "INSERT INTO paper_margin_accounts (account_id, pair) VALUES ('PAPER-ACCOUNT-001','BTCUSDT')")

    period_start = datetime(2026, 10, 29, 12, 20, tzinfo=UTC)
    period_end = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)
    args = (margin_id, period_start, period_end, "USDT", "1.25", "0.00000656", "0.000001312")
    insert_charge = """
            INSERT INTO paper_margin_interest
                (margin_account_id, period_start, period_end, asset, principal, hourly_rate, amount)
            VALUES ($1, $2::timestamptz, $3::timestamptz, $4, $5::numeric, $6::numeric, $7::numeric)
            ON CONFLICT (margin_account_id, period_end, asset) DO NOTHING
        """

    async def concurrent_passes():
        async def insert_once():
            conn = await asyncpg.connect(url)
            try:
                return await conn.execute(insert_charge, *args)
            finally:
                await conn.close()
        return await asyncio.gather(insert_once(), insert_once())

    race_results = asyncio.run(concurrent_passes())
    assert sorted(race_results) == ["INSERT 0 0", "INSERT 0 1"]
    # A process restart repeats the same settlement insert; the persisted unique key turns it into a no-op.
    assert _exec(url, insert_charge, *args) == "INSERT 0 0"
    assert _val(url, "SELECT sum(amount) FROM paper_margin_interest WHERE margin_account_id=$1", margin_id) == Decimal("0.000001312")


def test_margin_positions_allow_isolated_short_but_plain_spot_stays_long_only(db050):
    url = db050
    _alembic(url, "upgrade", "051")
    _seed_account(url)
    base = ("margin-short", "PAPER-ACCOUNT-001", "MANUAL_TRADE", "BTCUSDT-MARGIN.BINANCE", "SHORT", 100,
            0.1, 90, "OPEN")
    _run(url, """
        INSERT INTO paper_positions
            (position_id, account_id, instance_id, instrument_id, side, entry_price, quantity, current_price, status,
             leverage, margin_mode, isolated_margin)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,5,'ISOLATED',10)
    """, *base)
    plain_spot = ("spot-short", "PAPER-ACCOUNT-001", "MANUAL_TRADE", "BTCUSDT.BINANCE", "SHORT", 100, 0.1, 90,
                  "OPEN")
    with pytest.raises(asyncpg.CheckViolationError):
        _run(url, """
            INSERT INTO paper_positions
                (position_id, account_id, instance_id, instrument_id, side, entry_price, quantity, current_price, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, *plain_spot)


def test_downgrade_refuses_when_a_margin_balance_or_position_exists(db050):
    url = db050
    _alembic(url, "upgrade", "051")
    _seed_account(url)
    _run(url, "INSERT INTO paper_margin_accounts (account_id, pair, collateral_usdt) VALUES ('PAPER-ACCOUNT-001','BTCUSDT',1)")
    down = _alembic(url, "downgrade", "050", check=False)
    assert down.returncode != 0
    assert "Cannot downgrade 051: open margin accounts, margin balances, or margin positions exist" in _out(down)
    assert _val(url, "SELECT version_num FROM alembic_version") == "051"


def test_downgrade_refuses_margin_position_even_when_account_table_is_empty(db050):
    url = db050
    _alembic(url, "upgrade", "051")
    _seed_account(url)
    _run(url, """
        INSERT INTO paper_positions
            (position_id, account_id, instance_id, instrument_id, side, entry_price, quantity, current_price, status,
             leverage, margin_mode, isolated_margin)
        VALUES ('margin-history','PAPER-ACCOUNT-001','MANUAL_TRADE','BTCUSDT-MARGIN.BINANCE','LONG',100,0,100,'CLOSED',
                1,'ISOLATED',0)
    """)
    down = _alembic(url, "downgrade", "050", check=False)
    assert down.returncode != 0
    assert "Cannot downgrade 051" in _out(down)
    assert _val(url, "SELECT version_num FROM alembic_version") == "051"
