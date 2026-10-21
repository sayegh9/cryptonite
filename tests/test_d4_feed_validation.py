"""D4 (F-H7): refuse what cannot be traded.

- ingest publishes the universe it feeds (``ingest:universe``); the API reads it;
- mandate and strategy-instance creation: 422 naming the reason for an un-fed symbol or timeframe,
  503 when the universe is unknown (never a silent pass);
- instances carry the real source hash of their strategy, not ``'dummy_hash'``;
- one Brain decision (or one debate) approved twice gives one order.

Runs on a throwaway migrated database (``cryptonite_ci_d4_*``) and the Redis at REDIS_URL.
"""

import asyncio
import json
import re
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_orchestrator.server import SimpleQuoteCache, command_poller
from cryptonite_orchestrator.source_hash import instance_source_hash
from cryptonite_schemas.universe import (
    INGEST_UNIVERSE_KEY,
    UNIVERSE_TTL_S,
    IngestUniverse,
    normalize_timeframe,
)
from cryptonite_strategies.registry import STRATEGY_REGISTRY, get_strategy_source_hash
from migrated_db import migrated_database
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_fresh_dispatcher

BTC = "BTCUSDT.BINANCE"
FED = IngestUniverse.build(["BTCUSDT", "ETHUSDT"], ["1m", "1h"], ["BTCUSDT"])


# ------------------------------------------------------------------------------------ the universe


@pytest.mark.parametrize("raw,expected", [
    ("1m", "1m"), ("1H", "1h"), ("15m", "15m"), ("1-MINUTE-LAST-EXTERNAL", "1m"),
    ("BTCUSDT.BINANCE-15-MINUTE-LAST-EXTERNAL", "15m"), ("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-INTERNAL", "1h"),
    ("fortnightly", None), ("", None),
])
def test_timeframes_normalise_from_every_form_the_web_sends(raw, expected):
    assert normalize_timeframe(raw) == expected


def test_refusal_names_the_reason():
    assert FED.refusal("BTCUSDT") is None and FED.refusal("ethusdt.binance", "1h") is None
    assert FED.refusal("BTCUSDT.BINANCE", "1-MINUTE-LAST-EXTERNAL") is None
    assert FED.refusal("XRPUSDT.BINANCE").startswith("SYMBOL_NOT_INGESTED: ingest has no feed for XRPUSDT")
    assert FED.refusal("SOLUSDT.BINANCE", "1m").startswith("SYMBOL_NOT_INGESTED")
    assert FED.refusal(BTC, "15m").startswith("TIMEFRAME_NOT_INGESTED: ingest has no 15m bars")
    assert FED.refusal(BTC, "nonsense").startswith("TIMEFRAME_NOT_INGESTED")
    # a full bar type must be the instrument's own (the orchestrator would run the named bars verbatim)
    assert "is for ETHUSDT.BINANCE" in FED.refusal(BTC, "ETHUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL")
    assert FED.refusal("BTCUSDT-PERP.BINANCE") is None
    assert FED.refusal("ETHUSDT-PERP.BINANCE").startswith("PERP_NOT_INGESTED")
    assert FED.refusal("BTCUSDT-PERP.BINANCE", "1m").startswith("PERP_NOT_INGESTED")  # quotes only, no bars
    assert FED.refusal("BTCUSDT.BYBIT").startswith("VENUE_NOT_INGESTED")


def test_unknown_or_unreadable_universe_parses_to_none():
    assert IngestUniverse.parse(None) is None and IngestUniverse.parse("") is None
    assert IngestUniverse.parse("{not json") is None and IngestUniverse.parse('"a string"') is None
    assert IngestUniverse.parse(FED.model_dump_json()) == FED


