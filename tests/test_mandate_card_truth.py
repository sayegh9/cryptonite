"""The order card and the mandate editor tell the operator the true reason a request is refused (WS-2E, 2E-api).

2026-10-15 incident: approving a BTC card while only a PAUSED mandate covered BTC returned NO_MANDATE, so the card
opened its inline create-mandate form. create_mandate checked the TOTP code first and only then refused with 409
MANDATE_PAUSED, so the operator's code was spent on a request that could never succeed. Every retry re-sent the used
code and got TOTP_REQUIRED, which made TOTP look broken. Now the stop is checked before the code is spent, and a code
that was already used says TOTP_REUSED.

Runs on a throwaway migrated database (``cryptonite_ci_truth_*``) and the Redis at REDIS_URL, as test_mandate_stop_guard does.
"""

import json
import time
import uuid
from decimal import Decimal

import httpx
import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_api.routes import ai as ai_routes
from cryptonite_execution.approvals import create_approval
from cryptonite_schemas import notifier as contract
from cryptonite_schemas.mandate import ActionClass
from migrated_db import migrated_database
from test_brain_card_sell import run_seeded_poller

BTC = "BTCUSDT.BINANCE"
APPROVE = "/api/v1/agents/brain/sessions/{sid}/cards/card-1/approve"
REUSED = "TOTP_REUSED: this code was already used (each code works once); wait for the next code from your authenticator"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_truth") as url:
        yield url


@pytest.fixture
async def h(db_url):
    # No 000000 bypass here: every code in this module is a real one, or a wrong one on purpose
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute(
        "TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_orders, paper_positions CASCADE")
    try:
        yield harness
    finally:
        await close_harness(harness)


def create_body(*instruments: str, totp: str, name: str | None = None) -> dict:
    return {"name": name or f"truth-{uuid.uuid4().hex[:6]}", "capital_budget_usd": "10000", "instruments": list(instruments),
            "max_position_pct": "10", "totp_code": totp}


