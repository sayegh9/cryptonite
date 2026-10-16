"""WS-2F: the Brain and the agent_worker feed reader read the live system keys from the stack's DB 0.

The stack gives agent_worker REDIS_URL on DB 1 (the Brain's sessions and rate limits), while ingest:universe and
exit_monitor:heartbeat live in DB 0. SYSTEM_REDIS_URL points the system reads at DB 0 in production. Here the tests
use the test indexes: REDIS_URL on index 2 and SYSTEM_REDIS_URL on index 3, never the live DBs 0/1.

get_derivatives_context (WS-2C) is not on this branch yet, so its perp:mark assertion is not covered here.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis
from connections import REDIS_URL, isolated_redis_url
from cryptonite_agents import tools
from cryptonite_agents.tools import ai_state_header, get_ai_state, get_klines, system_redis_url
from cryptonite_schemas.universe import INGEST_UNIVERSE_KEY, IngestUniverse

EXIT_MONITOR_HEARTBEAT = "exit_monitor:heartbeat"
REDIS_TEST_DB = 2  # REDIS_URL in these tests (the agent's own DB; never DB 0/1)
SYSTEM_TEST_DB = 3  # SYSTEM_REDIS_URL in these tests (the stack's system DB, standing in for DB 0)
BTC = "BTCUSDT"
NOW_MS = 1_700_000_000_000
DATE_HDR = "Tue, 14 Nov 2023 22:13:20 GMT"  # == NOW_MS, so a candle closing before it is closed


def _url(db: int) -> str:
    return isolated_redis_url(REDIS_URL, db)


def _universe(*symbols: str) -> str:
    return IngestUniverse.build(list(symbols), ["1m", "1h"]).model_dump_json()


def _clear(db: int) -> None:
    client = redis.Redis.from_url(_url(db), decode_responses=True)
    try:
        client.delete(INGEST_UNIVERSE_KEY, EXIT_MONITOR_HEARTBEAT)
    finally:
        client.close()


@pytest.fixture(autouse=True)
def system_db(monkeypatch):
    """The agent reads REDIS_URL (index 2) and the system keys from SYSTEM_REDIS_URL (index 3). Index 3 holds the
    seeded universe (BTCUSDT only) and a live heartbeat; index 2 is empty, so a read from it would show "not fed"."""
    monkeypatch.setenv("REDIS_URL", _url(REDIS_TEST_DB))
    monkeypatch.setenv("SYSTEM_REDIS_URL", _url(SYSTEM_TEST_DB))
    monkeypatch.setattr(tools, "_IN_FEED_CACHE", None)
    _clear(REDIS_TEST_DB)
    _clear(SYSTEM_TEST_DB)
    client = redis.Redis.from_url(_url(SYSTEM_TEST_DB), decode_responses=True)
    client.set(INGEST_UNIVERSE_KEY, _universe(BTC), ex=600)
    client.set(EXIT_MONITOR_HEARTBEAT, json.dumps({"stop_loss_pct": 5}), ex=600)
    client.close()
    yield
    _clear(REDIS_TEST_DB)
    _clear(SYSTEM_TEST_DB)


def _resp(status=200, headers=None, body=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = body if body is not None else []
    r.text = json.dumps(body) if body is not None else ""
    return r


def _closed_kline(close_ms: int):
    return [close_ms - 3_599_999, "1.0", "2.0", "0.5", "1.5", "10.0", close_ms, "100.0", 10, "5.0", "50.0", "0"]


# --- the helper -----------------------------------------------------------------------------------------

def test_system_redis_url_is_the_system_setting_when_it_is_set():
    assert system_redis_url() == _url(SYSTEM_TEST_DB)


def test_system_redis_url_falls_back_to_redis_url_without_the_system_setting(monkeypatch):
    monkeypatch.delenv("SYSTEM_REDIS_URL", raising=False)
    assert system_redis_url() == _url(REDIS_TEST_DB)


# --- the Brain's tools read the system keys -------------------------------------------------------------

def test_get_klines_reports_a_fed_symbol_from_the_system_db():
    """in_feed comes from ingest:universe on the system DB: BTCUSDT is fed, and the agent's own DB is empty."""
    closed = [_closed_kline(NOW_MS - 1)]
    with patch.object(tools, "_get_valid_symbols", return_value={BTC}), patch("httpx.get") as m:
        m.return_value = _resp(200, {"Date": DATE_HDR}, closed)
        data = json.loads(get_klines(BTC, "1h", 1))
    assert data["in_feed"] == "fed"