async def test_ingest_publishes_its_universe_with_a_ttl_including_onboarded_symbols():
    import redis.asyncio as aioredis
    from connections import REDIS_URL
    from cryptonite_ingest.recorder import IngestRecorder

    rec = IngestRecorder.__new__(IngestRecorder)  # no catalogs, no network: only the universe fields
    rec.symbols, rec.timeframes, rec.perp_quote_symbols = ["BTCUSDT", "ETHUSDT"], ["1m", "1h"], ["BTCUSDT"]
    rec.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await rec.publish_universe()
        published = IngestUniverse.parse(await rec.redis.get(INGEST_UNIVERSE_KEY))
        assert (published.symbols, published.timeframes, published.perp_symbols) == (["BTCUSDT", "ETHUSDT"], ["1h", "1m"], ["BTCUSDT"])
        assert 0 < await rec.redis.ttl(INGEST_UNIVERSE_KEY) <= UNIVERSE_TTL_S
        rec.symbols.append("XRPUSDT")  # what the CHANNEL_INSTRUMENTS_UPDATED listener does before republishing
        await rec.publish_universe()
        assert "XRPUSDT" in IngestUniverse.parse(await rec.redis.get(INGEST_UNIVERSE_KEY)).symbols
    finally:
        await rec.redis.delete(INGEST_UNIVERSE_KEY)
        await rec.redis.aclose()


# ------------------------------------------------------------------------------------ the API


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_d4") as url:
        yield url


