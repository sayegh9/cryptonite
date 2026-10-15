"""Onboarded symbols survive ingest restarts (WS-1A).

Onboarding a symbol from the Watchlist inserts an ``instruments`` row and publishes an update. The running
recorder adds the symbol live, but on start it used to read ``INGEST_SYMBOLS`` only, so every restart
silently dropped onboarded symbols and their mandates and stops ran on stale prices. On start the recorder
now merges the active TRADING SPOT BINANCE rows into its symbol list, capped by ``INGEST_MAX_SYMBOLS``.
"""

import logging
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import cryptonite_ingest.recorder as recorder_module
import pytest
from connections import DB_URL
from cryptonite_ingest.recorder import IngestRecorder

# Fake symbols that cannot collide with real instruments in the throwaway test DB
ACTIVE = "ZZTEST1USDT"
ACTIVE_2 = "ZZTEST2USDT"
ACTIVE_3 = "ZZTEST3USDT"
BREAK = "ZZTEST4USDT"
INACTIVE = "ZZTEST5USDT"
PERP = "ZZTEST6USDT"
OTHER_VENUE = "ZZTEST7USDT"
BAD = "bad sym"
TEST_SYMBOLS = [ACTIVE, ACTIVE_2, ACTIVE_3, BREAK, INACTIVE, PERP, OTHER_VENUE, BAD]
CLEANUP_SQL = "DELETE FROM instruments WHERE raw_symbol = ANY($1::text[])"


async def insert_instrument(
    pool: asyncpg.Pool,
    raw_symbol: str,
    *,
    venue: str = "BINANCE",
    market_type: str = "SPOT",
    status: str = "TRADING",
    is_active: bool = True,
) -> None:
    await pool.execute(
        """
        INSERT INTO instruments (instrument_id, raw_symbol, venue, market_type, base_currency,
            quote_currency, price_precision, size_precision, price_increment, size_increment,
            status, is_active)
        VALUES ($1, $2, $3, $4, 'ZZ', 'USDT', 2, 6, $5, $5, $6, $7)
        """,
        f"{raw_symbol}.{venue}.{market_type}",
        raw_symbol,
        venue,
        market_type,
        Decimal("0.01"),
        status,
        is_active,
    )


async def seed_rows(pool: asyncpg.Pool) -> None:
    await insert_instrument(pool, ACTIVE)
    await insert_instrument(pool, ACTIVE_2)
    await insert_instrument(pool, ACTIVE_3)
    await insert_instrument(pool, BREAK, status="BREAK")
    await insert_instrument(pool, INACTIVE, is_active=False)
    await insert_instrument(pool, PERP, market_type="PERP")
    await insert_instrument(pool, OTHER_VENUE, venue="BYBIT")


@pytest.fixture
async def db_pool():
    created = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    await created.execute(CLEANUP_SQL, TEST_SYMBOLS)  # leftovers from an interrupted run
    try:
        yield created
    finally:
        await created.execute(CLEANUP_SQL, TEST_SYMBOLS)
        await created.close()


@pytest.fixture
def stub_startup(monkeypatch):
    """Replace the coordinator and recovery manager so initialize() touches no lock or REST API."""
    coordinator = MagicMock()
    coordinator.acquire_process_lock = AsyncMock()
    coordinator.reconcile_pending_batches = AsyncMock()
    monkeypatch.setattr(recorder_module, "TwoStoreCoordinator", MagicMock(return_value=coordinator))
    monkeypatch.setattr(recorder_module, "GapRecoveryManager", MagicMock())
    return coordinator


def listing_sync(recorder: IngestRecorder, not_listed=(), not_trading=()):
    """Stand-in for sync_instruments: records metadata for every symbol except the ones Binance does not list."""

    async def fake_sync(symbols=None):
        for sym in recorder.symbols:
            if sym not in not_listed:
                recorder._symbol_metadata[sym] = {"symbol": sym, "is_trading": sym not in not_trading}

    return fake_sync


