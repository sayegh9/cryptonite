"""Shared pytest fixtures for Cryptonite test suite."""

import json
import os
import tempfile
import time
from decimal import Decimal
from pathlib import Path

import pytest
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
