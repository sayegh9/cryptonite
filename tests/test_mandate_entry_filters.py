"""ADR 0010 entry filters, refuse only: the debate worker's routing, the Brain card's dry run (and approve), the Inbox
debate approval, and the mandate API (today's session in the views; loosening a filter or a limit needs TOTP).

Runs on a throwaway migrated database and the Redis at REDIS_URL, as test_brain_card_preflight does.
"""

import importlib.util
import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from ai_api_harness import close_harness, make_harness
from migrated_db import migrated_database
from test_brain_card_preflight import (  # noqa: F401
    approve,
    ask,
    assert_refused_like_approve,
    hold_position,
    worker,
)

WORKER = Path(__file__).resolve().parents[1] / "services" / "debate_worker"
sys.path.insert(0, str(WORKER))
BTC = "BTCUSDT.BINANCE"
D = Decimal


@pytest.fixture(scope="module")
def routing():
    spec = importlib.util.spec_from_file_location("debate_mandate_routing_filters", WORKER / "mandate_routing.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_filters") as url:
        yield url


@pytest.fixture
async def h(db_url, monkeypatch):
    monkeypatch.setenv("AI_ALLOW_DEV_TOTP", "true")  # 000000 for the TOTP-gated edits below
    monkeypatch.setenv("ALLOW_DEV_TOTP", "true")
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_orders, "
                               "paper_positions, ai_mandate_daily_sessions CASCADE")
    try:
        yield harness
    finally:
        await close_harness(harness)


async def limits(h, m, **kw):
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(kw))
    await h.pool.execute(f"UPDATE ai_mandates SET {sets} WHERE id = $1", m.id,
                         *(D(v) if v is not None else None for v in kw.values()))


async def latch(h, m, column: str):
    """Today's session row with ``column`` recorded, as the orchestrator's monitor writes it."""
    await h.pool.execute(
        f"""INSERT INTO ai_mandate_daily_sessions (mandate_id, session_date, start_equity, start_budget, last_pnl, {column})
            VALUES ($1, (now() AT TIME ZONE 'UTC')::date, 10000, 10000, 0, now())""", m.id)


# --- the debate worker's route_entry -------------------------------------------------------------------------------

async def test_route_entry_refuses_a_conviction_below_the_minimum(h, routing):
    m = await h.mandate(level=1)
    await limits(h, m, min_conviction="0.70")
    async with h.pool.acquire() as conn:
        low = await routing.route_entry(conn, uuid.uuid4(), BTC, "Overweight", 0.62, 5.0)
        ok = await routing.route_entry(conn, uuid.uuid4(), BTC, "Overweight", 0.70, 5.0)
    assert (low[0], low[1].id) == ("NO_TRADE", m.id)
    assert ok[0] == "PENDING_APPROVAL"
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 1  # only the second asked the operator


async def test_route_entry_refuses_a_reward_risk_below_the_minimum(h, routing):
    m = await h.mandate(level=1)
    await limits(h, m, min_rr="2.0")
    async with h.pool.acquire() as conn:
        low = await routing.route_entry(conn, uuid.uuid4(), BTC, "Buy", 0.8, 5.0, stop_loss=95.0, take_profit=108.0,
                                        reference_price=100.0)
        ok = await routing.route_entry(conn, uuid.uuid4(), BTC, "Buy", 0.8, 5.0, stop_loss=95.0, take_profit=110.0,
                                       reference_price=100.0)
        # no reference price: the R:R filter is skipped (logged), not a refusal
        unpriced = await routing.route_entry(conn, uuid.uuid4(), BTC, "Buy", 0.8, 5.0, stop_loss=95.0,
                                             take_profit=101.0)
    assert [low[0], ok[0], unpriced[0]] == ["NO_TRADE", "PENDING_APPROVAL", "PENDING_APPROVAL"]


async def test_route_entry_records_that_min_rr_was_skipped_without_a_stop_and_target(h, routing, caplog):
    m = await h.mandate(level=1)
    await limits(h, m, min_rr="2.0")
    with caplog.at_level("INFO"):
        async with h.pool.acquire() as conn:
            res = await routing.route_entry(conn, uuid.uuid4(), BTC, "Buy", 0.8, 5.0, reference_price=100.0)
    assert res[0] == "PENDING_APPROVAL"
    row = await h.pool.fetchrow("SELECT payload, summary FROM ai_approvals")
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    assert payload["filters_skipped"] == ["min_rr skipped: no stop/target"]
    assert "min_rr skipped: no stop/target" in row["summary"]
    assert any("min_rr skipped: no stop/target" in r.message and r.levelname == "INFO" for r in caplog.records)


