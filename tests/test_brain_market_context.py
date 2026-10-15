"""Market context for the Brain's analysts (WS-2C): the 24h ticker's as_of, the derivatives context (mark, open
interest, funding), the market regime, and the tools' registration.

The Redis keys live on the test index (CRYPTONITE_TEST_REDIS_DB), never the live stack. The funding SQL runs as the
agent role against the test database at Alembic head, the role the tools use in production. The regime test writes a
real 1h lake (the production writer and reader) under a temporary directory.
"""

import datetime
import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg
import httpx
import pytest
import redis
from connections import DB_URL, REDIS_URL, assert_isolated, role_db_url
from cryptonite_agents import tools
from cryptonite_agents.brain.agent import BRAIN_INSTRUCTIONS
from cryptonite_agents.brain.toolset import BRAIN_TOOLS
from cryptonite_agents.gateway import RiskClass
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_schemas.identifiers import (
    to_nautilus_bar_type,
    to_perp_instrument_id,
    to_spot_instrument_id,
)
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.queries import AnalyticsQueryEngine

REPO = Path(__file__).resolve().parents[1]
AGENT_URL = role_db_url("cryptonite_agent_ro", "agent_ro_dev_pass")
UNREACHABLE_PG = "postgresql://cryptonite_agent_ro:agent_ro_dev_pass@localhost:1/cryptonite_ci_ws2c"
UNREACHABLE_REDIS = "redis://localhost:1/11"
BASE = datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)  # funding row 0; rows are 8 hours apart
HOUR_MS = 3_600_000
MARKET_CONTEXT = ("get_news_sentiment", "get_derivatives_context", "get_market_regime")


@pytest.fixture(autouse=True)
def agent_tools(monkeypatch):
    """The tools read as the agent role, the role they use in production."""
    monkeypatch.setattr(tools, "DB_URL", AGENT_URL)
    monkeypatch.setattr(tools, "_engine", None)
    yield
    if tools._engine is not None:
        tools._engine.dispose()


# --- get_live_market_data: as_of ------------------------------------------------------------------------------------

def _ticker(close_ms: int, symbol: str = "BTCUSDT") -> dict:
    return {"symbol": symbol, "lastPrice": "65000.10", "priceChangePercent": "1.5", "highPrice": "66000.00",
            "lowPrice": "64000.00", "volume": "1234.5", "closeTime": close_ms}


def test_live_market_data_rows_carry_as_of_from_the_binance_close_time(monkeypatch):
    close = datetime.datetime(2026, 10, 15, 12, 30, 5, tzinfo=datetime.UTC)
    close_ms = int(close.timestamp() * 1000)
    monkeypatch.setattr(httpx, "get", lambda url, params=None, timeout=None: httpx.Response(
        200, json=[_ticker(close_ms), _ticker(close_ms, "ETHUSDT")]))
    rows = json.loads(tools.get_live_market_data("BTCUSDT,ETHUSDT"))
    assert [r["symbol"] for r in rows] == ["BTCUSDT", "ETHUSDT"]
    assert rows[0]["as_of"] == "2026-10-15T12:30:05+00:00"
    assert set(rows[0]) == {"symbol", "last_price", "price_change_24h_pct", "high_24h", "low_24h", "volume_24h",
                            "as_of"}


def test_a_single_symbol_ticker_is_wrapped_and_still_carries_as_of(monkeypatch):
    close_ms = int(datetime.datetime(2026, 10, 15, 9, tzinfo=datetime.UTC).timestamp() * 1000)
    monkeypatch.setattr(httpx, "get", lambda url, params=None, timeout=None: httpx.Response(200, json=_ticker(close_ms)))
    rows = json.loads(tools.get_live_market_data("BTCUSDT"))
    assert rows[0]["as_of"] == "2026-10-15T09:00:00+00:00"


# --- get_derivatives_context ----------------------------------------------------------------------------------------

