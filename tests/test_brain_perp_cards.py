"""Stage 4 (leverage plan 4.1, 4.3, 4.4, 4.8): perp order cards in the API. The dry run shows the card's LeveragePlan
(the same function and sizing margin the orchestrator uses); approve needs the operator's TOTP (ADR 0009 D3) and a
second confirmation at 8x and above, and queues the leverage, the stop and ISOLATED; a mandate without perps is offered
"Allow perps", a mandate maximum below the request "Raise max leverage". No Binance call: the mark is a Redis key."""

import json
import uuid
from decimal import Decimal

import pytest
from ai_api_harness import close_harness, make_harness
from migrated_db import migrated_database
from test_brain_card_preflight import APPROVE, PREFLIGHT, worker  # noqa: F401 - worker is a fixture

PERP = "BTCUSDT-PERP.BINANCE"
MARK_KEY = "perp:mark:BTCUSDT"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_perp_cards") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute(
        "TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_orders, paper_positions CASCADE")
    await harness.pool.execute(
        """INSERT INTO paper_accounts (account_id, venue, currency, starting_balance, cash_balance, total_equity,
               realized_pnl, daily_realized_pnl, daily_loss_latched, updated_at)
           VALUES ('PAPER-ACCOUNT-001', 'BINANCE', 'USDT', 100000, 100000, 100000, 0, 0, false, now())
           ON CONFLICT (account_id) DO UPDATE SET total_equity = 100000""")
    await harness.redis.set(MARK_KEY, json.dumps({"type": "perp.mark", "mark_price": "60000", "funding_rate": "0.0001",
                                                  "ts_event_ns": 1}), ex=300)
    try:
        yield harness
    finally:
        await harness.redis.delete(MARK_KEY)
        await close_harness(harness)