def _card_worker(monkeypatch, side: str):
    """The Brain session worker as the approve route sees it: one card on BTC of ``side``. Records every note it gets."""
    from cryptonite_api.routes import brain as brain_routes

    card = {"kind": "order_card", "id": "card-1", "instrument_id": BTC, "side": side, "quantity": 0.01,
            "order_type": "MARKET", "limit_price": None, "allocation_pct": 5.0, "stop_loss": 58000.0, "take_profit": None,
            "status": "PENDING_APPROVAL", "trace_id": str(uuid.uuid4()), "command_id": None}
    notes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"session": {}, "messages": [{"role": "assistant", "widgets": [card]}]})
        body = json.loads(request.content)
        notes.append(body)
        if body.get("expect_status") and card["status"] != body["expect_status"]:
            return httpx.Response(409)
        card.update({k: body[k] for k in ("status", "command_id") if body.get(k)})
        return httpx.Response(200, json={"status": "ok"})

    real = httpx.AsyncClient
    monkeypatch.setattr(brain_routes.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return card, notes


@pytest.fixture
def brain_card(monkeypatch):
    return _card_worker(monkeypatch, "BUY")


@pytest.fixture
def sell_card(monkeypatch):
    return _card_worker(monkeypatch, "SELL")


# --- The card: a stopped mandate is named, not reported as "no mandate" -------------------------------------------

@pytest.mark.parametrize("status", ["PAUSED", "HALTED"])
async def test_approving_a_buy_card_under_a_stopped_mandate_names_the_stop(h, brain_card, status):
    card, notes = brain_card
    name = f"btc-{status.lower()}-{uuid.uuid4().hex[:6]}"
    await h.mandate(instruments=(BTC,), status=status, name=name)
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 409
    assert res.json()["detail"] == (f"MANDATE_{status}: {name} already covers {BTC}; "
                                    "resume it in Mission Control, then approve this card")
    assert card["status"] == "PENDING_APPROVAL" and notes == []  # nothing claimed, nothing queued
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0


async def test_the_card_hint_keeps_a_mandate_name_that_holds_a_semicolon(h, brain_card):
    # Names are free text: the hint is cut at the helper's own tail, not at the first ";" in the name
    await h.mandate(instruments=(BTC,), status="PAUSED", name="btc; core")
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 409
    assert res.json()["detail"] == (f"MANDATE_PAUSED: btc; core already covers {BTC}; "
                                    "resume it in Mission Control, then approve this card")


async def test_approving_a_buy_card_with_no_mandate_still_says_no_mandate(h, brain_card):
    card, notes = brain_card
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("NO_MANDATE")
    assert card["status"] == "PENDING_APPROVAL" and notes == []


# --- The mandate editor: a refused create does not spend the operator's code --------------------------------------

async def test_creating_a_mandate_under_a_stopped_mandate_does_not_spend_the_code(h):
    name = f"btc-stopped-{uuid.uuid4().hex[:6]}"
    await h.mandate(instruments=(BTC,), status="PAUSED", name=name)
    code = h.totp.now()
    async with h.client() as c:
        refused = await c.post("/api/v1/ai/mandates", json=create_body("BTCUSDT", totp=code), headers=h.headers)
        assert refused.status_code == 409
        assert refused.json()["detail"] == f"MANDATE_PAUSED: {name} already covers {BTC}; resume or edit it instead"
        assert await h.pool.fetchval("SELECT count(*) FROM totp_replay_cache WHERE user_id = $1", h.operator_id) == 0
        # The same code still opens a free instrument: the refusal did not use it up
        accepted = await c.post("/api/v1/ai/mandates", json=create_body("ETHUSDT", totp=code), headers=h.headers)
    assert accepted.status_code == 201, accepted.text


# --- TOTP: a used code says TOTP_REUSED, and neither reuse nor a wrong code is counted the wrong way ----------------

async def test_a_totp_code_used_twice_is_refused_as_reused_and_never_locks_the_operator_out(h):
    key = f"ai:totp_failures:user:{h.operator_id}"
    code = h.totp.now()
    async with h.client() as c:
        first = await c.post("/api/v1/ai/mandates", json=create_body("ETHUSDT", totp=code), headers=h.headers)
        assert first.status_code == 201, first.text
        for _ in range(7):  # more reuses than the 5-failure lockout allows: none of them counts
            again = await c.post("/api/v1/ai/mandates", json=create_body("ETHUSDT", totp=code), headers=h.headers)
            assert again.status_code == 401 and again.json()["detail"] == REUSED
    assert int(await h.redis.get(key) or 0) == 0
    assert await h.pool.fetchval("SELECT count(*) FROM ai_mandates WHERE status <> 'ARCHIVED'") == 1


async def test_a_wrong_code_is_refused_as_required_and_counted(h):
    key = f"ai:totp_failures:user:{h.operator_id}"
    now = int(time.time())
    valid = {h.totp.at(now + offset) for offset in (-30, 0, 30)}
    wrong = next(c for c in ("000001", "000002") if c not in valid)  # 000000 is the dev code, never a real one
    async with h.client() as c:
        res = await c.post("/api/v1/ai/mandates", json=create_body("ETHUSDT", totp=wrong), headers=h.headers)
    assert res.status_code == 401 and res.json()["detail"].startswith("TOTP_REQUIRED")
    assert int(await h.redis.get(key) or 0) == 1


async def test_a_code_recorded_by_a_concurrent_submit_is_reused_not_wrong(h, monkeypatch):
    """Two submits of one fresh code race: the second one's check ran before the first recorded the step, so its
    insert is refused. The step is then recorded, which makes it a reuse (not counted), not a wrong code."""
    key = f"ai:totp_failures:user:{h.operator_id}"
    code = h.totp.now()
    async with h.client() as c:
        first = await c.post("/api/v1/ai/mandates", json=create_body("ETHUSDT", totp=code), headers=h.headers)
    assert first.status_code == 201, first.text
    real = ai_routes._totp_step_used
    seen: list[int] = []

    async def check_before_the_other_submit_recorded_it(request, user_id, step):
        seen.append(step)
        return False if len(seen) == 1 else await real(request, user_id, step)

    monkeypatch.setattr(ai_routes, "_totp_step_used", check_before_the_other_submit_recorded_it)
    async with h.client() as c:
        again = await c.post("/api/v1/ai/mandates", json=create_body("ETHUSDT", totp=code), headers=h.headers)
    assert again.status_code == 401 and again.json()["detail"] == REUSED
    assert len(seen) == 2
    assert int(await h.redis.get(key) or 0) == 0


# --- A SELL closes a stopped mandate's holding; the other sides and the ambiguous case stay as they were ---------------

XRP = "XRPUSDT.BINANCE"


async def hold(pool, instance_id: str, qty: str, instrument_id: str = BTC) -> None:
    """A filled BUY on the mandate's own instance: what ``instance_spot_holdings`` nets as a holding."""
    await pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type, quantity,
               filled_qty, avg_fill_price, status)
           VALUES ($1, $2, 'PAPER-ACCOUNT-001', $3, 'BUY', 'MARKET', $4::numeric, $4::numeric, 50000, 'FILLED')""",
        f"hold-{uuid.uuid4().hex[:8]}", instance_id, instrument_id, qty)


@pytest.mark.parametrize("status", ["PAUSED", "HALTED"])
async def test_a_sell_closing_a_stopped_mandates_holding_is_approved_and_completes(h, sell_card, status):
    card, _ = sell_card
    m = await h.mandate(instruments=(BTC,), status=status, name=f"btc-{status.lower()}-{uuid.uuid4().hex[:6]}")
    await hold(h.pool, m.instance_id, "0.005")
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 200, res.text
    assert res.json()["mandate_id"] == str(m.id) and card["status"] == "APPROVED"
    cmds = await h.pool.fetch("SELECT * FROM execution_commands")
    assert len(cmds) == 1 and str(cmds[0]["mandate_id"]) == str(m.id)
    payload = json.loads(cmds[0]["payload"])
    assert payload["side"] == "SELL" and Decimal(payload["quantity"]) == Decimal("0.005")  # the whole holding
    # The orchestrator's poller takes the command through check_mandate and the dispatcher: not refused downstream
    row, engine = await run_seeded_poller(h.pool, cmds[0]["command_id"], Decimal("0.005"))
    assert row["status"] == "COMPLETED", row["error_message"]
    assert engine.dispatcher.positions.get(BTC, Decimal("0")) == 0


async def test_a_sell_with_no_holding_behind_a_stopped_mandate_is_still_no_mandate(h, sell_card):
    card, notes = sell_card
    await h.mandate(instruments=(BTC,), status="PAUSED")
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("NO_MANDATE")
    assert card["status"] == "PENDING_APPROVAL" and notes == []
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_a_buy_on_a_paused_mandate_that_holds_the_instrument_is_still_mandate_paused(h, brain_card):
    card, notes = brain_card
    name = f"btc-paused-{uuid.uuid4().hex[:6]}"
    m = await h.mandate(instruments=(BTC,), status="PAUSED", name=name)
    await hold(h.pool, m.instance_id, "0.005")
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 409
    assert res.json()["detail"] == (f"MANDATE_PAUSED: {name} already covers {BTC}; "
                                    "resume it in Mission Control, then approve this card")
    assert card["status"] == "PENDING_APPROVAL" and notes == []
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_a_sell_with_two_stopped_mandates_holding_the_instrument_is_ambiguous(h, sell_card):
    card, notes = sell_card
    paused = await h.mandate(instruments=(BTC,), status="PAUSED")
    halted = await h.mandate(instruments=(BTC,), status="HALTED")
    await hold(h.pool, paused.instance_id, "0.005")
    await hold(h.pool, halted.instance_id, "0.005")
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("AMBIGUOUS_MANDATE")
    assert card["status"] == "PENDING_APPROVAL" and notes == []
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_a_sell_on_an_active_mandate_that_holds_none_is_closed_in_the_stopped_mandate_that_holds_it(h, sell_card):
    card, _ = sell_card
    card.update(instrument_id=XRP)
    active = await h.mandate(instruments=(XRP,))  # allows XRP, holds none of it
    stopped = await h.mandate(instruments=(XRP,), status="PAUSED")
    await hold(h.pool, stopped.instance_id, "40", XRP)
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 200, res.text
    assert res.json()["mandate_id"] == str(stopped.id) and res.json()["mandate_id"] != str(active.id)
    assert card["status"] == "APPROVED"
    cmds = await h.pool.fetch("SELECT * FROM execution_commands")
    assert len(cmds) == 1 and str(cmds[0]["mandate_id"]) == str(stopped.id)
    payload = json.loads(cmds[0]["payload"])
    assert payload["side"] == "SELL" and Decimal(payload["quantity"]) == Decimal("40")  # the whole holding


async def test_an_active_mandate_that_holds_the_sell_is_used_as_before_even_with_a_stopped_holder(h, sell_card):
    card, _ = sell_card
    card.update(instrument_id=XRP)
    active = await h.mandate(instruments=(XRP,))
    stopped = await h.mandate(instruments=(XRP,), status="PAUSED")
    await hold(h.pool, active.instance_id, "40", XRP)
    await hold(h.pool, stopped.instance_id, "10", XRP)
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 200, res.text
    assert res.json()["mandate_id"] == str(active.id)
    cmds = await h.pool.fetch("SELECT * FROM execution_commands")
    assert len(cmds) == 1 and str(cmds[0]["mandate_id"]) == str(active.id)
    assert Decimal(json.loads(cmds[0]["payload"])["quantity"]) == Decimal("40")


async def test_a_sell_no_mandate_holds_stays_spot_sell_not_held(h, sell_card):
    card, notes = sell_card
    card.update(instrument_id=XRP)
    await h.mandate(instruments=(XRP,))  # allows XRP, holds none, and nothing stopped holds it
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("SPOT_SELL_NOT_HELD")
    assert card["status"] == "PENDING_APPROVAL" and notes == []
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_a_sell_with_two_stopped_holders_behind_an_active_mandate_that_holds_none_is_ambiguous(h, sell_card):
    card, notes = sell_card
    card.update(instrument_id=XRP)
    await h.mandate(instruments=(XRP,))
    paused = await h.mandate(instruments=(XRP,), status="PAUSED")
    halted = await h.mandate(instruments=(XRP,), status="HALTED")
    await hold(h.pool, paused.instance_id, "40", XRP)
    await hold(h.pool, halted.instance_id, "40", XRP)
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("AMBIGUOUS_MANDATE")
    assert card["status"] == "PENDING_APPROVAL" and notes == []
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


# --- The Telegram approval path: a reused code is not counted and is answered TOTP_REQUIRED -------------------------

TG_SECRET = "ws2e-notifier-secret"
TG_CHAT = 555001


async def tg_post(h, approval_id, body: dict):
    """A notifier-signed decision, as the bot relays it (machine call, no operator session)."""
    path = f"/api/v1/ai/notifier/approvals/{approval_id}"
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", **contract.sign_request(TG_SECRET, "POST", path, raw)}
    async with h.client() as c:
        c.cookies.clear()
        return await c.post(path, content=raw, headers=headers)


async def test_a_reused_code_on_telegram_is_not_counted_and_is_answered_totp_required(h, monkeypatch):
    monkeypatch.setenv("NOTIFIER_HMAC_SECRET", TG_SECRET)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", str(TG_CHAT))
    monkeypatch.setenv("NOTIFIER_OPERATOR_USERNAME", h.username)
    m = await h.mandate(level=1)
    async with h.pool.acquire() as conn:
        first, second = [await create_approval(conn, m, ActionClass.ADD_RISK, "SUBMIT_ORDER",
                                               {"instrument_id": BTC, "side": "BUY"}, summary="s", source="test",
                                               source_ref=uuid.uuid4().hex) for _ in range(2)]
    code = h.totp.now()
    ok = await tg_post(h, first["id"], {"decision": "APPROVE", "chat_id": str(TG_CHAT), "totp_code": code})
    assert ok.status_code == 200 and ok.json()["status"] == "APPROVED"
    for _ in range(7):  # more reuses than the 5-failure lockout allows: none of them counts
        again = await tg_post(h, second["id"], {"decision": "APPROVE", "chat_id": str(TG_CHAT), "totp_code": code})
        assert again.status_code == 401 and again.json()["detail"] == "TOTP_REQUIRED"
    assert int(await h.redis.get(f"ai:totp_failures:telegram:{h.username}") or 0) == 0
    assert await h.pool.fetchval("SELECT status FROM ai_approvals WHERE id = $1", second["id"]) == "PENDING"