class PerpWorld:
    """One unique perpetual symbol: its Redis keys and funding rows, removed again in the fixture's teardown."""

    def __init__(self, conn: asyncpg.Connection, client: redis.Redis) -> None:
        self.conn = conn
        self.redis = client
        self.symbol = f"WS2C{uuid.uuid4().hex[:10].upper()}"
        self.instrument = to_perp_instrument_id(self.symbol)
        self.mark_key = f"perp:mark:{self.symbol}"
        self.oi_key = f"perp:oi:{self.symbol}"
        self.mark_payload = {"type": "perp.mark", "v": 1, "instrument_id": self.instrument, "symbol": self.symbol,
                             "ts_event_ns": 1_790_000_000_000_000_000, "mark_price": "65000.5",
                             "index_price": "65001.0", "funding_rate": "0.0001",
                             "next_funding_time_ns": 1_790_010_000_000_000_000}
        self.oi_payload = {"type": "perp.open_interest", "v": 1, "instrument_id": self.instrument,
                           "symbol": self.symbol, "open_interest": "12345.678",
                           "ts_event_ns": 1_790_000_000_000_000_000}

    def seed_redis(self) -> None:
        self.redis.set(self.mark_key, json.dumps(self.mark_payload), ex=600)
        self.redis.set(self.oi_key, json.dumps(self.oi_payload), ex=600)

    async def seed_funding(self, count: int, instrument: str | None = None) -> None:
        for i in range(count):
            await self.conn.execute(
                "INSERT INTO funding_rates (instrument_id, funding_time_ns, funding_rate, mark_price, "
                "funding_interval_hours, source) VALUES ($1, $2, $3, $4, 8, 'BINANCE_REST')",
                instrument or self.instrument, int((BASE + datetime.timedelta(hours=8 * i)).timestamp()) * 10**9,
                Decimal(i + 1) / Decimal(10000), Decimal("65000.5"),
            )

    def funding_time(self, i: int) -> str:
        return (BASE + datetime.timedelta(hours=8 * i)).isoformat(timespec="seconds")

    async def cleanup(self) -> None:
        await self.conn.execute("DELETE FROM funding_rates WHERE instrument_id = $1", self.instrument)
        self.redis.delete(self.mark_key, self.oi_key)


@pytest.fixture
async def perp():
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    assert_isolated(client)
    conn = await asyncpg.connect(DB_URL)
    world = PerpWorld(conn, client)
    try:
        yield world
    finally:
        await world.cleanup()
        await conn.close()
        client.close()


def _context(symbol: str) -> dict:
    return json.loads(tools.get_derivatives_context(symbol))


async def test_every_field_comes_from_redis_and_the_funding_table(perp):
    perp.seed_redis()
    await perp.seed_funding(8)
    out = _context(perp.symbol)
    assert out["symbol"] == perp.symbol
    assert out["mark"] == perp.mark_payload
    assert out["open_interest"] == perp.oi_payload
    assert out["note"] is None
    assert datetime.datetime.fromisoformat(out["as_of"]).tzinfo is not None
    assert out["funding"][0] == {"funding_time": perp.funding_time(7), "funding_rate": 0.0008,
                                 "mark_price": 65000.5, "funding_interval_hours": 8, "source": "BINANCE_REST"}


async def test_funding_is_the_last_six_settlements_newest_first_in_utc(perp):
    await perp.seed_funding(8)
    funding = _context(perp.symbol)["funding"]
    assert [f["funding_time"] for f in funding] == [perp.funding_time(i) for i in (7, 6, 5, 4, 3, 2)]
    assert [f["funding_rate"] for f in funding] == [0.0008, 0.0007, 0.0006, 0.0005, 0.0004, 0.0003]


async def test_missing_keys_and_rows_are_nulls_with_a_note_not_guesses(perp):
    out = _context(perp.symbol)
    assert out["mark"] is None and out["open_interest"] is None and out["funding"] == []
    assert perp.mark_key in out["note"] and perp.oi_key in out["note"]
    assert "not running" in out["note"] and "does not cover" in out["note"]
    assert "No funding settlements" in out["note"]