async def perp_mandate(h, *, allow_perps=True, max_leverage=10, level=1):
    mid = await h.pool.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, autonomy_level, status,
               allow_perps, max_leverage, direction)
           VALUES ($1, 10000, ARRAY[$2], 25, $3, 'ACTIVE', $4, $5, $6) RETURNING id""",
        f"perp-{uuid.uuid4().hex[:6]}", PERP, level, allow_perps, max_leverage,
        "BOTH" if allow_perps else "LONG")  # H4: a short card needs a mandate that allows shorts (PERP only)
    from cryptonite_execution.mandate_store import load_mandate
    from cryptonite_orchestrator.mandate_gate import ensure_mandate_instance

    async with h.pool.acquire() as conn:
        m = await load_mandate(conn, mid)
        await ensure_mandate_instance(conn, m, "PAPER-ACCOUNT-001")
    return m


def perp_card(worker, **kw):  # noqa: F811
    worker.card.update({"instrument_id": PERP, "side": "BUY", "allocation_pct": 2.5, "stop_loss": 57000.0,
                        "take_profit": 66000.0, "setup_grade": "A", "quantity": None, **kw})


async def ask(h, sid, **params):
    async with h.client() as c:
        return (await c.get(PREFLIGHT.format(sid=sid, wid="card-1"), params=params or None)).json()


async def approve(h, sid, **body):
    async with h.client() as c:
        return await c.post(APPROVE.format(sid=sid, wid="card-1"), headers=h.headers, json=body or None)


async def test_the_dry_run_shows_the_leverage_plan(h, worker):  # noqa: F811
    await perp_mandate(h)
    perp_card(worker)
    body = await ask(h, uuid.uuid4())
    assert body["ok"] is True and body["needs_totp"] is True, body
    lp = body["leverage_plan"]
    # $250 margin, 5% stop + 0.3% slippage (+0.1% sizing margin) + 0.1% fees: 99 / (250 x 5.5%) = 7.2 -> 7x
    assert (lp["leverage"], lp["binding_ceiling"], lp["margin_mode"]) == (7, "risk at stop", "ISOLATED")
    # the quantity is rounded down to the lot step, so compare to the cent
    assert round(Decimal(lp["notional_usd"])) == Decimal("1750")
    assert round(Decimal(lp["loss_if_liquidated_usd"])) == Decimal("250")
    assert Decimal(lp["liquidation_price"]) < Decimal("57000")  # the stop fires first
    assert round(Decimal(lp["funding_8h_usd"]), 3) == Decimal("-0.175")  # a long pays a positive rate
    assert body["high_leverage_confirm"] is False and body["leverage_remedy"] is None
    assert "LONG BTCUSDT-PERP.BINANCE MARKET 7x ISOLATED" in body["summary"]
    lower = await ask(h, uuid.uuid4(), leverage=5)  # the operator picks a lower pill
    assert lower["leverage_plan"]["leverage"] == 5 and lower["leverage_plan"]["capped"] is False, lower
    # 3x of $250 is $750, under the dispatcher's 1% of equity minimum order: refused, never rounded up
    tiny = await ask(h, uuid.uuid4(), leverage=3)
    assert tiny["ok"] is False and tiny["code"] == "ORDER_BELOW_MIN_NOTIONAL"


async def test_approve_needs_totp_and_queues_leverage_stop_and_isolated(h, worker):  # noqa: F811
    await perp_mandate(h)
    perp_card(worker)
    sid = uuid.uuid4()
    res = await approve(h, sid)
    assert res.status_code == 401, res.text
    assert worker.card["status"] == "PENDING_APPROVAL" and worker.notes == []  # refused before the claim
    res = await approve(h, sid, totp_code=h.totp.now(), leverage=5)
    assert res.status_code == 200, res.text
    payload = await h.pool.fetchval("SELECT payload FROM execution_commands WHERE command_id = $1::uuid",
                                    res.json()["command_id"])
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert (payload["leverage"], Decimal(str(payload["stop_loss"])), payload["margin_mode"], payload["side"]) == (
        5, Decimal("57000.0"), "ISOLATED", "BUY")
    assert payload["setup_grade"] == "A" and payload["allocation_pct"] == 2.5


async def test_8x_and_above_needs_a_second_confirmation(h, worker):  # noqa: F811
    await perp_mandate(h)
    perp_card(worker, stop_loss=59100.0)  # 1.5% stop: the room binds at 9x
    sid = uuid.uuid4()
    pre = await ask(h, sid)
    assert pre["leverage_plan"]["leverage"] == 9 and pre["high_leverage_confirm"] is True, pre
    res = await approve(h, sid, totp_code=h.totp.now())
    assert res.status_code == 409 and res.json()["detail"].startswith("HIGH_LEVERAGE_CONFIRMATION_REQUIRED")
    res = await approve(h, sid, totp_code=h.totp.now(), confirm_high_leverage=True)
    assert res.status_code == 200, res.text


async def test_a_short_card(h, worker):  # noqa: F811
    await perp_mandate(h)
    perp_card(worker, side="SELL", stop_loss=63000.0, take_profit=54000.0)
    body = await ask(h, uuid.uuid4())
    assert body["ok"] is True and body["leverage_plan"]["side"] == "SELL"
    assert Decimal(body["leverage_plan"]["liquidation_price"]) > Decimal("63000")
    assert "SHORT BTCUSDT-PERP.BINANCE" in body["summary"]


async def test_a_mandate_without_perps_offers_allow_perps(h, worker):  # noqa: F811
    m = await perp_mandate(h, allow_perps=False, max_leverage=1)
    perp_card(worker)
    body = await ask(h, uuid.uuid4())
    assert body["ok"] is False and body["code"] == "MANDATE_PERPS_NOT_ALLOWED"
    assert body["remedy"] == {"kind": "ALLOW_PERPS", "mandate_id": str(m.id), "mandate_name": m.name,
                              "autonomy_level": 1, "max_leverage_after": 3}


async def test_the_mandate_maximum_offers_raise_max_leverage(h, worker):  # noqa: F811
    m = await perp_mandate(h, max_leverage=3)
    perp_card(worker, leverage=7, allocation_pct=5, stop_loss=59100.0)  # $500 margin, risk allows 9x
    body = await ask(h, uuid.uuid4())
    assert body["leverage_plan"]["leverage"] == 3 and body["leverage_plan"]["binding_ceiling"] == "mandate maximum"
    assert body["leverage_remedy"] == {"kind": "RAISE_MAX_LEVERAGE", "mandate_id": str(m.id), "mandate_name": m.name,
                                       "from": 3, "to": 7}


async def test_no_mark_is_refused_with_the_plan(h, worker):  # noqa: F811
    await perp_mandate(h)
    perp_card(worker)
    await h.redis.delete(MARK_KEY)
    body = await ask(h, uuid.uuid4())
    assert body["ok"] is False and body["code"] == "NO_MARK_PRICE" and body["leverage_plan"]["reject"] == "NO_MARK_PRICE"


async def test_a_stop_too_close_to_liquidation_is_refused(h, worker):  # noqa: F811
    await perp_mandate(h)
    perp_card(worker, stop_loss=30000.0, allocation_pct=25)  # a 50% stop: even 1x leaves no buffer under the risk cap
    body = await ask(h, uuid.uuid4())
    assert body["ok"] is False and body["code"] in ("RISK_AT_STOP_EXCEEDED", "LIQUIDATION_BUFFER_TOO_THIN")
    assert body["leverage_plan"]["ceilings"]


async def _sol_mandate(h, *, markets=("PERP", "SPOT", "SPOT_MARGIN"), instruments=None, max_leverage=3):
    insts = list(instruments or ("SOLUSDT.BINANCE", "SOLUSDT-MARGIN.BINANCE", "SOLUSDT-PERP.BINANCE"))
    mid = await h.pool.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, markets, max_position_pct, autonomy_level,
               status, allow_perps, max_leverage, direction)
           VALUES ($1, 10000, $2, $3, 25, 1, 'ACTIVE', true, $4, 'BOTH') RETURNING id""",
        f"sol-{uuid.uuid4().hex[:6]}", insts, list(markets), max_leverage)
    from cryptonite_execution.mandate_store import load_mandate
    from cryptonite_orchestrator.mandate_gate import ensure_mandate_instance

    async with h.pool.acquire() as conn:
        m = await load_mandate(conn, mid)
        await ensure_mandate_instance(conn, m, "PAPER-ACCOUNT-001")
    return m


