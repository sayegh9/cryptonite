"""H1 approval routes attach protective stops before queuing risk-adding orders."""

import json
import time
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_api.routes import agents as agents_routes
from cryptonite_api.routes import brain as brain_routes
from cryptonite_execution.approvals import create_approval
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_execution.risk import OrderIntent
from cryptonite_orchestrator.mandate_gate import gate_ai_order
from cryptonite_schemas.mandate import ActionClass
from cryptonite_schemas.marks import quote_tick_key
from migrated_db import migrated_database
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_fresh_dispatcher

BTC = "BTCUSDT.BINANCE"
PERP = "BTCUSDT-PERP.BINANCE"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_h1_approval") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute(
        "TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_orders, paper_positions CASCADE")
    await harness.pool.execute("DELETE FROM scan_runs")
    await harness.pool.execute("DELETE FROM scan_specs")
    await harness.pool.execute("""
        INSERT INTO instruments (instrument_id, raw_symbol, venue, market_type, base_currency, quote_currency,
                                 price_precision, size_precision, price_increment, size_increment, min_notional,
                                 status, is_active)
        VALUES ($1, 'BTCUSDT', 'BINANCE', 'SPOT', 'BTC', 'USDT', 2, 6, 0.01, 0.000001, 10, 'TRADING', true)
        ON CONFLICT (instrument_id) DO UPDATE SET price_increment = EXCLUDED.price_increment, is_active = true
    """, BTC)
    try:
        yield harness
    finally:
        await harness.redis.delete(quote_tick_key(BTC), "perp:mark:BTCUSDT")
        await close_harness(harness)


async def fresh_spot_quote(h, *, bid="49999", ask="50001"):
    await h.redis.set(quote_tick_key(BTC), json.dumps({
        "instrument_id": BTC, "bid_price": bid, "ask_price": ask, "arrival_ns": time.time_ns(),
    }), ex=60)


async def spot_mandate(h):
    mandate = await h.mandate(instruments=(BTC,), budget=10000)
    await h.pool.execute("UPDATE ai_mandates SET min_rr = NULL WHERE id = $1", mandate.id)
    return mandate