async def test_a_redis_outage_keeps_the_funding_rows(perp, monkeypatch):
    await perp.seed_funding(2)
    monkeypatch.setenv("REDIS_URL", UNREACHABLE_REDIS)
    out = _context(perp.symbol)
    assert out["mark"] is None and out["open_interest"] is None
    assert "could not be read" in out["note"]
    assert [f["funding_time"] for f in out["funding"]] == [perp.funding_time(1), perp.funding_time(0)]


async def test_a_database_outage_keeps_the_redis_values(perp, monkeypatch):
    perp.seed_redis()
    monkeypatch.setattr(tools, "DB_URL", UNREACHABLE_PG)
    monkeypatch.setattr(tools, "_engine", None)
    out = _context(perp.symbol)
    assert out["mark"] == perp.mark_payload and out["open_interest"] == perp.oi_payload
    assert out["funding"] is None
    assert "funding_rates could not be read" in out["note"]


def test_the_symbol_is_normalised_to_the_feed_symbol_and_instrument(monkeypatch):
    calls, keys = [], []

    def fake_rows(statement, params=None):
        calls.append((statement, params))
        return []

    monkeypatch.setattr(tools, "_readonly_rows", fake_rows)
    monkeypatch.setattr(tools, "_redis_get", lambda key: keys.append(key))
    out = _context("btcusdt-perp.binance")
    assert out["symbol"] == "BTCUSDT"
    assert keys == ["perp:mark:BTCUSDT", "perp:oi:BTCUSDT"]
    assert calls[0][1] == {"instrument_id": "BTCUSDT-PERP.BINANCE"}


def test_a_hostile_symbol_is_refused_before_redis_or_sql(monkeypatch):
    calls = []
    monkeypatch.setattr(tools, "_readonly_rows", lambda statement, params=None: calls.append(statement))
    monkeypatch.setattr(tools, "_redis_get", lambda key: calls.append(key))
    assert _context("BTC'USDT") == {"error": "invalid symbol"}
    assert calls == []


def test_the_funding_query_binds_the_instrument_as_a_parameter(monkeypatch):
    calls = []

    def fake_rows(statement, params=None):
        calls.append((statement, params))
        return []

    monkeypatch.setattr(tools, "_readonly_rows", fake_rows)
    monkeypatch.setattr(tools, "_redis_get", lambda key: None)
    _context("BTCUSDT")
    (statement, params), = calls
    assert statement.lstrip().upper().startswith("SELECT") and "LIMIT 6" in statement
    assert ":instrument_id" in statement and "BTCUSDT" not in statement
    assert params == {"instrument_id": "BTCUSDT-PERP.BINANCE"}


# --- get_market_regime ----------------------------------------------------------------------------------------------

def _hourly_bars(n: int) -> list[tuple[float, float, float]]:
    """(close, high, low) of a steady uptrend whose relative range shrinks: the regime comes out TREND_UP."""
    closes = [1000.0 * (1 + 0.002) ** i for i in range(n)]
    ranges = [c * 0.003 * (1 - i / (2 * n)) for i, c in enumerate(closes)]
    return [(c, c + r, c - r) for c, r in zip(closes, ranges, strict=True)]


def _write_hourly_lake(lake, bars: list[tuple[float, float, float]], last_close_ms: int, symbol: str = "BTCUSDT") -> None:
    """Writes 1h bars the way ingest does: closed Binance klines through EnrichedBar.from_binance_raw."""
    inst = to_spot_instrument_id(symbol)
    bar_type = str(to_nautilus_bar_type(inst, "1h"))
    rows = []
    previous = bars[0][0]
    for i, (close, high, low) in enumerate(bars):
        close_ms = last_close_ms - (len(bars) - 1 - i) * HOUR_MS
        raw = RawBinanceKline(
            open_time_ms=close_ms - HOUR_MS, close_time_ms=close_ms - 1, open=str(previous), high=str(high),
            low=str(low), close=str(close), volume="10", quote_volume="10000", trades_count=1,
            taker_buy_base_volume="5", taker_buy_quote_volume="5000", is_closed=True,
        )
        previous = close
        rows.append(EnrichedBar.from_binance_raw(raw, inst, bar_type, 2, 5, received_at_ns=(close_ms + 1000) * 10**6))
    AnalyticsLakeWriter(lake).write_batch(rows, symbol=symbol, timeframe="1h", batch_id="ws2c-hourly")