async def test_missing_perp_brackets_routes_to_spot_margin_for_preflight_and_approve(h, worker):  # noqa: F811
    import time

    from ai_api_harness import publish_test_universe
    from cryptonite_schemas.marks import quote_tick_key

    sol_perp = "SOLUSDT-PERP.BINANCE"
    sol_spot = "SOLUSDT.BINANCE"
    sol_margin = "SOLUSDT-MARGIN.BINANCE"
    await publish_test_universe(h.redis, symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
                                timeframes=("1m", "1h"), perps=("BTCUSDT", "ETHUSDT", "SOLUSDT"))
    await h.redis.set(quote_tick_key(sol_spot), json.dumps({
        "instrument_id": sol_spot, "bid_price": "71.32", "ask_price": "71.33", "arrival_ns": time.time_ns()}), ex=300)
    await h.redis.set("perp:mark:SOLUSDT", json.dumps({
        "type": "perp.mark", "mark_price": "71.38", "funding_rate": "0.0001", "ts_event_ns": 1}), ex=300)
    try:
        await _sol_mandate(h)
        perp_card(worker, instrument_id=sol_perp, side="SELL", allocation_pct=15, stop_loss=73.57,
                  take_profit=66.57, setup_grade="B", trace_id=str(uuid.uuid4()))
        sid = uuid.uuid4()
        body = await ask(h, sid)
        assert body["ok"] is True and body["needs_totp"] is True, body
        lp = body["leverage_plan"]
        assert lp["instrument_id"] == sol_margin
        assert lp["side"] == "SELL"
        assert lp["leverage"] == 2 and lp["margin_mode"] == "ISOLATED"
        assert lp["binding_ceiling"] == "risk at stop"
        assert lp["funding_8h_usd"] is None
        assert "SHORT SOLUSDT-MARGIN.BINANCE MARKET 2x ISOLATED" in body["summary"]
        assert body["tp1"] is not None

        res = await approve(h, sid, totp_code=h.totp.now())
        assert res.status_code == 200, res.text
        payload = await h.pool.fetchval("SELECT payload FROM execution_commands WHERE command_id = $1::uuid",
                                        res.json()["command_id"])
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["instrument_id"] == sol_margin
        assert payload["leverage"] == 2 and payload["margin_mode"] == "ISOLATED"
        assert payload["margin_plan"] == payload["leverage_plan"]
        assert "exit monitor closes the position there" in worker.notes[-1]["content"]

        # Same trace_id and side with a new card id must still be detected as a duplicate across -PERP / -MARGIN
        worker.card["id"] = "card-2"
        worker.card["status"] = "PENDING_APPROVAL"
        async with h.client() as c:
            dup = (await c.get(PREFLIGHT.format(sid=sid, wid="card-2"))).json()
        assert dup["ok"] is False and dup["code"] == "DUPLICATE_DECISION"

        # BUY -PERP card with missing brackets also routes via spot margin
        worker.card["id"] = "card-1"
        worker.card["status"] = "PENDING_APPROVAL"
        perp_card(worker, instrument_id=sol_perp, side="BUY", allocation_pct=15, stop_loss=69.0,
                  take_profit=76.0, setup_grade="B", trace_id=str(uuid.uuid4()))
        buy_body = await ask(h, uuid.uuid4())
        assert buy_body["ok"] is True, buy_body
        assert buy_body["leverage_plan"]["instrument_id"] == sol_margin
        assert buy_body["leverage_plan"]["side"] == "BUY"
        assert buy_body["leverage_plan"]["leverage"] == 1
        assert "LONG SOLUSDT-MARGIN.BINANCE MARKET 1x ISOLATED" in buy_body["summary"]
    finally:
        await h.redis.delete(quote_tick_key(sol_spot), "perp:mark:SOLUSDT")