async def test_route_entry_still_applies_min_conviction_to_a_debate_without_a_stop(h, routing):
    m = await h.mandate(level=1)
    await limits(h, m, min_conviction="0.70", min_rr="2.0")
    async with h.pool.acquire() as conn:
        low = await routing.route_entry(conn, uuid.uuid4(), BTC, "Buy", 0.6, 5.0)
    assert low[0] == "NO_TRADE"


async def test_route_entry_refuses_a_stop_on_the_wrong_side(h, routing):
    m = await h.mandate(level=1)
    await limits(h, m, min_rr="2.0")
    async with h.pool.acquire() as conn:
        res = await routing.route_entry(conn, uuid.uuid4(), BTC, "Buy", 0.8, 5.0, stop_loss=105.0, take_profit=115.0,
                                        reference_price=100.0)
    assert res[0] == "NO_TRADE"


@pytest.mark.parametrize("column", ["loss_latched_at", "target_hit_at", "lock_fired_at"])
async def test_route_entry_refuses_entries_while_the_session_is_not_active(h, routing, column):
    m = await h.mandate(level=1)
    await limits(h, m, daily_loss_limit_pct="3", daily_profit_target_pct="2")
    await latch(h, m, column)
    await hold_position(h.pool, m.instance_id, 0.01)
    async with h.pool.acquire() as conn:
        buy = await routing.route_entry(conn, uuid.uuid4(), BTC, "Buy", 0.9, 5.0)
        sell = await routing.route_entry(conn, uuid.uuid4(), BTC, "Sell", 0.9, 5.0)
    assert buy[0] == "NO_TRADE"
    assert sell[0] == "PENDING_APPROVAL"  # selling the mandate's holding is risk reducing: an L1 veto window
    assert await h.pool.fetchval("SELECT action_class FROM ai_approvals") == "REDUCE_RISK"


# --- the Brain card's dry run and approve --------------------------------------------------------------------------

@pytest.mark.parametrize(("column", "code", "text"), [
    ("loss_latched_at", "MANDATE_DAILY_LOSS_LATCHED", "daily loss limit reached — new entries resume 00:00 UTC"),
    ("target_hit_at", "MANDATE_DAILY_TARGET_MET", "daily profit target reached — new entries resume 00:00 UTC"),
    ("lock_fired_at", "MANDATE_DAILY_TARGET_MET", "daily profit target reached — new entries resume 00:00 UTC"),
])
async def test_a_card_entry_is_refused_while_the_session_is_not_active(h, worker, column, code, text):  # noqa: F811
    m = await h.mandate(instruments=(BTC,))
    await limits(h, m, daily_loss_limit_pct="3", daily_profit_target_pct="2")
    await latch(h, m, column)
    body = await assert_refused_like_approve(h, worker, code=code)
    assert text in body["detail"]


async def test_a_card_below_the_conviction_minimum_is_refused(h, worker):  # noqa: F811
    m = await h.mandate(instruments=(BTC,))
    await limits(h, m, min_conviction="0.70")
    worker.card["confidence"] = 0.62
    body = await assert_refused_like_approve(h, worker, code="MANDATE_MIN_CONVICTION")
    assert "conviction 0.62 below the mandate minimum 0.70" in body["detail"]


async def test_a_card_without_a_confidence_is_refused_when_the_mandate_sets_a_minimum(h, worker):  # noqa: F811
    m = await h.mandate(instruments=(BTC,))
    await limits(h, m, min_conviction="0.70")
    body = await assert_refused_like_approve(h, worker, code="MANDATE_MIN_CONVICTION")
    assert "the card has no confidence" in body["detail"]
    worker.card["confidence"] = 0.8
    assert (await ask(h, uuid.uuid4())).json()["ok"] is True
    await limits(h, m, min_conviction=None)
    del worker.card["confidence"]
    assert (await ask(h, uuid.uuid4())).json()["ok"] is True  # no minimum: nothing required


async def test_a_card_without_a_stop_and_a_target_is_refused_when_the_mandate_sets_min_rr(h, worker):  # noqa: F811
    m = await h.mandate(instruments=(BTC,))
    await limits(h, m, min_rr="2.0")
    worker.card.update(stop_loss=58000.0, take_profit=None)
    body = await assert_refused_like_approve(h, worker, code="MANDATE_MIN_RR")
    assert "the mandate requires R:R \u2265 2.0: add a stop and a target" in body["detail"]
    worker.card.update(stop_loss=None, take_profit=64000.0)
    await assert_refused_like_approve(h, worker, code="MANDATE_MIN_RR")


async def test_a_card_with_the_stop_on_the_wrong_side_is_refused(h, worker):  # noqa: F811
    m = await h.mandate(instruments=(BTC,))
    await limits(h, m, min_rr="2.0")
    worker.card.update(order_type="LIMIT", limit_price=60000.0, stop_loss=62000.0, take_profit=66000.0)  # a BUY
    body = await assert_refused_like_approve(h, worker, code="MANDATE_MIN_RR")
    assert "stop/target on the wrong side" in body["detail"]


