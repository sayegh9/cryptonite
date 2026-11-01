"""H4 (master plan 2026-11-01, Stage 1, item 9): one synthetic setup per side through the whole paper trade path.

Spot long: gate -> debate route -> operator approval -> command -> mandate gate -> dispatcher -> paper fill -> the exit
monitor's stop. Perp short: gate -> Brain card -> approval (TOTP) -> command -> mandate gate -> dispatcher -> paper
fill -> resting stop and the exit monitor's stop. Quotes and marks are faked; no model is called (no gateway).
"""

import json
import time
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_api.routes import agents as agents_routes
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_execution.risk import OrderIntent
from cryptonite_orchestrator.mandate_gate import gate_ai_order
from cryptonite_orchestrator.server import SimpleQuoteCache
from cryptonite_scanner_service import exit_monitor as em
from cryptonite_schemas.marks import quote_tick_key
from migrated_db import migrated_database
from test_debate_gate import Redis as GateRedis
from test_debate_gate import load
from test_exit_monitor import Redis as MonitorRedis
from test_exit_monitor import book
from test_h1_approval_stops import (
    fresh_spot_quote,
    insert_debate_candidate,
    perp_mandate,
    spot_mandate,
    worker_for_brain,
)
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_fresh_dispatcher, make_perp_dispatcher

BTC = "BTCUSDT.BINANCE"
PERP = "BTCUSDT-PERP.BINANCE"
HEARTBEAT = json.dumps({"stop_loss_pct": 5.0, "trail_pct": 2.0, "trail_activate_pct": 3.0})


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_h4_e2e") as url:
        yield url


@pytest.fixture(scope="module")
def gate():
    return load("debate_worker_gate_h4_e2e", "gate.py")


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("DELETE FROM ai_profile_coin_locks")
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
    await harness.redis.set("exit_monitor:heartbeat", HEARTBEAT, ex=60)
    try:
        yield harness
    finally:
        await harness.redis.delete(quote_tick_key(BTC), "perp:mark:BTCUSDT")
        await close_harness(harness)


def brief(direction, trend, entry, stop, target):
    return {"direction": direction,
            "candidate": {"timeframe": "1h", "entry": entry, "stop": stop, "target": target,
                          "reward_to_risk": round(abs(target - entry) / abs(entry - stop), 2)},
            "timeframes": [{"timeframe": "4h", "trend": trend}, {"timeframe": "1h", "trend": trend, "volume_ratio": 1.2,
                                                                 "bands": "above"}]}


async def gate_decision(gate, h, redis, data):
    payload = {"instrument_id": BTC, "trigger_source": "scanner", "brief_key": "",
               "candidate_payload": json.dumps({"timeframe": "1h", "bar_close_inclusive_ns": time.time_ns() - 60e9})}
    return await gate.pre_debate_gate(h.pool, redis, payload, None, "MARKET BRIEF", brief_data=data)


async def queued_command(h):
    row = await h.pool.fetchrow("SELECT * FROM execution_commands ORDER BY created_at DESC LIMIT 1")
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    return row, payload


