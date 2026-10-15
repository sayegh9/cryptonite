"""Approval context served to the web (remediation R6.4, R6.3): past decisions next to an approval,
and the learner / sentiment / stop-coverage health fields of /ai/status."""

import datetime
import json
import time
from decimal import Decimal

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_api import similar as similar_mod
from cryptonite_api.similar import similar_decisions
from migrated_db import migrated_database

INST = "BTCUSDT.BINANCE"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_approval_ctx") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    try:
        yield harness
    finally:
        await harness.redis.delete("learner:heartbeat", "sentiment:last_ok")
        await close_harness(harness)


async def decision(conn, rec: str, ret: str | None, *, days_ago: float, inst: str = INST):
    """One debate run + portfolio decision, labelled at 24h unless ``ret`` is None."""
    run = await conn.fetchval(
        "INSERT INTO debate_runs (instrument_id, trigger_source) VALUES ($1, 'test') RETURNING id", inst)
    created = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days_ago)
    dec = await conn.fetchval(
        "INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, allocation_pct, "
        "created_at, decision_price) VALUES ($1, $2, $3, 0.7, 5, $4, 60000) RETURNING id", run, inst, rec, created)
    if ret is not None:
        await conn.execute("INSERT INTO ai_decision_outcomes (decision_id, horizon, return_pct, mfe, mae, bars) "
                           "VALUES ($1, '24h', $2, 0.05, -0.05, 1440)", dec, Decimal(ret))
    return run


async def test_similar_decisions_same_instrument_and_side_newest_first_signed(h):
    async with h.pool.acquire() as conn:
        await conn.execute("DELETE FROM ai_decision_outcomes; DELETE FROM portfolio_decisions; DELETE FROM debate_runs")
        await decision(conn, "BUY", "0.02", days_ago=5)
        await decision(conn, "BUY", "-0.0005", days_ago=4)   # inside the neutral band
        await decision(conn, "BUY", "-0.03", days_ago=3)
        current = await decision(conn, "BUY", "0.01", days_ago=2)
        await decision(conn, "SELL", "-0.02", days_ago=1)    # other side
        await decision(conn, "BUY", None, days_ago=0.5)      # not labelled yet
        await decision(conn, "BUY", "0.05", days_ago=1, inst="ETHUSDT.BINANCE")

        rows = await similar_decisions(conn, INST, "buy", exclude_run_id=current)
        assert [r["asset_return_pct"] for r in rows] == [-0.03, -0.0005, 0.02]
        assert [r["outcome"] for r in rows] == ["LOSS", "NEUTRAL", "WIN"]
        assert all(r["recommendation"] == "BUY" for r in rows)

        sells = await similar_decisions(conn, INST, "SELL")
        assert len(sells) == 1 and sells[0]["return_pct"] == 0.02 and sells[0]["outcome"] == "WIN"  # SELL gained from a fall
        assert await similar_decisions(conn, None, "BUY") == []


async def test_decision_endpoint_carries_similar_decisions_and_omits_them_on_failure(h, monkeypatch):
    async with h.pool.acquire() as conn:
        await conn.execute("DELETE FROM ai_decision_outcomes; DELETE FROM portfolio_decisions; DELETE FROM debate_runs")
        await decision(conn, "BUY", "0.02", days_ago=3)
        run = await decision(conn, "BUY", None, days_ago=0)
    async with h.client() as c:
        body = (await c.get(f"/api/v1/agents/debates/{run}/decision")).json()
        assert [d["outcome"] for d in body["similar_decisions"]] == ["WIN"]
        assert body["recommendation"] == "BUY"

        async def boom(*a, **k):
            raise RuntimeError("db hiccup")

        monkeypatch.setattr(similar_mod, "similar_decisions", boom)
        res = await c.get(f"/api/v1/agents/debates/{run}/decision")
        assert res.status_code == 200 and "similar_decisions" not in res.json()


async def test_status_reports_learner_and_sentiment(h):
    async with h.client() as c:
        await h.redis.delete("learner:heartbeat", "sentiment:last_ok")
        status = (await c.get("/api/v1/ai/status")).json()
        assert status["learner_up"] is False and status["sentiment_last_ok"] is None

        now = int(time.time())
        await h.redis.set("learner:heartbeat", "1", ex=60)
        await h.redis.set("sentiment:last_ok", str(now))
        status = (await c.get("/api/v1/ai/status")).json()
        assert status["learner_up"] is True and status["sentiment_last_ok"] == now


async def test_status_reports_stop_coverage_and_names_blind_positions(h):
    now = datetime.datetime.now(datetime.UTC)
    hours = [f"exit_monitor:blind_s:{(now - datetime.timedelta(hours=i)).strftime('%Y%m%d%H')}" for i in (0, 1, 25)]
    try:
        async with h.client() as c:
            await h.redis.delete("exit_monitor:coverage", *hours)
            status = (await c.get("/api/v1/ai/status")).json()
            assert status["stop_coverage"] is None and not any("blind" in d for d in status["degraded"])

            await h.redis.set("exit_monitor:coverage", json.dumps({"checked_at": 1.0, "positions": [
                {"instrument_id": "BTCUSDT.BINANCE", "source": "REST", "blind_since": None},
                {"instrument_id": "XRPUSDT.BINANCE", "source": None, "blind_since": 1.0}]}), ex=60)
            await h.redis.hset(hours[0], "XRPUSDT.BINANCE", "30")
            await h.redis.hset(hours[1], mapping={"XRPUSDT.BINANCE": "60.5", "BTCUSDT.BINANCE": "12"})
            await h.redis.hset(hours[2], "XRPUSDT.BINANCE", "999")  # older than 24 h: not counted
            cov = (await c.get("/api/v1/ai/status")).json()
            assert cov["stop_coverage"]["blind_s_24h"] == {"BTCUSDT.BINANCE": 12, "XRPUSDT.BINANCE": 90}
            assert [p["source"] for p in cov["stop_coverage"]["positions"]] == ["REST", None]
            assert [d for d in cov["degraded"] if d.startswith("Stops blind on XRPUSDT.BINANCE")]
    finally:
        await h.redis.delete("exit_monitor:coverage", *hours)