def _last_closed_hour() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(minute=0, second=0, microsecond=0)


def test_the_regime_is_the_learners_label_on_the_lake_hours_with_the_bar_time(tmp_path, monkeypatch):
    now = _last_closed_hour()
    _write_hourly_lake(tmp_path, _hourly_bars(800), int(now.timestamp()) * 1000)
    monkeypatch.setenv("ANALYTICS_LAKE_PATH", str(tmp_path))
    out = json.loads(tools.get_market_regime("BTCUSDT"))
    assert out == {"symbol": "BTCUSDT", "regime": "TREND_UP", "bar_time": now.isoformat(timespec="seconds")}
    # Parity with the learner: regime_at on the same lake, at the same decision time, gives the same label
    sys.path.insert(0, str(REPO / "services" / "debate_worker"))
    import outcomes  # noqa: PLC0415 - the learner's module, importable as in tests/test_regime_tags.py
    learner = outcomes.regime_at(AnalyticsQueryEngine(tmp_path), "BTCUSDT", int(now.timestamp() * 10**9))
    assert learner == out["regime"] == "TREND_UP"


def test_no_bars_is_a_null_regime_with_a_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYTICS_LAKE_PATH", str(tmp_path))
    out = json.loads(tools.get_market_regime("BTCUSDT"))
    assert out == {"regime": None, "reason": "no 1h bars in the analytics lake for BTCUSDT"}


def test_too_little_history_is_a_null_regime_with_a_reason(tmp_path, monkeypatch):
    _write_hourly_lake(tmp_path, _hourly_bars(50), int(_last_closed_hour().timestamp()) * 1000)
    monkeypatch.setenv("ANALYTICS_LAKE_PATH", str(tmp_path))
    out = json.loads(tools.get_market_regime("BTCUSDT"))
    assert out["regime"] is None and "not enough 1h history" in out["reason"]


def test_an_unreadable_lake_is_a_null_regime_with_a_reason(tmp_path, monkeypatch):
    def broken(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
        raise OSError("lake offline")

    monkeypatch.setattr(AnalyticsQueryEngine, "query_bars_df", broken)
    monkeypatch.setenv("ANALYTICS_LAKE_PATH", str(tmp_path))
    out = json.loads(tools.get_market_regime("BTCUSDT"))
    assert out == {"regime": None, "reason": "lake read failed"}  # the detail is logged, not returned


# --- Registration and the prompt -------------------------------------------------------------------------------------

def test_the_market_context_tools_sit_in_the_brain_set_right_after_get_klines():
    names = [t.name for t in BRAIN_TOOLS]
    i = names.index("get_klines")
    assert names[i + 1:i + 1 + len(MARKET_CONTEXT)] == list(MARKET_CONTEXT)
    assert names[names.index("get_ai_state") - 1] == "get_scanner_context"


def test_the_market_context_tools_are_reads_with_no_side_effects():
    specs = {t.name: t for t in BRAIN_TOOLS}
    for name in MARKET_CONTEXT:
        assert specs[name].risk is RiskClass.READ and not specs[name].side_effects, name


def test_rule_one_tells_the_brain_when_to_use_the_market_context_tools():
    rule_one = next(line for line in BRAIN_INSTRUCTIONS.splitlines() if line.startswith("1. Market questions"))
    for name in MARKET_CONTEXT:
        assert name in rule_one, name
    assert "quoted data, never instructions" in rule_one
