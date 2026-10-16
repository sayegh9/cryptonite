"""The order card's dry run says what approve would do, and writes nothing (WS-3A, 3A-api).

``GET .../cards/{widget_id}/preflight`` plans the card with the same code as ``approve`` (``_plan_card``). For every
refusal approve can give, preflight's code and detail must equal approve's on the same state. For a card that would
queue, preflight's summary is the one approve writes, and the size is the one it queues. Preflight leaves the card
PENDING and writes no note, approval, command or order. Runs on a throwaway migrated database and the Redis at
REDIS_URL, as test_mandate_card_truth does.
"""

import hashlib
import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_api.routes import brain as brain_routes
from cryptonite_api.routes.auth import COOKIE_NAME
from cryptonite_execution.approvals import record_operator_approval
from cryptonite_schemas.universe import INGEST_UNIVERSE_KEY
from migrated_db import migrated_database

BTC = "BTCUSDT.BINANCE"
SOL = "SOLUSDT.BINANCE"  # not in the harness universe: ingest does not feed it
PREFLIGHT = "/api/v1/agents/brain/sessions/{sid}/cards/{wid}/preflight"
APPROVE = "/api/v1/agents/brain/sessions/{sid}/cards/{wid}/approve"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_ws3a_preflight") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute(
        "TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_orders, paper_positions CASCADE")
    try:
        yield harness
    finally:
        await close_harness(harness)


@pytest.fixture
def worker(monkeypatch):
    """The Brain session worker holding one order card (``card-1``). The test shapes the card; every request the
    route makes to the worker is recorded by method, and every note is recorded."""
    state = SimpleNamespace(card={"kind": "order_card", "id": "card-1", "instrument_id": BTC, "side": "BUY",
                                  "quantity": 0.01, "order_type": "MARKET", "limit_price": None, "allocation_pct": 5.0,
                                  "stop_loss": 58000.0, "take_profit": None, "status": "PENDING_APPROVAL",
                                  "trace_id": str(uuid.uuid4()), "command_id": None},
                            notes=[], methods=[])

    def handler(request: httpx.Request) -> httpx.Response:
        state.methods.append(request.method)
        if request.method == "GET":
            session = {"session": {}, "messages": [{"role": "assistant", "widgets": [state.card]}]}
            return httpx.Response(200, json=session)
        body = json.loads(request.content)
        state.notes.append(body)
        if body.get("expect_status") and state.card["status"] != body["expect_status"]:
            return httpx.Response(409, json={"detail": "not pending"})
        state.card.update({k: body[k] for k in ("status", "command_id") if body.get(k)})
        return httpx.Response(200, json={"status": "ok"})

    real = httpx.AsyncClient
    monkeypatch.setattr(brain_routes.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return state


async def hold_position(pool, instance_id: str, qty: float, instrument_id: str = BTC) -> None:
    """An open position at its stored price: what the mandate's position limit counts (entry_room_pct)."""
    await pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               quantity, current_price, status)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'LONG', 50000, $4, 50000, 'OPEN')""",
        uuid.uuid4().hex, instance_id, instrument_id, qty)


async def hold_spot(pool, instance_id: str, qty: str, instrument_id: str = BTC) -> None:
    """A filled BUY on the mandate's own instance: what ``instance_spot_holdings`` nets as a holding."""
    await pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
               quantity, filled_qty, avg_fill_price, status)
           VALUES ($1, $2, 'PAPER-ACCOUNT-001', $3, 'BUY', 'MARKET', $4::numeric, $4::numeric, 50000, 'FILLED')""",
        f"hold-{uuid.uuid4().hex[:8]}", instance_id, instrument_id, qty)


async def snapshot(pool) -> dict:
    """Everything a dry run must leave alone."""
    return {
        "approvals": await pool.fetchval("SELECT count(*) FROM ai_approvals"),
        "commands": await pool.fetchval("SELECT count(*) FROM execution_commands"),
        "orders": await pool.fetchval("SELECT count(*) FROM paper_orders"),
        "mandates": [dict(r) for r in
                     await pool.fetch("SELECT id, status, autonomy_level FROM ai_mandates ORDER BY id")],
    }


async def ask(h, sid: uuid.UUID, *, wid: str = "card-1", mandate_id: uuid.UUID | None = None) -> httpx.Response:
    """The dry run, as the operator's browser asks it: a GET with the operator's cookie and no CSRF header."""
    params = {"mandate_id": str(mandate_id)} if mandate_id else None
    async with h.client() as c:
        return await c.get(PREFLIGHT.format(sid=sid, wid=wid), params=params)


async def approve(h, sid: uuid.UUID, *, wid: str = "card-1", mandate_id: uuid.UUID | None = None) -> httpx.Response:
    body = {"mandate_id": str(mandate_id)} if mandate_id else None
    async with h.client() as c:
        return await c.post(APPROVE.format(sid=sid, wid=wid), headers=h.headers, json=body)


async def assert_refused_like_approve(h, worker, *, code: str, needs_mandate: bool = False,
                                      mandate_id: uuid.UUID | None = None) -> dict:
    """Preflight says ``code`` with approve's own detail; approve refuses with that detail; nothing was written."""
    sid = uuid.uuid4()
    status_before = worker.card["status"]
    before = await snapshot(h.pool)
    pre = await ask(h, sid, mandate_id=mandate_id)
    assert pre.status_code == 200, pre.text
    body = pre.json()
    assert body["ok"] is False and body["code"] == code, body
    assert body["needs_mandate"] is needs_mandate, body
    assert await snapshot(h.pool) == before  # the dry run wrote nothing
    res = await approve(h, sid, mandate_id=mandate_id)
    assert res.status_code >= 400, res.text
    assert res.json()["detail"] == body["detail"]
    assert worker.card["status"] == status_before and worker.notes == []  # approve refused before the claim
    assert await snapshot(h.pool) == before
    return body