def make_recorder(tmp_path, symbols=("BTCUSDT", "ETHUSDT")) -> IngestRecorder:
    return IngestRecorder(
        symbols=list(symbols),
        timeframes=["1m"],
        nautilus_catalog_path=str(tmp_path / "nautilus"),
        analytics_lake_path=str(tmp_path / "analytics"),
        database_url=DB_URL,
    )


async def test_loads_only_active_trading_spot_binance_rows(db_pool, tmp_path):
    await seed_rows(db_pool)
    recorder = make_recorder(tmp_path)
    recorder.pool = db_pool

    loaded = await recorder._load_onboarded_symbols()

    assert ACTIVE in loaded
    assert ACTIVE_2 in loaded
    assert ACTIVE_3 in loaded
    for excluded in (BREAK, INACTIVE, PERP, OTHER_VENUE):
        assert excluded not in loaded
    assert loaded == sorted(loaded)


async def test_invalid_raw_symbol_is_skipped(db_pool, tmp_path):
    await insert_instrument(db_pool, BAD)
    await insert_instrument(db_pool, ACTIVE)
    recorder = make_recorder(tmp_path)
    recorder.pool = db_pool

    loaded = await recorder._load_onboarded_symbols()

    assert BAD not in loaded
    assert ACTIVE in loaded


async def test_without_pool_returns_empty_list(tmp_path):
    recorder = make_recorder(tmp_path)
    assert recorder.pool is None

    assert await recorder._load_onboarded_symbols() == []


async def test_fetch_failure_returns_empty_list_and_warns(tmp_path, caplog):
    recorder = make_recorder(tmp_path)
    recorder.pool = MagicMock()
    recorder.pool.fetch = AsyncMock(side_effect=OSError("db down"))

    with caplog.at_level(logging.WARNING, logger="cryptonite.recorder"):
        assert await recorder._load_onboarded_symbols() == []

    assert "db down" in caplog.text


def test_merge_keeps_env_order_first_and_dedupes(tmp_path):
    recorder = make_recorder(tmp_path)

    merged = recorder._merged_symbols(
        ["BTCUSDT", "ETHUSDT"], ["ETHUSDT", "XRPUSDT", "BTCUSDT", "SOLUSDT"]
    )

    assert merged == ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"]


def test_cap_drops_last_onboarded_symbol_and_warns(tmp_path, caplog):
    recorder = make_recorder(tmp_path)
    recorder.max_symbols = 4

    with caplog.at_level(logging.WARNING, logger="cryptonite.recorder"):
        merged = recorder._merged_symbols(
            ["BTCUSDT", "ETHUSDT"], ["XRPUSDT", "SOLUSDT", "ADAUSDT"]
        )

    assert merged == ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT"]
    assert "ADAUSDT" in caplog.text


def test_cap_never_truncates_the_env_list(tmp_path, caplog):
    recorder = make_recorder(tmp_path)
    recorder.max_symbols = 2

    with caplog.at_level(logging.WARNING, logger="cryptonite.recorder"):
        merged = recorder._merged_symbols(["BTCUSDT", "ETHUSDT", "XRPUSDT"], ["SOLUSDT"])

    assert merged == ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    assert "SOLUSDT" in caplog.text


async def test_disabled_flag_keeps_env_list_and_skips_db(tmp_path):
    recorder = make_recorder(tmp_path)
    recorder.onboarded_from_db = False
    recorder.pool = MagicMock()
    recorder.pool.fetch = AsyncMock(return_value=[])

    await recorder._apply_onboarded_symbols()

    assert recorder.symbols == ["BTCUSDT", "ETHUSDT"]
    recorder.pool.fetch.assert_not_awaited()


async def test_initialize_starts_from_merged_symbols(db_pool, tmp_path, stub_startup, monkeypatch):
    await seed_rows(db_pool)
    recorder = make_recorder(tmp_path, symbols=["BTCUSDT"])
    recorder.max_symbols = 1000  # the test DB may hold other rows; only containment is asserted
    symbols_at_sync: list[list[str]] = []
    listed = listing_sync(recorder)

    async def recording_sync(symbols=None):
        symbols_at_sync.append(list(recorder.symbols))
        await listed(symbols)

    monkeypatch.setattr(recorder, "sync_instruments", recording_sync)
    try:
        await recorder.initialize()
    finally:
        if recorder.pool:
            await recorder.pool.close()

    assert recorder.symbols[0] == "BTCUSDT"
    assert ACTIVE in recorder.symbols
    assert ACTIVE_3 in recorder.symbols
    assert BREAK not in recorder.symbols
    # merged before the instrument sync and the reconcile; nothing listed was dropped after the sync
    assert set(recorder.symbols) <= set(symbols_at_sync[0])
    stub_startup.reconcile_pending_batches.assert_awaited()