@pytest.fixture
async def h(db_url, monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_TOTP", "true")  # 000000 in DEV, as in test_autonomy
    monkeypatch.setenv("AI_ALLOW_DEV_TOTP", "true")
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_positions CASCADE")
    try:
        yield harness
    finally:
        await close_harness(harness)


def mandate_body(*instruments: str) -> dict:
    return {"name": f"d4-{uuid.uuid4().hex[:6]}", "capital_budget_usd": "5000", "instruments": list(instruments),
            "max_position_pct": "10", "totp_code": "000000"}


async def test_mandate_on_an_unfed_symbol_is_422_and_nothing_is_written(h):
    async with h.client() as c:
        res = await c.post("/api/v1/ai/mandates", json=mandate_body("BTCUSDT", "XRPUSDT"), headers=h.headers)
        assert res.status_code == 422
        assert res.json()["detail"].startswith("SYMBOL_NOT_INGESTED: ingest has no feed for XRPUSDT")
        ok = await c.post("/api/v1/ai/mandates", json=mandate_body("BTCUSDT"), headers=h.headers)
        assert ok.status_code == 201, ok.text
        widen = await c.patch(f"/api/v1/ai/mandates/{ok.json()['id']}", headers=h.headers,
                              json={"instruments": ["BTCUSDT", "XRPUSDT"], "totp_code": "000000"})
        assert widen.status_code == 422 and "XRPUSDT" in widen.json()["detail"]
    assert await h.pool.fetchval("SELECT count(*) FROM ai_mandates WHERE 'XRPUSDT.BINANCE' = ANY(instruments)") == 0


async def test_creation_fails_closed_when_the_universe_is_unknown(h):
    await h.redis.delete(INGEST_UNIVERSE_KEY)  # ingest down for longer than the TTL
    async with h.client() as c:
        mandate = await c.post("/api/v1/ai/mandates", json=mandate_body("BTCUSDT"), headers=h.headers)
        deploy = await c.post("/api/v1/paper/strategies/deploy", headers=h.headers, json=deploy_body(BTC, "1m"))
        view = await c.get("/api/v1/market/universe")
    for res in (mandate, deploy):
        assert res.status_code == 503 and res.json()["detail"].startswith("INGEST_UNIVERSE_UNKNOWN"), res.text
    assert view.status_code == 200 and view.json()["available"] is False
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_the_web_can_read_the_universe(h):
    async with h.client() as c:
        body = (await c.get("/api/v1/market/universe")).json()
        assert (await httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://api.test")
                .get("/api/v1/market/universe")).status_code == 401  # operators only
    assert body["available"] is True and body["timeframes"] == ["1h", "1m"]
    assert "BTCUSDT.BINANCE" in body["instruments"] and "XRPUSDT.BINANCE" not in body["instruments"]


def deploy_body(instrument: str, timeframe: str, strategy: str = "ema_cross", version: str | None = None) -> dict:
    return {"strategy_name": strategy, "strategy_version_id": version or str(uuid.uuid4()), "instrument_id": instrument,
            "timeframe": timeframe, "allocation_usd": 1000.0}


@pytest.mark.parametrize("instrument,timeframe,reason", [
    ("SOLUSDT.BINANCE", "1m", "SYMBOL_NOT_INGESTED"),  # INST-513B9013
    (BTC, "15m", "TIMEFRAME_NOT_INGESTED"),  # INST-7DF91538
    (BTC, "BTCUSDT.BINANCE-15-MINUTE-LAST-EXTERNAL", "TIMEFRAME_NOT_INGESTED"),  # DeployStrategyModal's form
])
async def test_strategy_instance_on_an_unfed_symbol_or_timeframe_is_422(h, instrument, timeframe, reason):
    async with h.client() as c:
        res = await c.post("/api/v1/paper/strategies/deploy", headers=h.headers, json=deploy_body(instrument, timeframe))
    assert res.status_code == 422 and res.json()["detail"].startswith(reason), res.text
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands WHERE command_type = 'DEPLOY'") == 0


async def test_a_venue_less_instrument_is_deployed_with_its_venue(h):
    """2026-10-21: the Strategy drawer sent XRPUSDT (no venue); the engine refused it and the instance sat RUNNING."""
    async with h.client() as c:
        res = await c.post("/api/v1/paper/strategies/deploy", headers=h.headers, json=deploy_body("btcusdt", "1m"))
    assert res.status_code == 200, res.text
    payload = await h.pool.fetchval("SELECT payload FROM execution_commands WHERE command_type = 'DEPLOY'")
    assert json.loads(payload)["instrument_id"] == BTC


# ------------------------------------------------------------------------------------ source hash


async def deploy_and_run(h, slug: str, version_id: uuid.UUID, instrument: str = BTC) -> dict:
    async with h.client() as c:
        res = await c.post("/api/v1/paper/strategies/deploy", headers=h.headers,
                           json=deploy_body(instrument, "1m", strategy=slug, version=str(version_id)))
    assert res.status_code == 200, res.text
    engine = make_engine(make_fresh_dispatcher(starting_cash=Decimal("100000"))[0])
    engine.bridge = MagicMock()
    task = asyncio.create_task(command_poller(h.pool, engine, SimpleQuoteCache()))
    try:
        for _ in range(100):
            row = await h.pool.fetchrow("SELECT status, error_message, target_instance_id FROM execution_commands WHERE command_id = $1",
                                        res.json()["command_id"])
            if row["status"] in ("COMPLETED", "REJECTED", "FAILED"):
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert row["status"] == "COMPLETED", row["error_message"]
    return dict(await h.pool.fetchrow("SELECT * FROM paper_strategy_instances WHERE instance_id = $1", row["target_instance_id"]))


async def test_instances_carry_the_real_and_stable_source_hash(h):
    from cryptonite_api.routes.strategies import ensure_strategies_seeded

    await ensure_strategies_seeded(h.pool)
    slug = sorted(STRATEGY_REGISTRY)[0]
    version_id = await h.pool.fetchval(
        "SELECT v.id FROM strategy_versions v JOIN strategies s ON s.id = v.strategy_id WHERE s.slug = $1", slug)
    first = await deploy_and_run(h, slug, version_id)
    second = await deploy_and_run(h, slug, version_id, "ETHUSDT.BINANCE")  # one RUNNING instance per instrument
    expected = get_strategy_source_hash(slug)
    assert re.fullmatch(r"[0-9a-f]{64}", expected)
    assert first["strategy_source_hash"] == second["strategy_source_hash"] == expected != "dummy_hash"
    # The risk policy it was deployed under is a real hash of the enforced limits too, the same for both
    assert re.fullmatch(r"[0-9a-f]{64}", first["risk_policy_hash"])
    assert first["risk_policy_hash"] == second["risk_policy_hash"] != "dummy_hash"
    assert get_strategy_source_hash(slug) == expected  # deterministic: the same file hashes the same


async def test_an_unregistered_strategy_gets_the_hash_recorded_with_its_version(h):
    from cryptonite_api.routes.strategies import ensure_strategies_seeded

    await ensure_strategies_seeded(h.pool)
    version_id, code_hash = await h.pool.fetchrow("SELECT id, code_hash FROM strategy_versions ORDER BY created_at LIMIT 1")
    async with h.pool.acquire() as conn:
        assert await instance_source_hash(conn, "ai-gen-not-registered", version_id) == code_hash


# ------------------------------------------------------------------------------------ one decision, one order


def trace_cards(trace: str, n: int = 3) -> list[dict]:
    """The Brain emitted the same XRP order as 3 cards in one turn (one trace); here on BTC."""
    return [{"kind": "order_card", "id": f"call-{i}", "instrument_id": BTC, "side": "BUY", "quantity": 0.01,
             "order_type": "MARKET", "limit_price": None, "allocation_pct": 5.0, "stop_loss": 58000.0,
             "take_profit": None, "status": "PENDING_APPROVAL", "trace_id": trace, "command_id": None} for i in range(n)]


@pytest.fixture
def brain_cards(monkeypatch):
    from cryptonite_api.routes import brain as brain_routes

    cards = trace_cards(str(uuid.uuid4()))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"session": {}, "messages": [{"role": "assistant", "widgets": cards}]})
        body = json.loads(request.content)
        card = next(c for c in cards if c["id"] == body.get("widget_id"))
        if body.get("expect_status") and card["status"] != body["expect_status"]:
            return httpx.Response(409)
        card.update({k: body[k] for k in ("status", "command_id") if body.get(k)})
        return httpx.Response(200, json={"status": "ok"})

    real = httpx.AsyncClient
    monkeypatch.setattr(brain_routes.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return cards


async def test_three_cards_of_one_brain_decision_give_one_order(h, brain_cards):
    await h.mandate(level=1, instruments=(BTC,))
    sid = uuid.uuid4()
    async with h.client() as c:
        results = [await c.post(f"/api/v1/agents/brain/sessions/{sid}/cards/{card['id']}/approve", headers=h.headers)
                   for card in brain_cards]
        replay = await c.post(f"/api/v1/agents/brain/sessions/{sid}/cards/call-0/approve", headers=h.headers)
    assert [r.status_code for r in results] == [200, 409, 409]
    assert all(r.json()["detail"].startswith("DUPLICATE_DECISION") for r in results[1:])
    assert replay.status_code == 200 and replay.json()["command_id"] == results[0].json()["command_id"]
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands WHERE command_type = 'SUBMIT_ORDER'") == 1
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals WHERE source = 'brain_card'") == 1
    assert [card["status"] for card in brain_cards] == ["APPROVED", "PENDING_APPROVAL", "PENDING_APPROVAL"]  # twins unclaimed


async def test_concurrent_approvals_of_one_brain_decision_give_one_order(h, brain_cards):
    await h.mandate(level=1, instruments=(BTC,))
    sid = uuid.uuid4()
    async with h.client() as c:
        results = await asyncio.gather(*[
            c.post(f"/api/v1/agents/brain/sessions/{sid}/cards/{card['id']}/approve", headers=h.headers) for card in brain_cards])
    assert sorted(r.status_code for r in results) == [200, 409, 409], [r.text for r in results]
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands WHERE command_type = 'SUBMIT_ORDER'") == 1


async def test_a_debate_approved_twice_at_once_gives_one_order(h):
    await h.mandate(level=1, instruments=(BTC,))
    run_id = uuid.uuid4()
    await h.pool.execute("INSERT INTO debate_runs (id, instrument_id, status, trigger_source) VALUES ($1, $2, 'PENDING_APPROVAL', 'test')",
                         run_id, BTC)
    await h.pool.execute(
        """INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, allocation_pct, reasoning)
           VALUES ($1, $2, 'BUY', 0.7, 5, 'r')""", run_id, BTC)
    await h.redis.set("market:24h:BTCUSDT", json.dumps({"last_price": 50000}), ex=60)
    async with h.client() as c:
        results = await asyncio.gather(*[c.post(f"/api/v1/agents/debates/{run_id}/approve", headers=h.headers) for _ in range(3)])
    assert sorted(r.status_code for r in results) == [200, 409, 409], [r.text for r in results]
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands WHERE command_type = 'SUBMIT_ORDER'") == 1



def test_the_risk_policy_hash_follows_the_enforced_limits():
    from decimal import Decimal
    from types import SimpleNamespace

    from cryptonite_orchestrator.source_hash import risk_policy_hash

    def dispatcher(**kw):
        base = dict(max_daily_loss_pct=Decimal("0.03"), min_order_equity_pct=Decimal("0.01"),
                    max_position_concentration_pct=Decimal("0.25"), taker_fee=Decimal("0.001"))
        return SimpleNamespace(**{**base, **kw})

    a, b = risk_policy_hash(dispatcher()), risk_policy_hash(dispatcher())
    assert re.fullmatch(r"[0-9a-f]{64}", a) and a == b  # deterministic
    assert risk_policy_hash(dispatcher(max_daily_loss_pct=Decimal("0.02"))) != a  # a tighter limit is a new policy
