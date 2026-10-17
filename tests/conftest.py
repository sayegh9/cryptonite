"""Shared pytest fixtures for Cryptonite test suite."""

import json
import os
import tempfile
import time
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
from connections import (
    DB_URL,
    DEV_DATABASES,
    REDIS_URL,
    DevDatabaseError,
    assert_not_dev_db,
    install_redis_db_guard,
    redis_report_line,
)
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_schemas.identifiers import (
    to_nautilus_bar_type,
    to_nautilus_instrument_id,
    to_spot_instrument_id,
)
from nautilus_trader.model import (
    Currency,
    CurrencyPair,
    Price,
    Quantity,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# The API may fall back to the all-zero dev TOTP key only in dev/test (R7.10); tests are "test"
os.environ.setdefault("CRYPTONITE_ENV", "test")
# Code under test that reads REDIS_URL itself must land on the same isolated test DB (connections.py).
os.environ["REDIS_URL"] = REDIS_URL
install_redis_db_guard()  # any redis-py client on the live DBs 0/1 raises (connections.py)


def pytest_report_header(config):
    return redis_report_line()  # REDIS_URL is rewritten to the test DB: say so once per run

# Never the dev DB (C3, F-M9). Services default to `cryptonite` when DATABASE_URL is unset, so pin the
# test default before any service module is imported; then refuse a dev-DB URL in any *DATABASE_URL
# variable (pytest_configure) and any asyncpg connection that lands on it, however its URL was built.
os.environ.setdefault("DATABASE_URL", DB_URL)
os.environ.setdefault("ASYNC_DATABASE_URL", DB_URL.replace("postgresql://", "postgresql+asyncpg://", 1))


def dev_db_env_vars(environ=os.environ) -> list[str]:
    bad = []
    for key, value in sorted(environ.items()):
        if key.endswith("DATABASE_URL") and value:
            try:
                assert_not_dev_db(value.replace("+asyncpg", ""), key)
            except DevDatabaseError:
                bad.append(key)
    return bad


def pytest_configure(config):
    bad = dev_db_env_vars()
    if bad:
        raise pytest.UsageError(
            f"Refusing to run tests: {', '.join(bad)} point(s) at the dev database "
            f"({', '.join(sorted(DEV_DATABASES))}). Use a throwaway database such as cryptonite_ci_<item>."
        )


_asyncpg_connect = asyncpg.connection.connect


async def _connect_never_dev_db(*args, **kwargs):
    con = await _asyncpg_connect(*args, **kwargs)
    name = await con.fetchval("SELECT current_database()")
    if name in DEV_DATABASES:
        await con.close()
        raise DevDatabaseError(f"A test connected to the dev database '{name}'; tests must use a throwaway database.")
    return con


# asyncpg.create_pool resolves asyncpg.connection.connect when the pool is built, so both paths are covered
asyncpg.connection.connect = _connect_never_dev_db
asyncpg.connect = _connect_never_dev_db


@pytest.fixture(autouse=True)
def no_paid_model_calls(monkeypatch):
    """Tests use pydantic-ai test models; without a key any stray Gemini call fails instead of billing."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


@pytest.fixture
def temp_nautilus_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def temp_analytics_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def sample_exchange_info():
    with open(FIXTURES_DIR / "exchange_info_spot.json") as f:
        return json.load(f)


@pytest.fixture
def sample_raw_klines():
    with open(FIXTURES_DIR / "klines_sample.json") as f:
        rows = json.load(f)
    return [
        RawBinanceKline(
            open_time_ms=int(r[0]),
            open=str(r[1]),
            high=str(r[2]),
            low=str(r[3]),
            close=str(r[4]),
            volume=str(r[5]),
            close_time_ms=int(r[6]),
            quote_volume=str(r[7]),
            trades_count=int(r[8]),
            taker_buy_base_volume=str(r[9]),
            taker_buy_quote_volume=str(r[10]),
            is_closed=True,
        )
        for r in rows
    ]


@pytest.fixture
def sample_currency_pair():
    inst_id = to_nautilus_instrument_id(to_spot_instrument_id("BTCUSDT"))
    return CurrencyPair(
        instrument_id=inst_id,
        raw_symbol=inst_id.symbol,
        base_currency=Currency.from_str("BTC"),
        quote_currency=Currency.from_str("USDT"),
        price_precision=2,
        size_precision=5,
        price_increment=Price(Decimal("0.01"), 2),
        size_increment=Quantity(Decimal("0.00001"), 5),
        ts_event=1725700000000000000,
        ts_init=1725700000000000000,
    )


@pytest.fixture
def sample_enriched_bars(sample_raw_klines):
    inst_id = to_spot_instrument_id("BTCUSDT")
    bar_type = to_nautilus_bar_type(inst_id, "1m")
    arr_ns = time.time_ns()
    return [
        EnrichedBar.from_binance_raw(
            raw=k,
            instrument_id=inst_id,
            bar_type=str(bar_type),
            price_precision=2,
            size_precision=5,
            received_at_ns=arr_ns,
        )
        for k in sample_raw_klines
    ]


@pytest.fixture
def operator_auth():
    """Authenticate every request to the shared API app as an operator (auth is tested in test_route_auth_guards)."""
    from cryptonite_api.main import app
    from cryptonite_api.routes.auth import require_auth, require_csrf, require_operator

    session = {"type": "cookie", "role": "operator", "user_id": "test-operator"}
    deps = (require_auth, require_operator, require_csrf)
    for dep in deps:
        app.dependency_overrides[dep] = lambda: session
    yield session
    for dep in deps:
        app.dependency_overrides.pop(dep, None)


@pytest.fixture(autouse=True)
def reset_klines_shared_weight_state():
    """Reset the get_klines module-level state between tests.

    The pre-flight weight gate (review proposal for PR #46) and the cached in_feed universe both
    keep module-level state, so they must be isolated per test; otherwise one test's 3005 reading
    would trip the gate inside its neighbor, or a cached universe could leak between tests.
    The USD-M state (weight, Retry-After pause, 30 s answer cache) and the regime answer cache are reset the same way.
    """
    import cryptonite_agents.tools as _tools

    old = (
        _tools._LAST_USED_WEIGHT,
        _tools._LAST_WEIGHT_AT,
        _tools._KLINES_CACHE.copy(),
        _tools._IN_FEED_CACHE,
        _tools._FAPI_CACHE.copy(),
        _tools._FAPI_LAST_USED_WEIGHT,
        _tools._FAPI_LAST_WEIGHT_AT,
        _tools._FAPI_BLOCKED_UNTIL,
        _tools._REGIME_CACHE.copy(),
    )
    _tools._LAST_USED_WEIGHT = 0
    _tools._LAST_WEIGHT_AT = 0.0
    _tools._KLINES_CACHE.clear()
    _tools._IN_FEED_CACHE = None
    _tools._FAPI_CACHE.clear()
    _tools._FAPI_LAST_USED_WEIGHT = 0
    _tools._FAPI_LAST_WEIGHT_AT = 0.0
    _tools._FAPI_BLOCKED_UNTIL = 0.0
    _tools._REGIME_CACHE.clear()
    yield
    (
        _tools._LAST_USED_WEIGHT,
        _tools._LAST_WEIGHT_AT,
        klines,
        _tools._IN_FEED_CACHE,
        fapi,
        _tools._FAPI_LAST_USED_WEIGHT,
        _tools._FAPI_LAST_WEIGHT_AT,
        _tools._FAPI_BLOCKED_UNTIL,
        regime,
    ) = old
    _tools._KLINES_CACHE.clear()
    _tools._KLINES_CACHE.update(klines)
    _tools._FAPI_CACHE.clear()
    _tools._FAPI_CACHE.update(fapi)
    _tools._REGIME_CACHE.clear()
    _tools._REGIME_CACHE.update(regime)