async def test_a_card_below_the_reward_risk_minimum_is_refused(h, worker):  # noqa: F811
    m = await h.mandate(instruments=(BTC,))
    await limits(h, m, min_rr="2.0")
    worker.card.update(order_type="LIMIT", limit_price=60000.0, stop_loss=58000.0, take_profit=63200.0)
    body = await assert_refused_like_approve(h, worker, code="MANDATE_MIN_RR")
    assert "R:R 1.6 below the mandate minimum 2.0" in body["detail"]
    # a target far enough away passes
    worker.card["take_profit"] = 64000.0
    assert (await ask(h, uuid.uuid4())).json()["ok"] is True


async def test_a_market_card_measures_reward_risk_from_the_last_price(h, worker):  # noqa: F811
    m = await h.mandate(instruments=(BTC,))
    await limits(h, m, min_rr="2.0")
    await h.redis.set("market:24h:BTCUSDT", json.dumps({"last_price": 60000}), ex=60)
    worker.card.update(stop_loss=58000.0, take_profit=63000.0)  # 1.5
    body = (await ask(h, uuid.uuid4())).json()
    assert body["code"] == "MANDATE_MIN_RR" and "R:R 1.5 below the mandate minimum 2.0" in body["detail"]
    await h.redis.delete("market:24h:BTCUSDT")
    assert (await ask(h, uuid.uuid4())).json()["ok"] is True  # no price: skipped


# --- a perp card that closes a short is an exit: never refused ---------------------------------------------------

PERP = "BTCUSDT-PERP.BINANCE"