async def perp_mandate(h):
    mid = await h.pool.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, autonomy_level,
                   status, allow_perps, markets, max_leverage, direction)
           VALUES ($1, 10000, ARRAY[$2], 25, 1, 'ACTIVE', true, ARRAY['PERP'], 10, 'BOTH') RETURNING id""",
        f"h1-perp-{uuid.uuid4().hex[:6]}", PERP)
    async with h.pool.acquire() as conn:
        mandate = await load_mandate(conn, mid)
        from cryptonite_orchestrator.mandate_gate import ensure_mandate_instance
        await ensure_mandate_instance(conn, mandate, "PAPER-ACCOUNT-001")
    return mandate


async def insert_debate_candidate(h, mandate_id, *, stop="49000", target="52000", with_candidate=True):
    spec_id, version_id, scan_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    candidate_id = uuid.uuid4() if with_candidate else None
    run_id = uuid.uuid4()
    if with_candidate:
        await h.pool.execute("INSERT INTO scan_specs (id, slug, name) VALUES ($1, $2, $3)",
                             spec_id, f"h1-{spec_id.hex}", "H1 candidate")
        await h.pool.execute(
            """INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
               VALUES ($1, $2, 1, 'h1-test', '{}'::jsonb, '[]'::jsonb)""", version_id, spec_id)
        await h.pool.execute("UPDATE scan_specs SET active_version_id = $2 WHERE id = $1", spec_id, version_id)
        await h.pool.execute(
            """INSERT INTO scan_runs (id, spec_version_id, spec_hash, scanner_version, indicator_version,
                   input_watermark_ns, source_partitions, source_partition_checksums, trigger_reason)
               VALUES ($1, $2, 'h1-test', 'test', 'test', 1, '{}'::jsonb, '{}'::jsonb, 'MANUAL')""",
            scan_id, version_id)
        await h.pool.execute(
            """INSERT INTO signal_candidates (id, scan_run_id, spec_version_id, instrument_id, timeframe,
                   bar_open_ns, bar_close_inclusive_ns, interval_end_exclusive_ns, matched_rules, metrics,
                   suggested_entry, suggested_stop, suggested_target)
               VALUES ($1, $2, $3, $4, '1h', 1, 2, 3, '[]'::jsonb,
                       '{"stop_type":"atr_fixed","stop_distance":1000}'::jsonb, 50000, $5, $6)""",
            candidate_id, scan_id, version_id, BTC, Decimal(stop), Decimal(target))

    await h.pool.execute(
        """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, candidate_id, mandate_id)
           VALUES ($1, $2, 'PENDING_APPROVAL', 'scanner', $3, $4)""",
        run_id, BTC, candidate_id, mandate_id)
    await h.pool.execute(
        """INSERT INTO portfolio_decisions
               (run_id, instrument_id, recommendation, conviction_level, conviction_score, allocation_pct, decision_price)
           VALUES ($1, $2, 'BUY', 'BUY', 0.9, 5, 50000)""",
        run_id, BTC)
    return run_id


def worker_for_brain(monkeypatch, card):
    state = SimpleNamespace(card=card, notes=[])

    async def get_session(_path):
        return {"session": {}, "messages": [{"role": "assistant", "widgets": [state.card]}]}

    async def note(_sid, body):
        state.notes.append(body)
        if body.get("expect_status") and state.card.get("status") != body["expect_status"]:
            return 409
        state.card.update({key: body[key] for key in ("status", "command_id") if body.get(key)})
        return 200

    monkeypatch.setattr(brain_routes, "_worker_get", get_session)
    monkeypatch.setattr(brain_routes, "_worker_note", note)
    return state


async def assert_command_passes_gate(h, command_id):
    from cryptonite_orchestrator.server import SimpleQuoteCache

    row = await h.pool.fetchrow("SELECT * FROM execution_commands WHERE command_id = $1", command_id)
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    instrument = payload["instrument_id"]
    structural_stop = payload.get("proposed_stop_loss") or payload.get("stop_loss")
    assert structural_stop is not None

    quotes = SimpleQuoteCache()
    quotes.set_quote(BTC, 49999, 50001)
    quotes.set_quote(PERP, 59999, 60001)
    quote = quotes.quote(instrument)
    dispatcher, _ = make_fresh_dispatcher(Decimal("100000"))
    engine = make_engine(dispatcher)
    intent = OrderIntent(
        client_order_id=str(command_id), instance_id=row["target_instance_id"], instrument_id=instrument,
        side=payload["side"], order_type=payload["order_type"], quantity=Decimal("0.001"),
        price=Decimal(str(payload["price"])) if payload.get("price") is not None else None,
        stop_loss=Decimal(str(payload.get("stop_loss") or structural_stop)), require_stop=True,
        leverage=payload.get("leverage"), margin_mode=payload.get("margin_mode"),
    )
    async with h.pool.acquire() as conn:
        mandate = await load_mandate(conn, row["mandate_id"])
        reason = await gate_ai_order(conn, engine, row, payload, intent, mandate, quote)
    assert reason is None, reason


async def test_debate_approval_queues_structural_stop_and_passes_mandate_gate(h, monkeypatch):
    mandate = await spot_mandate(h)
    await h.pool.execute("""INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('BTC', $1)
                            ON CONFLICT (base_asset) DO UPDATE SET mandate_id = EXCLUDED.mandate_id""", mandate.id)
    await fresh_spot_quote(h)
    run_id = await insert_debate_candidate(h, mandate.id)
    monkeypatch.setattr(agents_routes, "_fetch_decision_price", AsyncMock(return_value=50000.0))

    async with h.client() as client:
        response = await client.post(f"/api/v1/agents/debates/{run_id}/approve", headers=h.headers)

    assert response.status_code == 200, response.text
    command_id = await h.pool.fetchval("SELECT command_id FROM execution_commands ORDER BY created_at DESC LIMIT 1")
    row = await h.pool.fetchrow("SELECT payload FROM execution_commands WHERE command_id = $1", command_id)
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    assert Decimal(payload["proposed_stop_loss"]) == Decimal("49000")
    assert "stop_price" not in payload
    await assert_command_passes_gate(h, command_id)


async def test_brain_percentage_stop_rounds_a_long_safely_and_passes_gate(h, monkeypatch):
    await spot_mandate(h)
    await fresh_spot_quote(h)
    await h.redis.set("exit_monitor:heartbeat", json.dumps({"stop_loss_pct": 5.00001, "trail_pct": 2.0,
                                                            "trail_activate_pct": 3.0}), ex=60)
    card = {"kind": "order_card", "id": "card-long", "instrument_id": BTC, "side": "BUY",
            "order_type": "MARKET", "limit_price": None, "allocation_pct": 5, "stop_loss": None,
            "take_profit": None, "confidence": 0.9, "status": "PENDING_APPROVAL", "trace_id": str(uuid.uuid4())}
    worker = worker_for_brain(monkeypatch, card)

    async with h.client() as client:
        response = await client.post(f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/card-long/approve",
                                     headers=h.headers)

    assert response.status_code == 200, response.text
    row = await h.pool.fetchrow("SELECT command_id, payload FROM execution_commands")
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    assert Decimal(payload["stop_loss"]) == Decimal("47499.99")
    assert Decimal(payload["proposed_stop_loss"]) == Decimal("47499.99")
    assert worker.card["status"] == "APPROVED"
    await assert_command_passes_gate(h, row["command_id"])


async def test_brain_percentage_stop_rounds_a_short_safely(h, monkeypatch):
    await perp_mandate(h)
    await h.redis.set("perp:mark:BTCUSDT", json.dumps({"mark_price": "60000", "funding_rate": "0.0001"}), ex=60)
    await h.redis.set("exit_monitor:heartbeat", json.dumps({"stop_loss_pct": 5.00001, "trail_pct": 2.0,
                                                            "trail_activate_pct": 3.0}), ex=60)
    card = {"kind": "order_card", "id": "card-short", "instrument_id": PERP, "side": "SELL",
            "order_type": "MARKET", "limit_price": None, "allocation_pct": 2.5, "stop_loss": None,
            "take_profit": 54000.0, "confidence": 0.9, "setup_grade": "A", "status": "PENDING_APPROVAL",
            "trace_id": str(uuid.uuid4())}
    worker = worker_for_brain(monkeypatch, card)

    async with h.client() as client:
        response = await client.post(f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/card-short/approve",
                                     json={"totp_code": h.totp.now()}, headers=h.headers)

    assert response.status_code == 200, response.text
    row = await h.pool.fetchrow("SELECT command_id, payload FROM execution_commands")
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    assert Decimal(payload["stop_loss"]) == Decimal("63000.01")
    assert Decimal(payload["proposed_stop_loss"]) == Decimal("63000.01")
    assert worker.card["status"] == "APPROVED"


async def test_brain_limit_stop_is_checked_against_submitted_limit_before_claim(h, monkeypatch):
    await spot_mandate(h)
    await fresh_spot_quote(h)
    await h.redis.set("exit_monitor:heartbeat", json.dumps({"stop_loss_pct": 5.0}), ex=60)

    invalid = {"kind": "order_card", "id": "card-limit-invalid", "instrument_id": BTC, "side": "BUY",
               "order_type": "LIMIT", "limit_price": 45000, "allocation_pct": 5, "stop_loss": 49000,
               "take_profit": None, "status": "PENDING_APPROVAL", "trace_id": str(uuid.uuid4())}
    invalid_worker = worker_for_brain(monkeypatch, invalid)
    async with h.client() as client:
        refused = await client.post(
            f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/card-limit-invalid/approve", headers=h.headers)
    assert refused.status_code == 409 and refused.json()["detail"].startswith("STOP_REQUIRED:")
    assert invalid_worker.card["status"] == "PENDING_APPROVAL"
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0

    valid = {**invalid, "id": "card-limit-valid", "limit_price": 49500, "stop_loss": 47000}
    valid_worker = worker_for_brain(monkeypatch, valid)
    async with h.client() as client:
        approved = await client.post(
            f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/card-limit-valid/approve", headers=h.headers)
    assert approved.status_code == 200, approved.text
    assert valid_worker.card["status"] == "APPROVED"
    command_id = await h.pool.fetchval("SELECT command_id FROM execution_commands")
    await assert_command_passes_gate(h, command_id)


async def test_missing_or_stale_brain_stop_refuses_before_claiming_the_card(h, monkeypatch):
    await spot_mandate(h)
    await h.redis.set("exit_monitor:heartbeat", json.dumps({"stop_loss_pct": 5.0}), ex=60)
    card = {"kind": "order_card", "id": "card-stale", "instrument_id": BTC, "side": "BUY",
            "order_type": "MARKET", "limit_price": None, "allocation_pct": 5, "stop_loss": None,
            "take_profit": None, "status": "PENDING_APPROVAL", "trace_id": str(uuid.uuid4())}
    worker = worker_for_brain(monkeypatch, card)
    await h.redis.delete(quote_tick_key(BTC))

    async with h.client() as client:
        stale = await client.post(f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/card-stale/approve",
                                  headers=h.headers)
    assert stale.status_code == 409 and stale.json()["detail"].startswith("DATA_STALE:")
    assert worker.card["status"] == "PENDING_APPROVAL"
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0

    await h.redis.set("exit_monitor:heartbeat", json.dumps({"trail_pct": 2.0}), ex=60)
    missing_card = {**card, "id": "card-no-stop"}
    missing_worker = worker_for_brain(monkeypatch, missing_card)
    async with h.client() as client:
        missing = await client.post(f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/card-no-stop/approve",
                                    headers=h.headers)
    assert missing.status_code == 409 and "no stop available, cannot approve" in missing.json()["detail"]
    assert missing_worker.card["status"] == "PENDING_APPROVAL"


async def test_debate_without_candidate_stop_refuses_but_spot_exit_is_unchanged(h, monkeypatch):
    mandate = await spot_mandate(h)
    await fresh_spot_quote(h)
    missing_run = await insert_debate_candidate(h, mandate.id, with_candidate=False)
    monkeypatch.setattr(agents_routes, "_fetch_decision_price", AsyncMock(return_value=50000.0))
    async with h.client() as client:
        sizing = await client.get(f"/api/v1/agents/debates/{missing_run}/sizing")
        refused = await client.post(f"/api/v1/agents/debates/{missing_run}/approve", headers=h.headers)
    assert sizing.status_code == 200 and sizing.json()["ok"] is False
    assert sizing.json()["code"] == "NO_STOP_AVAILABLE"
    assert refused.status_code == 409 and "no stop available, cannot approve" in refused.json()["detail"]
    assert await h.pool.fetchval("SELECT status FROM debate_runs WHERE id = $1", missing_run) == "PENDING_APPROVAL"
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0

    stale_run = await insert_debate_candidate(h, mandate.id)
    await h.redis.delete(quote_tick_key(BTC))
    async with h.client() as client:
        stale_sizing = await client.get(f"/api/v1/agents/debates/{stale_run}/sizing")
        stale_approval = await client.post(f"/api/v1/agents/debates/{stale_run}/approve", headers=h.headers)
    assert stale_sizing.status_code == 200 and stale_sizing.json()["ok"] is False
    assert stale_sizing.json()["code"] == "DATA_STALE"
    assert stale_approval.status_code == 409 and stale_approval.json()["detail"].startswith("DATA_STALE:")
    assert await h.pool.fetchval("SELECT status FROM debate_runs WHERE id = $1", stale_run) == "PENDING_APPROVAL"
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
    await fresh_spot_quote(h)

    # A spot SELL closes a seeded holding and does not need an entry stop.
    sell_run = uuid.uuid4()
    await h.pool.execute("""INSERT INTO debate_runs (id, instrument_id, status, trigger_source, mandate_id)
                            VALUES ($1, $2, 'PENDING_APPROVAL', 'operator', $3)""", sell_run, BTC, mandate.id)
    await h.pool.execute("""INSERT INTO portfolio_decisions
                            (run_id, instrument_id, recommendation, conviction_level, conviction_score, allocation_pct, decision_price)
                            VALUES ($1, $2, 'SELL', 'SELL', 0.9, 5, 50000)""", sell_run, BTC)
    await h.pool.execute("""INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side,
                             order_type, quantity, filled_qty, avg_fill_price, status)
                            VALUES ($1, $2, 'PAPER-ACCOUNT-001', $3, 'BUY', 'MARKET', 0.01, 0.01, 50000, 'FILLED')""",
                         f"h1-hold-{uuid.uuid4().hex[:8]}", mandate.instance_id, BTC)
    async with h.client() as client:
        approved = await client.post(f"/api/v1/agents/debates/{sell_run}/approve", headers=h.headers)
    assert approved.status_code == 200, approved.text
    command = await h.pool.fetchrow("SELECT payload, command_type FROM execution_commands ORDER BY created_at DESC LIMIT 1")
    payload = command["payload"] if isinstance(command["payload"], dict) else json.loads(command["payload"])
    assert command["command_type"] == "SUBMIT_ORDER" and payload["side"] == "SELL"
    assert "stop_loss" not in payload and "proposed_stop_loss" not in payload


async def test_debate_sizing_and_approval_use_worker_pending_stop(h, monkeypatch):
    mandate = await spot_mandate(h)
    await fresh_spot_quote(h)
    run_id = await insert_debate_candidate(h, mandate.id, with_candidate=False)
    async with h.pool.acquire() as conn:
        await create_approval(
            conn, mandate, ActionClass.ADD_RISK, "SUBMIT_ORDER",
            {"instrument_id": BTC, "side": "BUY", "order_type": "LIMIT", "price": 50000,
             "proposed_stop_loss": "49000", "allocation_pct": 5},
            summary="worker debate entry", source="debate", source_ref=str(run_id), notify=False)
    monkeypatch.setattr(agents_routes, "_fetch_decision_price", AsyncMock(return_value=50000.0))

    async with h.client() as client:
        sizing = await client.get(f"/api/v1/agents/debates/{run_id}/sizing")
        approved = await client.post(f"/api/v1/agents/debates/{run_id}/approve", headers=h.headers)
    assert sizing.status_code == 200 and sizing.json()["ok"] is True, sizing.text
    assert approved.status_code == 200, approved.text
    row = await h.pool.fetchrow("SELECT payload, command_id FROM execution_commands")
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    assert payload["proposed_stop_loss"] == "49000"
    assert row["command_id"] is not None