async def gate_and_fill(h, row, payload, dispatcher, quantity: Decimal, fill_price: Decimal) -> OrderIntent:
    """The orchestrator's mandate gate, then the dispatcher, then the paper fill booked as the bridge books it."""
    quotes = SimpleQuoteCache()
    quotes.set_quote(BTC, 49999, 50001)
    quotes.set_quote(PERP, 49999, 50001)
    instrument = payload["instrument_id"]
    stop = Decimal(str(payload.get("stop_loss") or payload["proposed_stop_loss"]))
    intent = OrderIntent(
        client_order_id=str(row["command_id"]), instance_id=row["target_instance_id"], instrument_id=instrument,
        side=payload["side"], order_type=payload["order_type"], quantity=quantity, price=None, stop_loss=stop,
        require_stop=True, leverage=payload.get("leverage"), margin_mode=payload.get("margin_mode"))
    async with h.pool.acquire() as conn:
        mandate = await load_mandate(conn, row["mandate_id"])
        assert await gate_ai_order(conn, make_engine(dispatcher), row, payload, intent, mandate,
                                   quotes.quote(instrument)) is None
    assert dispatcher.dispatch(intent, lambda _order: None, None) == (True, "SUBMITTED")
    dispatcher.handle_fill(intent.client_order_id, quantity, fill_price, Decimal("0.5"), payload["side"], instrument)
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
                                        quantity, current_price, status)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, $4, $5, $6, $5, 'OPEN')""",
        f"P-{uuid.uuid4().hex[:8]}", row["target_instance_id"], instrument,
        "LONG" if payload["side"] == "BUY" else "SHORT", fill_price, quantity)
    return intent


async def assert_exit_monitor_stops(h, instance_id, instrument, price):
    redis = MonitorRedis()
    await em.monitor_once(h.pool, redis, book(50000.0, instrument))  # at the entry: nothing to do
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands WHERE command_type = 'CLOSE_POSITION'") == 0
    await em.monitor_once(h.pool, redis, book(price, instrument))
    close = await h.pool.fetchrow(
        "SELECT target_instance_id, payload FROM execution_commands WHERE command_type = 'CLOSE_POSITION'")
    assert close is not None and close["target_instance_id"] == instance_id
    assert json.loads(close["payload"])["reason"] == "HARD_STOP"


async def test_a_spot_long_goes_from_the_gate_to_a_protected_paper_fill(gate, h, monkeypatch):
    decision = await gate_decision(gate, h, GateRedis(), brief("LONG", "up", 50000, 49000, 52000))
    assert (decision.allowed, decision.reason) == (True, "NO_TRIAGE_GATEWAY")

    mandate = await spot_mandate(h)
    await h.pool.execute("""INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('BTC', $1)
                            ON CONFLICT (base_asset) DO UPDATE SET mandate_id = EXCLUDED.mandate_id""", mandate.id)
    await fresh_spot_quote(h)
    run_id = await insert_debate_candidate(h, mandate.id)  # entry 50,000, stop 49,000, target 52,000
    monkeypatch.setattr(agents_routes, "_fetch_decision_price", AsyncMock(return_value=50000.0))
    async with h.client() as client:
        response = await client.post(f"/api/v1/agents/debates/{run_id}/approve", headers=h.headers)
    assert response.status_code == 200, response.text

    row, payload = await queued_command(h)
    assert (payload["instrument_id"], payload["side"]) == (BTC, "BUY")
    assert Decimal(payload["proposed_stop_loss"]) == Decimal("49000")
    dispatcher, _ = make_fresh_dispatcher(Decimal("10000"))
    await gate_and_fill(h, row, payload, dispatcher, Decimal("0.01"), Decimal("50001"))
    assert dispatcher.positions[BTC] == Decimal("0.01")

    await assert_exit_monitor_stops(h, row["target_instance_id"], BTC, 47000.0)


async def test_a_perp_short_goes_from_the_gate_to_a_protected_paper_fill(gate, h, monkeypatch):
    redis = GateRedis()
    # The opposing short is refused by the free rule and takes no cooldown; the aligned one then passes (H4 1, 5)
    opposing = await gate_decision(gate, h, redis, brief("SHORT", "up", 50000, 51000, 48000))
    assert (opposing.allowed, opposing.reason) == (False, "TRIAGE_RULE_TIMEFRAMES_AGAINST")
    aligned = await gate_decision(gate, h, redis, brief("SHORT", "down", 50000, 51000, 48000))
    assert (aligned.allowed, aligned.reason) == (True, "NO_TRIAGE_GATEWAY")

    await perp_mandate(h)  # PERP, direction BOTH
    await h.redis.set("perp:mark:BTCUSDT", json.dumps({"mark_price": "50000", "funding_rate": "0.0001"}), ex=60)
    card = {"kind": "order_card", "id": "card-h4-short", "instrument_id": PERP, "side": "SELL",
            "order_type": "MARKET", "limit_price": None, "allocation_pct": 5, "stop_loss": 51000.0,
            "take_profit": 48000.0, "confidence": 0.9, "setup_grade": "B", "status": "PENDING_APPROVAL",
            "trace_id": str(uuid.uuid4())}
    worker = worker_for_brain(monkeypatch, card)
    async with h.client() as client:
        response = await client.post(f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/card-h4-short/approve",
                                     json={"totp_code": h.totp.now()}, headers=h.headers)
    assert response.status_code == 200, response.text
    assert worker.card["status"] == "APPROVED"

    row, payload = await queued_command(h)
    assert (payload["instrument_id"], payload["side"]) == (PERP, "SELL")
    stop = Decimal(str(payload["stop_loss"]))
    assert stop > Decimal("50000")
    dispatcher = make_perp_dispatcher("50000")
    intent = await gate_and_fill(h, row, payload, dispatcher, Decimal("0.04"), Decimal("49999"))
    ok, reason = dispatcher.place_resting_stop(f"{intent.client_order_id}-SL", PERP, stop,
                                               instance_id=row["target_instance_id"])
    assert ok, reason
    fills, _ = dispatcher.triggered_stops({PERP: stop + Decimal("500")})
    assert len(fills) == 1  # the resting stop fires on the mark

    await assert_exit_monitor_stops(h, row["target_instance_id"], PERP, 53500.0)


async def test_a_short_card_in_a_long_only_mandate_is_refused_before_it_is_claimed(h, monkeypatch):
    # H4 item 2: the card preflight never checked the mandate's direction; only the orchestrator refused it, after
    # the card was claimed
    await h.pool.execute(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, autonomy_level, status,
                                    allow_perps, markets, max_leverage, direction)
           VALUES ('h4-long-only', 10000, ARRAY[$1], 25, 1, 'ACTIVE', true, ARRAY['PERP'], 10, 'LONG')""", PERP)
    await h.redis.set("perp:mark:BTCUSDT", json.dumps({"mark_price": "50000", "funding_rate": "0.0001"}), ex=60)
    card = {"kind": "order_card", "id": "card-h4-dir", "instrument_id": PERP, "side": "SELL", "order_type": "MARKET",
            "limit_price": None, "allocation_pct": 5, "stop_loss": 51000.0, "take_profit": 48000.0, "confidence": 0.9,
            "setup_grade": "B", "status": "PENDING_APPROVAL", "trace_id": str(uuid.uuid4())}
    worker = worker_for_brain(monkeypatch, card)
    async with h.client() as client:
        response = await client.post(f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/card-h4-dir/approve",
                                     json={"totp_code": h.totp.now()}, headers=h.headers)
    assert response.status_code == 409, response.text
    assert response.json()["detail"].startswith("MANDATE_DIRECTION_NOT_ALLOWED: mandate h4-long-only (LONG, markets PERP)")
    assert worker.card["status"] == "PENDING_APPROVAL"
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