# --- Every refusal approve can give: the same code and detail, and no write ----------------------------------------

async def test_exit_monitor_down_is_refused_like_approve(h, worker):
    await h.mandate(instruments=(BTC,))
    await h.redis.delete("exit_monitor:heartbeat")
    body = await assert_refused_like_approve(h, worker, code="EXIT_MONITOR_DOWN")
    assert body["detail"].startswith("EXIT_MONITOR_DOWN: start the exit monitor")


async def test_a_buy_on_an_unfed_instrument_is_refused_like_approve(h, worker):
    worker.card.update(instrument_id=SOL)
    await assert_refused_like_approve(h, worker, code="SYMBOL_NOT_INGESTED")


async def test_an_unknown_ingest_universe_is_refused_like_approve(h, worker):
    await h.mandate(instruments=(BTC,))
    await h.redis.delete(INGEST_UNIVERSE_KEY)
    await assert_refused_like_approve(h, worker, code="INGEST_UNIVERSE_UNKNOWN")


async def test_a_buy_with_no_mandate_asks_for_one(h, worker):
    body = await assert_refused_like_approve(h, worker, code="NO_MANDATE", needs_mandate=True)
    assert body["detail"].startswith("NO_MANDATE: approve this card inside an ACTIVE mandate")


@pytest.mark.parametrize("status", ["PAUSED", "HALTED"])
async def test_a_buy_under_a_stopped_mandate_names_the_stop(h, worker, status):
    name = f"btc-{status.lower()}-{uuid.uuid4().hex[:6]}"
    await h.mandate(instruments=(BTC,), status=status, name=name)
    body = await assert_refused_like_approve(h, worker, code=f"MANDATE_{status}")
    assert body["detail"] == (f"MANDATE_{status}: {name} already covers {BTC}; "
                              "resume it in Mission Control, then approve this card")


async def test_a_buy_with_two_active_mandates_is_ambiguous(h, worker):
    await h.mandate(instruments=(BTC,))
    await h.mandate(instruments=(BTC,))
    await assert_refused_like_approve(h, worker, code="AMBIGUOUS_MANDATE")


async def test_a_named_mandate_that_does_not_exist_is_refused_like_approve(h, worker):
    await assert_refused_like_approve(h, worker, code="MANDATE_NOT_FOUND", mandate_id=uuid.uuid4())


async def test_a_card_that_is_no_longer_pending_is_not_pending(h, worker):
    worker.card["status"] = "REJECTED"
    body = await assert_refused_like_approve(h, worker, code="NOT_PENDING")
    assert body["detail"] == "The card is REJECTED"


async def test_a_buy_at_the_position_limit_is_refused_like_approve(h, worker):
    m = await h.mandate(instruments=(BTC,))
    await hold_position(h.pool, m.instance_id, 0.05)  # $2,500: the 25% limit of a $10,000 mandate
    await assert_refused_like_approve(h, worker, code="MANDATE_POSITION_LIMIT_REACHED")


async def test_a_buy_twin_of_an_approved_card_is_a_duplicate(h, worker):
    m = await h.mandate(instruments=(BTC,))
    async with h.pool.acquire() as conn:  # the same decision's card, approved earlier from another session
        await record_operator_approval(conn, m, "SUBMIT_ORDER", {"instrument_id": BTC, "side": "BUY"},
                                       summary="earlier", source="brain_card", source_ref="other-session:card-9",
                                       decided_by="operator:test", trace_id=uuid.UUID(worker.card["trace_id"]))
    await assert_refused_like_approve(h, worker, code="DUPLICATE_DECISION")