async def test_missing_perp_brackets_without_spot_margin_keeps_no_leverage_brackets(h, worker):  # noqa: F811
    import time

    from ai_api_harness import publish_test_universe
    from cryptonite_schemas.marks import quote_tick_key

    sol_perp = "SOLUSDT-PERP.BINANCE"
    sol_spot = "SOLUSDT.BINANCE"
    await publish_test_universe(h.redis, symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
                                timeframes=("1m", "1h"), perps=("BTCUSDT", "ETHUSDT", "SOLUSDT"))
    await h.redis.set(quote_tick_key(sol_spot), json.dumps({
        "instrument_id": sol_spot, "bid_price": "71.32", "ask_price": "71.33", "arrival_ns": time.time_ns()}), ex=300)
    await h.redis.set("perp:mark:SOLUSDT", json.dumps({
        "type": "perp.mark", "mark_price": "71.38", "funding_rate": "0.0001", "ts_event_ns": 1}), ex=300)
    try:
        await _sol_mandate(h, markets=("PERP",), instruments=(sol_perp,))
        perp_card(worker, instrument_id=sol_perp, side="SELL", allocation_pct=15, stop_loss=73.57,
                  take_profit=66.57, setup_grade="B")
        body = await ask(h, uuid.uuid4())
        assert body["ok"] is False and body["code"] == "NO_LEVERAGE_BRACKETS"
    finally:
        await h.redis.delete(quote_tick_key(sol_spot), "perp:mark:SOLUSDT")


async def test_missing_spot_quote_on_margin_fallback_reports_no_mark_price(h, worker):  # noqa: F811
    from ai_api_harness import publish_test_universe

    sol_perp = "SOLUSDT-PERP.BINANCE"
    await publish_test_universe(h.redis, symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
                                timeframes=("1m", "1h"), perps=("BTCUSDT", "ETHUSDT", "SOLUSDT"))
    await h.redis.set("perp:mark:SOLUSDT", json.dumps({
        "type": "perp.mark", "mark_price": "71.38", "funding_rate": "0.0001", "ts_event_ns": 1}), ex=300)
    try:
        await _sol_mandate(h)
        perp_card(worker, instrument_id=sol_perp, side="SELL", allocation_pct=15, stop_loss=73.57,
                  take_profit=66.57, setup_grade="B")
        body = await ask(h, uuid.uuid4())
        assert body["ok"] is False and body["code"] == "NO_MARK_PRICE"
        assert body["leverage_plan"]["instrument_id"] == "SOLUSDT-MARGIN.BINANCE"
    finally:
        await h.redis.delete("perp:mark:SOLUSDT")