async def short_perp_mandate(h):
    m = await h.mandate(instruments=(PERP,))
    await h.pool.execute("UPDATE ai_mandates SET allow_perps = true, max_leverage = 5 WHERE id = $1", m.id)
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               quantity, current_price, status)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'SHORT', 60000, 0.01, 60000, 'OPEN')""",
        uuid.uuid4().hex, m.instance_id, PERP)
    return m


@pytest.mark.parametrize("case", ["loss_latched", "target_met", "low_confidence", "no_stop_or_target"])
async def test_a_card_closing_a_short_is_allowed_whatever_the_filters_and_the_session(h, worker, case):  # noqa: F811
    m = await short_perp_mandate(h)
    # BUY + reduce_pct on a perp is a reduction of a short: not an entry
    worker.card.update(instrument_id=PERP, side="BUY", reduce_pct=100, quantity=None, stop_loss=None, take_profit=None)
    if case == "loss_latched":
        await limits(h, m, daily_loss_limit_pct="3")
        await latch(h, m, "loss_latched_at")
    elif case == "target_met":
        await limits(h, m, daily_profit_target_pct="2")
        await latch(h, m, "target_hit_at")
    elif case == "low_confidence":
        await limits(h, m, min_conviction="0.90")
        worker.card["confidence"] = 0.1
    else:
        await limits(h, m, min_rr="3.0", min_conviction="0.90")
    body = (await ask(h, uuid.uuid4())).json()
    assert body["ok"] is True, body


    done = await approve(h, uuid.uuid4())
    assert done.status_code == 200, done.text


async def test_the_picker_does_not_offer_a_latched_mandate(h):
    from cryptonite_api.mandate_choice import choose_mandate_remedy  # noqa: PLC0415

    a = await h.mandate(instruments=(BTC,))
    b = await h.mandate(instruments=(BTC,))
    await limits(h, a, daily_loss_limit_pct="3")
    await latch(h, a, "loss_latched_at")
    remedy = await choose_mandate_remedy(h.pool, BTC, D("5"), {})
    blocked = {c["id"]: c["blocked"] for c in remedy["candidates"]}
    assert "daily loss limit reached" in blocked[str(a.id)]
    assert blocked[str(b.id)] is None


# --- the Inbox debate approval -------------------------------------------------------------------------------------

async def pending_debate(h, conviction: float) -> uuid.UUID:
    run_id = uuid.uuid4()
    await h.pool.execute("INSERT INTO debate_runs (id, instrument_id, status, trigger_source) "
                         "VALUES ($1, $2, 'PENDING_APPROVAL', 'test')", run_id, BTC)
    await h.pool.execute(
        """INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_level, conviction_score,
                                            allocation_pct, reasoning)
           VALUES ($1, $2, 'BUY', 'Overweight', $3, 5, 'r')""", run_id, BTC, conviction)
    await h.redis.set("market:24h:BTCUSDT", json.dumps({"last_price": 50000}), ex=60)
    return run_id


async def test_approving_a_debate_applies_the_same_refusals(h):
    m = await h.mandate(level=1)
    await limits(h, m, min_conviction="0.70")
    run_id = await pending_debate(h, 0.62)
    async with h.client() as c:
        sizing = (await c.get(f"/api/v1/agents/debates/{run_id}/sizing")).json()
        res = await c.post(f"/api/v1/agents/debates/{run_id}/approve", headers=h.headers)
    assert sizing["ok"] is False and sizing["code"] == "MANDATE_MIN_CONVICTION", sizing
    assert res.status_code == 409 and "conviction 0.62 below the mandate minimum 0.70" in res.json()["detail"]
    await latch(h, m, "loss_latched_at")
    await limits(h, m, min_conviction=None)
    async with h.client() as c:
        res = await c.post(f"/api/v1/agents/debates/{run_id}/approve", headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("MANDATE_DAILY_LOSS_LATCHED: daily loss limit")
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
    assert await h.pool.fetchval("SELECT status FROM debate_runs WHERE id = $1", run_id) == "PENDING_APPROVAL"


# --- the mandate API ---------------------------------------------------------------------------------------------

async def test_the_mandate_views_carry_todays_session(h):
    m = await h.mandate(level=1)
    await limits(h, m, daily_loss_limit_pct="3", daily_profit_target_pct="2", min_rr="2")
    async with h.client() as c:
        fresh = (await c.get(f"/api/v1/ai/mandates/{m.id}")).json()
    s = fresh["session"]
    assert (s["verdict"], s["start_equity"], s["pnl"], s["loss_limit_usd"], s["target_usd"], s["lock_usd"]) == (
        "ACTIVE", None, "0", "300", "200", "100")
    assert (fresh["min_rr"], fresh["daily_loss_limit_pct"], fresh["profit_lock_pct"], fresh["min_conviction"]) == (
        "2", "3", "50", None)
    await latch(h, m, "loss_latched_at")
    async with h.client() as c:
        listed = {v["id"]: v for v in (await c.get("/api/v1/ai/mandates")).json()["mandates"]}
    s = listed[str(m.id)]["session"]
    assert s["verdict"] == "LOSS_LATCHED" and s["loss_latched_at"] is not None and s["start_equity"] == "10000"
    assert s["resets_at"].endswith("T00:00:00+00:00")


async def test_tightening_is_free_and_loosening_needs_totp(h):
    m = await h.mandate(level=1)
    url = f"/api/v1/ai/mandates/{m.id}"
    async with h.client() as c:
        tighten = await c.patch(url, headers=h.headers, json={
            "min_conviction": "0.7", "min_rr": "2", "daily_loss_limit_pct": "3", "daily_profit_target_pct": "2",
            "profit_lock_pct": "60"})
        assert tighten.status_code == 200, tighten.text
        for loosen in ({"min_conviction": "0.6"}, {"min_rr": "1.5"}, {"daily_loss_limit_pct": "4"},
                       {"daily_profit_target_pct": "3"}, {"profit_lock_pct": "40"},
                       {"clear_limits": ["daily_loss_limit_pct"]}):
            await h.redis.delete(f"ai:totp_failures:user:{h.operator_id}")  # 5 misses lock the code out
            res = await c.patch(url, headers=h.headers, json=loosen)
            assert res.status_code == 401 and "TOTP_REQUIRED" in res.text, (loosen, res.text)
        cleared = await c.patch(url, headers=h.headers,
                                json={"clear_limits": ["min_rr", "daily_profit_target_pct"], "totp_code": "000000"})
        assert cleared.status_code == 200, cleared.text
        bad = await c.patch(url, headers=h.headers, json={"daily_loss_limit_pct": "0.5"})  # below one trade's risk
    body = cleared.json()
    assert (body["min_rr"], body["daily_profit_target_pct"], body["min_conviction"], body["profit_lock_pct"]) == (
        None, None, "0.7", "60")
    assert bad.status_code == 422
    events = await h.pool.fetch("SELECT detail FROM ai_autonomy_events WHERE mandate_id = $1 AND kind = 'EDITED' "
                                "ORDER BY id", m.id)
    assert [json.loads(e["detail"])["risk_adding"] for e in events] == [False, True]


async def test_creating_a_mandate_stores_the_filters(h):
    body = {"name": f"f-{uuid.uuid4().hex[:6]}", "capital_budget_usd": "10000", "instruments": ["BTCUSDT"],
            "max_position_pct": "10", "min_conviction": "0.75", "min_rr": "2.5", "daily_loss_limit_pct": "2",
            "totp_code": "000000"}
    async with h.client() as c:
        res = await c.post("/api/v1/ai/mandates", json=body, headers=h.headers)
    assert res.status_code == 201, res.text
    v = res.json()
    assert (v["min_conviction"], v["min_rr"], v["daily_loss_limit_pct"], v["daily_profit_target_pct"],
            v["profit_lock_pct"]) == ("0.75", "2.5", "2", None, "50")