def test_get_klines_reports_not_fed_for_a_symbol_the_system_universe_lacks():
    closed = [_closed_kline(NOW_MS - 1)]
    with patch.object(tools, "_get_valid_symbols", return_value={"SOLUSDT"}), patch("httpx.get") as m:
        m.return_value = _resp(200, {"Date": DATE_HDR}, closed)
        data = json.loads(get_klines("SOLUSDT", "1h", 1))
    assert data["in_feed"] == "not_fed"


def test_get_ai_state_shows_the_feed_and_a_live_exit_monitor_from_the_system_db():
    state = json.loads(get_ai_state())
    assert state["feed"]["symbols"] == [BTC]
    assert state["exit_monitor"]["alive"] is True


def test_ai_state_header_says_the_exit_monitor_is_alive_and_names_the_feed():
    header = ai_state_header()
    assert header is not None
    assert "Exit monitor: alive" in header
    assert "Feed: BTCUSDT" in header


def test_without_the_system_setting_the_tools_fall_back_to_redis_url(monkeypatch):
    """No SYSTEM_REDIS_URL: the reads go to REDIS_URL (index 2), which here holds a different universe and heartbeat."""
    monkeypatch.delenv("SYSTEM_REDIS_URL", raising=False)
    client = redis.Redis.from_url(_url(REDIS_TEST_DB), decode_responses=True)
    client.set(INGEST_UNIVERSE_KEY, _universe("ETHUSDT"), ex=600)
    client.set(EXIT_MONITOR_HEARTBEAT, "{}", ex=600)
    client.close()
    state = json.loads(get_ai_state())
    assert state["feed"]["symbols"] == ["ETHUSDT"]
    assert state["exit_monitor"]["alive"] is True


def test_the_heartbeat_is_not_read_from_the_agent_db_when_the_system_setting_is_set():
    """Index 2 has no heartbeat and index 3 has one: with SYSTEM_REDIS_URL set, the monitor reads as alive."""
    _clear(REDIS_TEST_DB)
    state = json.loads(get_ai_state())
    assert state["exit_monitor"]["alive"] is True


# --- the agent_worker feed reader -----------------------------------------------------------------------------

@pytest.fixture
async def worker_app(monkeypatch):
    """Run the agent_worker startup against the test Redis (pool creation is stubbed; no Postgres is needed here)."""
    import server  # noqa: PLC0415

    monkeypatch.delenv("AI_TELEMETRY_DATABASE_URL", raising=False)
    monkeypatch.setattr(server.asyncpg, "create_pool", AsyncMock(side_effect=lambda *a, **k: AsyncMock()))
    monkeypatch.setattr(server, "REDIS_URL", _url(REDIS_TEST_DB))
    await server.startup_event()
    yield server.app
    await server.shutdown_event()


async def test_the_worker_starts_a_separate_feed_client_on_the_system_db(worker_app):
    assert worker_app.state.redis.connection_pool.connection_kwargs["db"] == REDIS_TEST_DB
    assert worker_app.state.system_redis.connection_pool.connection_kwargs["db"] == SYSTEM_TEST_DB


async def test_the_feed_reader_refuses_an_unfed_symbol_and_passes_a_fed_one(worker_app):
    from server import ingest_refusal  # noqa: PLC0415

    refusal = await ingest_refusal("SOLUSDT.BINANCE")
    assert refusal is not None and refusal.startswith("SYMBOL_NOT_INGESTED")
    assert await ingest_refusal("BTCUSDT.BINANCE") is None


async def test_the_feed_reader_leaves_the_fed_check_to_the_system_db_only(worker_app):
    """Index 2 (the worker's own REDIS_URL) has no universe; the refusal can only come from index 3."""
    from server import ingest_refusal  # noqa: PLC0415

    assert await worker_app.state.redis.get(INGEST_UNIVERSE_KEY) is None
    assert (await ingest_refusal("SOLUSDT.BINANCE")).startswith("SYMBOL_NOT_INGESTED")