async def test_onboarded_symbol_absent_from_exchange_info_is_dropped(
    db_pool, tmp_path, stub_startup, monkeypatch, caplog
):
    await insert_instrument(db_pool, ACTIVE)  # delisted on Binance: no exchangeInfo entry
    await insert_instrument(db_pool, ACTIVE_2)  # still listed
    recorder = make_recorder(tmp_path, symbols=["BTCUSDT"])
    recorder.max_symbols = 1000
    monkeypatch.setattr(recorder, "sync_instruments", listing_sync(recorder, not_listed=(ACTIVE,)))
    try:
        with caplog.at_level(logging.WARNING, logger="cryptonite.recorder"):
            await recorder.initialize()
    finally:
        if recorder.pool:
            await recorder.pool.close()

    assert ACTIVE not in recorder.symbols
    assert ACTIVE in caplog.text  # the warning names the dropped symbol
    assert ACTIVE_2 in recorder.symbols  # a present onboarded symbol is kept
    assert "BTCUSDT" in recorder.symbols


async def test_onboarded_symbol_not_trading_on_binance_is_dropped(
    db_pool, tmp_path, stub_startup, monkeypatch
):
    await insert_instrument(db_pool, ACTIVE_3)  # listed but status is not TRADING any more
    recorder = make_recorder(tmp_path, symbols=["BTCUSDT"])
    recorder.max_symbols = 1000
    monkeypatch.setattr(recorder, "sync_instruments", listing_sync(recorder, not_trading=(ACTIVE_3,)))
    try:
        await recorder.initialize()
    finally:
        if recorder.pool:
            await recorder.pool.close()

    assert ACTIVE_3 not in recorder.symbols
    assert "BTCUSDT" in recorder.symbols


async def test_env_symbol_absent_from_exchange_info_is_kept(db_pool, tmp_path, stub_startup, monkeypatch):
    recorder = make_recorder(tmp_path, symbols=["BTCUSDT", "ETHUSDT"])
    monkeypatch.setattr(recorder, "sync_instruments", listing_sync(recorder, not_listed=("ETHUSDT",)))
    try:
        await recorder.initialize()
    finally:
        if recorder.pool:
            await recorder.pool.close()

    assert "ETHUSDT" in recorder.symbols  # env symbols are never dropped by the listing check
    assert "BTCUSDT" in recorder.symbols


async def test_initialize_with_flag_off_keeps_env_list(db_pool, tmp_path, stub_startup, monkeypatch):
    await seed_rows(db_pool)
    recorder = make_recorder(tmp_path, symbols=["BTCUSDT"])
    recorder.onboarded_from_db = False
    monkeypatch.setattr(recorder, "sync_instruments", AsyncMock())
    try:
        await recorder.initialize()
    finally:
        if recorder.pool:
            await recorder.pool.close()

    assert recorder.symbols == ["BTCUSDT"]


def test_env_vars_are_read_in_init(tmp_path, monkeypatch):
    monkeypatch.setenv("INGEST_ONBOARDED_FROM_DB", "0")
    monkeypatch.setenv("INGEST_MAX_SYMBOLS", "7")
    recorder = make_recorder(tmp_path)
    assert recorder.onboarded_from_db is False
    assert recorder.max_symbols == 7

    monkeypatch.delenv("INGEST_ONBOARDED_FROM_DB")
    monkeypatch.delenv("INGEST_MAX_SYMBOLS")
    recorder = make_recorder(tmp_path)
    assert recorder.onboarded_from_db is True
    assert recorder.max_symbols == 12