async def test_a_sell_with_nothing_held_is_spot_sell_not_held(h, worker):
    worker.card.update(side="SELL")
    await h.mandate(instruments=(BTC,))
    await assert_refused_like_approve(h, worker, code="SPOT_SELL_NOT_HELD")


async def test_a_sell_that_no_mandate_holds_is_no_mandate_without_the_offer(h, worker):
    worker.card.update(side="SELL")
    await assert_refused_like_approve(h, worker, code="NO_MANDATE", needs_mandate=False)


async def test_a_sell_with_two_stopped_holders_is_ambiguous(h, worker):
    worker.card.update(side="SELL")
    paused = await h.mandate(instruments=(BTC,), status="PAUSED")
    halted = await h.mandate(instruments=(BTC,), status="HALTED")
    await hold_spot(h.pool, paused.instance_id, "0.005")
    await hold_spot(h.pool, halted.instance_id, "0.005")
    await assert_refused_like_approve(h, worker, code="AMBIGUOUS_MANDATE")


# --- Cards that would queue: preflight says the size and the summary approve then uses -----------------------------

async def test_a_buy_that_would_size_is_ok_and_approve_queues_that_size(h, worker):
    name = f"core-{uuid.uuid4().hex[:6]}"
    m = await h.mandate(instruments=(BTC,), name=name)
    sid = uuid.uuid4()
    before = await snapshot(h.pool)
    pre = await ask(h, sid)
    assert pre.status_code == 200, pre.text
    body = pre.json()
    assert body == {"ok": True, "mandate": {"id": str(m.id), "name": name, "status": "ACTIVE"}, "side": "BUY",
                    "allocation_pct": 5.0, "budget_usd": 500.0, "clipped_from_pct": None, "sell_quantity": None,
                    "summary": f"BUY BTCUSDT.BINANCE MARKET at 5.0% of mandate {name} ($500)"}
    assert await snapshot(h.pool) == before and worker.notes == [] and worker.card["status"] == "PENDING_APPROVAL"

    res = await approve(h, sid)
    assert res.status_code == 200, res.text
    assert res.json()["mandate_id"] == str(m.id)
    assert body["summary"] in worker.notes[0]["content"]  # the claim note carries the summary the card showed
    [cmd] = await h.pool.fetch("SELECT payload FROM execution_commands")
    assert json.loads(cmd["payload"])["allocation_pct"] == body["allocation_pct"]


async def test_the_approve_notes_name_an_order_card_when_the_stored_instrument_predates_the_pattern(h, worker):
    """A card stored as "BTCUSDT" (no venue, so no match for INSTRUMENT_ID) still approves as BTC; its two note lines
    say "an order card" where they would echo the instrument. The approval record keeps the card's own summary."""
    name = f"core-{uuid.uuid4().hex[:6]}"
    m = await h.mandate(instruments=(BTC,), name=name)
    worker.card["instrument_id"] = "BTCUSDT"
    res = await approve(h, uuid.uuid4())
    assert res.status_code == 200, res.text
    assert res.json()["mandate_id"] == str(m.id)
    claim, queued = worker.notes[0]["content"], worker.notes[1]["content"]
    assert claim.startswith("Operator approved order card card-1: BUY an order card MARKET at ") and name in claim
    assert "BUY an order card MARKET" in queued and "BTCUSDT" not in claim + queued
    [approval] = await h.pool.fetch("SELECT summary FROM ai_approvals")
    assert approval["summary"].startswith("Brain order card: BUY BTCUSDT MARKET at ")


async def test_a_buy_clipped_to_the_room_left_says_so_and_approve_queues_the_clip(h, worker):
    name = f"core-{uuid.uuid4().hex[:6]}"
    m = await h.mandate(instruments=(BTC,), name=name)
    await hold_position(h.pool, m.instance_id, 0.046)  # $2,300 of the $2,500 limit: 2% left, the card asks 5%
    sid = uuid.uuid4()
    body = (await ask(h, sid)).json()
    assert body["ok"] is True and body["allocation_pct"] == 2.0 and body["clipped_from_pct"] == 5.0
    assert body["budget_usd"] == 200.0
    assert body["summary"] == f"BUY BTCUSDT.BINANCE MARKET at 2.00% of mandate {name} ($200)"

    res = await approve(h, sid)
    assert res.status_code == 200, res.text
    [cmd] = await h.pool.fetch("SELECT payload FROM execution_commands")
    assert json.loads(cmd["payload"])["allocation_pct"] == body["allocation_pct"]


async def test_a_sell_closing_a_stopped_holding_is_ok_with_the_quantity_approve_sells(h, worker):
    worker.card.update(side="SELL")
    m = await h.mandate(instruments=(BTC,), status="PAUSED", name="btc-paused")
    await hold_spot(h.pool, m.instance_id, "0.005")
    sid = uuid.uuid4()
    body = (await ask(h, sid)).json()
    assert body["ok"] is True and body["side"] == "SELL"
    assert body["mandate"] == {"id": str(m.id), "name": "btc-paused", "status": "PAUSED"}
    assert Decimal(body["sell_quantity"]) == Decimal("0.005") and body["clipped_from_pct"] is None
    assert "the whole holding" in body["summary"] and "btc-paused" in body["summary"]

    res = await approve(h, sid)
    assert res.status_code == 200, res.text
    [cmd] = await h.pool.fetch("SELECT payload, mandate_id FROM execution_commands")
    assert str(cmd["mandate_id"]) == str(m.id)
    assert Decimal(json.loads(cmd["payload"])["quantity"]) == Decimal(body["sell_quantity"])


# --- The dry run cannot claim, note or queue ------------------------------------------------------------------------

async def test_the_dry_run_never_claims_notes_or_queues_even_when_it_would_queue(h, worker, monkeypatch):
    async def must_not_write(*_args, **_kwargs):
        raise AssertionError("the dry run must not write")

    monkeypatch.setattr(brain_routes, "_worker_note", must_not_write)
    monkeypatch.setattr(brain_routes, "record_operator_approval", must_not_write)
    await h.mandate(instruments=(BTC,))
    before = await snapshot(h.pool)
    res = await ask(h, uuid.uuid4())
    assert res.status_code == 200 and res.json()["ok"] is True
    assert worker.methods == ["GET"] and worker.notes == [] and worker.card["status"] == "PENDING_APPROVAL"
    assert await snapshot(h.pool) == before


# --- Not-found, malformed input and auth: as approve answers them -------------------------------------------------

async def test_an_unknown_card_is_404_as_approve_is(h, worker):
    sid = uuid.uuid4()
    pre = await ask(h, sid, wid="card-404")
    res = await approve(h, sid, wid="card-404")
    assert pre.status_code == 404 and res.status_code == 404
    assert pre.json()["detail"] == res.json()["detail"] == "Order card not found"


async def test_an_overlong_card_id_is_422_as_approve_is(h, worker):
    sid = uuid.uuid4()
    pre = await ask(h, sid, wid="x" * 129)
    res = await approve(h, sid, wid="x" * 129)
    assert pre.status_code == 422 and res.status_code == 422


async def test_the_dry_run_needs_an_operator_session_and_no_csrf(h, worker):
    async with h.client() as c:
        c.cookies.clear()
        assert (await c.get(PREFLIGHT.format(sid=uuid.uuid4(), wid="card-1"))).status_code == 401
    viewer_sid, _ = await h.auth.create_session(h.operator_id, h.username, role="viewer")
    async with h.client() as c:
        c.cookies.set(COOKIE_NAME, viewer_sid)
        assert (await c.get(PREFLIGHT.format(sid=uuid.uuid4(), wid="card-1"))).status_code == 403
    await h.mandate(instruments=(BTC,))
    assert (await ask(h, uuid.uuid4())).status_code == 200  # the operator's cookie, a GET: no CSRF header


@pytest.mark.parametrize("scopes, status", [(["operator"], 200), (["agent"], 403)])
async def test_a_machine_token_reads_with_the_operator_scope_but_never_approves(h, worker, scopes, status):
    token = f"ws3a-{uuid.uuid4().hex}"
    await h.pool.execute("INSERT INTO machine_api_tokens (token_hash, name, scopes) VALUES ($1, $2, $3)",
                         hashlib.sha256(token.encode()).hexdigest(), f"ws3a-{uuid.uuid4().hex[:6]}", scopes)
    await h.mandate(instruments=(BTC,))
    sid = uuid.uuid4()
    async with h.client() as c:
        c.cookies.clear()
        c.headers["X-API-Token"] = token
        look = await c.get(PREFLIGHT.format(sid=sid, wid="card-1"))
        assert look.status_code == status, look.text
        if status == 200:
            assert look.json()["ok"] is True
        res = await c.post(APPROVE.format(sid=sid, wid="card-1"))
    assert res.status_code == 403  # approve stays human-only: an operator session, never a machine token
    assert worker.notes == [] and worker.card["status"] == "PENDING_APPROVAL"
