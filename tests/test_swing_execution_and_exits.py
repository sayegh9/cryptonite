"""Tests for WS-S2: Per-Position Structural Stops, TP1 Scale-Out & Partial Sell Cards.

Covers:
1. evaluate_exit triggers STRUCTURAL_STOP when pos.stop_price is breached before -5%,
   and ignores invalid stop_price (e.g. above entry on a LONG).
2. evaluate_exit triggers REDUCE_TP1 when pos.take_profit_price is reached and tp1_done=False,
   and does not re-trigger when tp1_done=True.
3. monitor_once against the migrated test DB enqueues a 50% SUBMIT_ORDER SELL when
   take_profit_price is reached (from either execution_commands.payload proposed_take_profit
   or Redis exit:plan:{instrument}:{owner}), and sets exit:tp1:{pos.key}.
4. Brain OrderCardWidget / approve_order_card with side="SELL" and reduce_pct=50 queues an order
   for 50% of the mandate's holding and updates the summary accordingly, while reduce_pct=None
   still sells 100%.
"""

import json
import uuid
from decimal import Decimal

import asyncpg
import httpx
import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_agents.brain.widgets import OrderProposal, describe_widget, widget_for_tool
from cryptonite_scanner_service import exit_monitor as em
from cryptonite_scanner_service.exit_monitor import (
    ExitAction,
    Position,
    QuoteBook,
    evaluate_exit,
)
from migrated_db import migrated_database

BTC = "BTCUSDT.BINANCE"
ETH = "ETHUSDT.BINANCE"
APPROVE = "/api/v1/agents/brain/sessions/{sid}/cards/call-1/approve"
PREFLIGHT = "/api/v1/agents/brain/sessions/{sid}/cards/call-1/preflight"


def pos(
    current: float,
    side: str = "LONG",
    entry: float = 100.0,
    ai: bool = True,
    stop_price: float | None = None,
    take_profit_price: float | None = None,
    quantity: float = 1.0,
    **kw,
) -> Position:
    return Position(
        BTC,
        side,
        entry,
        current,
        ai,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        quantity=quantity,
        **kw,
    )


# --- 1. Structural Stop Tests ---

def test_structural_stop_triggered_before_hard_stop():
    # Long position: entry 100, stop_price 98 (loss of 2%, well before -5% hard stop)
    p = pos(98.0, side="LONG", entry=100.0, stop_price=98.0)
    actions = evaluate_exit(p, best_price=100.0, debate_armed=True)
    assert actions == [ExitAction("CLOSE", "STRUCTURAL_STOP")]

    # Below structural stop: still triggers STRUCTURAL_STOP
    p_below = pos(97.5, side="LONG", entry=100.0, stop_price=98.0)
    assert evaluate_exit(p_below, best_price=100.0, debate_armed=True) == [
        ExitAction("CLOSE", "STRUCTURAL_STOP")
    ]

    # Non-AI owned: alerts instead of closing
    p_shared = pos(98.0, side="LONG", entry=100.0, ai=False, stop_price=98.0)
    assert evaluate_exit(p_shared, best_price=100.0, debate_armed=True) == [
        ExitAction("ALERT", "STRUCTURAL_STOP")
    ]

    # Short position: entry 100, stop_price 102 (loss of 2%)
    p_short = pos(102.0, side="SHORT", entry=100.0, stop_price=102.0)
    assert evaluate_exit(p_short, best_price=100.0, debate_armed=True) == [
        ExitAction("CLOSE", "STRUCTURAL_STOP")
    ]


def test_hard_stop_takes_precedence_over_structural_stop_at_or_below_minus_5():
    # Long position: loss >= 5% (-5%) triggers HARD_STOP
    p = pos(95.0, side="LONG", entry=100.0, stop_price=98.0)
    assert evaluate_exit(p, best_price=100.0, debate_armed=True) == [
        ExitAction("CLOSE", "HARD_STOP")
    ]


def test_invalid_structural_stop_is_ignored():
    # Long position with stop_price above entry (invalid for LONG stop loss)
    p_invalid_long = pos(99.0, side="LONG", entry=100.0, stop_price=105.0)
    assert evaluate_exit(p_invalid_long, best_price=100.0, debate_armed=True) == []

    # Short position with stop_price below entry (invalid for SHORT stop loss)
    p_invalid_short = pos(101.0, side="SHORT", entry=100.0, stop_price=95.0)
    assert evaluate_exit(p_invalid_short, best_price=100.0, debate_armed=True) == []

    # Negative / zero stop price
    p_zero = pos(99.0, side="LONG", entry=100.0, stop_price=0.0)
    assert evaluate_exit(p_zero, best_price=100.0, debate_armed=True) == []


# --- 2. TP1 Scale-Out Tests ---

def test_tp1_scale_out_triggered_when_target_hit():
    # Long position: entry 100, take_profit_price 104
    p = pos(104.0, side="LONG", entry=100.0, take_profit_price=104.0)
    actions = evaluate_exit(p, best_price=104.0, debate_armed=True, tp1_done=False)
    assert actions == [ExitAction("REDUCE_TP1", "TAKE_PROFIT_1")]

    # Above target: still triggers TP1
    p_above = pos(105.0, side="LONG", entry=100.0, take_profit_price=104.0)
    assert evaluate_exit(p_above, best_price=105.0, debate_armed=True, tp1_done=False) == [
        ExitAction("REDUCE_TP1", "TAKE_PROFIT_1")
    ]

    # Non-AI owned: alerts
    p_shared = pos(104.0, side="LONG", entry=100.0, ai=False, take_profit_price=104.0)
    assert evaluate_exit(p_shared, best_price=104.0, debate_armed=True, tp1_done=False) == [
        ExitAction("ALERT", "TAKE_PROFIT_1")
    ]

    # Short position: entry 100, take_profit_price 96
    p_short = pos(96.0, side="SHORT", entry=100.0, take_profit_price=96.0)
    assert evaluate_exit(p_short, best_price=96.0, debate_armed=True, tp1_done=False) == [
        ExitAction("REDUCE_TP1", "TAKE_PROFIT_1")
    ]


def test_tp1_scale_out_does_not_retrigger_when_tp1_done():
    p = pos(104.0, side="LONG", entry=100.0, take_profit_price=104.0)
    # When tp1_done=True, no TP1 action is emitted
    actions = evaluate_exit(p, best_price=104.0, debate_armed=True, tp1_done=True)
    assert not any(a.kind == "REDUCE_TP1" for a in actions)


def test_invalid_tp1_target_is_ignored():
    # Long position with take_profit below entry
    p_invalid_long = pos(101.0, side="LONG", entry=100.0, take_profit_price=95.0)
    assert evaluate_exit(p_invalid_long, best_price=101.0, debate_armed=True, tp1_done=False) == []

    # Short position with take_profit above entry
    p_invalid_short = pos(99.0, side="SHORT", entry=100.0, take_profit_price=105.0)
    assert evaluate_exit(p_invalid_short, best_price=99.0, debate_armed=True, tp1_done=False) == []


# --- 3. Database & Monitor_Once TP1 Enqueue Tests ---

class MockRedis:
    def __init__(self):
        self.kv = {}
        self.streams = []
        self.published = []

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def exists(self, key):
        return int(key in self.kv)

    async def delete(self, key):
        self.kv.pop(key, None)

    async def xadd(self, stream, fields, **kwargs):
        self.streams.append((stream, fields))

    async def publish(self, channel, message):
        self.published.append(json.loads(message))

    async def hincrbyfloat(self, key, field, amount):
        h = self.kv.setdefault(key, {})
        h[field] = h.get(field, 0.0) + amount
        return h[field]

    async def expire(self, key, seconds):
        return True


def quote_book(price: float, instrument: str = BTC) -> QuoteBook:
    import time as _t
    b = QuoteBook()
    b.apply({"instrument_id": instrument, "bid_price": str(price), "ask_price": str(price), "arrival_ns": _t.time_ns()})
    return b


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_swing_exits") as url:
        yield url


async def seed_position(
    pool,
    instance_id: str,
    instrument: str = BTC,
    entry_price: float = 100.0,
    quantity: float = 0.2,
    side: str = "LONG",
):
    strat_id = uuid.uuid4()
    await pool.execute(
        "TRUNCATE paper_positions, paper_strategy_instances, paper_accounts, execution_commands CASCADE"
    )
    await pool.execute(
        "INSERT INTO strategies (id, slug, name, description, author) VALUES ($1, $2, $2, 't', 't')",
        strat_id,
        f"swing-{strat_id.hex[:8]}",
    )
    await pool.execute(
        """INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit,
              default_parameters, parameter_schema) VALUES ($1, $1, 1, 'h', 'c', '{}', '{}')""",
        strat_id,
    )
    await pool.execute(
        """INSERT INTO paper_accounts (account_id, venue, currency, starting_balance, cash_balance,
              total_equity, realized_pnl, daily_realized_pnl, daily_loss_latched, updated_at)
              VALUES ('PAPER-ACCOUNT-001', 'BINANCE', 'USDT', 1e5, 1e5, 1e5, 0, 0, false, now())"""
    )
    await pool.execute(
        """INSERT INTO paper_strategy_instances (instance_id, account_id, strategy_version_id,
              strategy_source_hash, risk_policy_hash, strategy_name, instrument_id, timeframe,
              resolved_parameters, allocation_usd, status)
              VALUES ($1, 'PAPER-ACCOUNT-001', $2, 'h', 'h', 'swing-inst', $3, '1h', '{}', 1000, 'RUNNING')""",
        instance_id,
        strat_id,
        instrument,
    )
    await pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side,
              entry_price, quantity, current_price, status)
              VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, $4, $5, $6, $5, 'OPEN')""",
        f"P-{uuid.uuid4().hex[:8]}",
        instance_id,
        instrument,
        side,
        entry_price,
        quantity,
    )


async def test_monitor_once_enqueues_50pct_sell_on_tp1_from_command_payload(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        inst_id = "AI_DEBATE_WORKER"
        await seed_position(pool, inst_id, instrument=BTC, entry_price=100.0, quantity=0.2)

        # Seed completed entry command carrying proposed_take_profit=105.0 and proposed_stop_loss=96.0
        cmd_id = uuid.uuid4()
        entry_payload = {
            "instrument_id": BTC,
            "side": "BUY",
            "proposed_take_profit": 105.0,
            "proposed_stop_loss": 96.0,
        }
        await pool.execute(
            """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id,
                   requester_id, payload, status, environment, account_id)
               VALUES ($1, $2, 'SUBMIT_ORDER', $3, 'debate', $4::jsonb, 'COMPLETED', 'PAPER', 'PAPER-ACCOUNT-001')""",
            cmd_id,
            f"entry:{cmd_id}",
            inst_id,
            json.dumps(entry_payload),
        )

        redis = MockRedis()
        # Price is at 100.0 (entry) -> no exit action
        await em.monitor_once(pool, redis, quote_book(100.0, BTC))
        assert await pool.fetchval(
            "SELECT count(*) FROM execution_commands WHERE requester_id = 'risk:exit_monitor'"
        ) == 0

        # Price hits 105.0 (TP1) -> enqueues 50% SUBMIT_ORDER SELL
        await em.monitor_once(pool, redis, quote_book(105.0, BTC))

        rows = await pool.fetch(
            "SELECT command_type, target_instance_id, requester_id, payload FROM execution_commands WHERE requester_id = 'risk:exit_monitor'"
        )
        assert len(rows) == 1
        cmd = rows[0]
        assert cmd["command_type"] == "SUBMIT_ORDER"
        assert cmd["target_instance_id"] == inst_id
        payload = json.loads(cmd["payload"])
        assert payload["instrument_id"] == BTC
        assert payload["side"] == "SELL"
        assert payload["order_type"] == "MARKET"
        assert Decimal(payload["quantity"]) == Decimal("0.1")  # 50% of 0.2
        assert payload["reason"] == "TAKE_PROFIT_1"
        assert payload["source"] == "exit_monitor_tp1"

        # Check exit:tp1 key set in Redis
        tp1_keys = [k for k in redis.kv if k.startswith("exit:tp1:")]
        assert len(tp1_keys) == 1
        assert redis.kv[tp1_keys[0]] == "1"

        # Subsequent monitor_once does not duplicate the scale-out
        await em.monitor_once(pool, redis, quote_book(106.0, BTC))
        assert await pool.fetchval(
            "SELECT count(*) FROM execution_commands WHERE requester_id = 'risk:exit_monitor'"
        ) == 1
    finally:
        await pool.close()


async def test_monitor_once_enqueues_tp1_from_redis_exit_plan_override(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        inst_id = "AI_DEBATE_WORKER"
        await seed_position(pool, inst_id, instrument=BTC, entry_price=100.0, quantity=0.4)

        redis = MockRedis()
        # Set Redis plan override: TP at 108.0
        await redis.set(
            f"exit:plan:{BTC}:{inst_id}",
            json.dumps({"stop_price": 95.0, "take_profit_price": 108.0}),
        )

        # Price hits 108.0 -> triggers TP1
        await em.monitor_once(pool, redis, quote_book(108.0, BTC))

        rows = await pool.fetch(
            "SELECT command_type, payload FROM execution_commands WHERE requester_id = 'risk:exit_monitor'"
        )
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload"])
        assert payload["side"] == "SELL"
        assert Decimal(payload["quantity"]) == Decimal("0.2")  # 50% of 0.4
        assert payload["reason"] == "TAKE_PROFIT_1"
    finally:
        await pool.close()


# --- 4. Brain Partial Sell Card Tests ---

def test_order_proposal_reduce_pct_and_describe_widget():
    # OrderProposal accepts reduce_pct in (25, 50, 100) or None
    prop = OrderProposal(
        instrument_id=BTC,
        side="SELL",
        allocation_pct=5.0,
        reduce_pct=50,
        rationale="Take 50% profit at TP1",
    )
    assert prop.reduce_pct == 50

    card = widget_for_tool(
        "propose_order",
        {
            "instrument_id": BTC,
            "side": "SELL",
            "allocation_pct": 5.0,
            "reduce_pct": 50,
            "rationale": "TP1 scale-out",
        },
        call_id="c-partial",
        status="DENIED",
    )
    assert card["reduce_pct"] == 50
    desc = describe_widget(card)
    assert "reduce 50% of holding" in desc

    # 100% or None retains exact legacy format without "reduce"
    card_full = widget_for_tool(
        "propose_order",
        {
            "instrument_id": BTC,
            "side": "SELL",
            "allocation_pct": 5.0,
            "reduce_pct": None,
            "rationale": "Full close",
        },
        call_id="c-full",
        status="DENIED",
    )
    desc_full = describe_widget(card_full)
    assert "reduce" not in desc_full


@pytest.fixture
async def h(db_url, monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_TOTP", "true")
    monkeypatch.setenv("AI_ALLOW_DEV_TOTP", "true")
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute(
        "TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_orders, paper_positions CASCADE"
    )
    try:
        yield harness
    finally:
        await close_harness(harness)


@pytest.fixture
def make_card_session(monkeypatch):
    from cryptonite_api.routes import brain as brain_routes

    def _setup(card_dict):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "session": {},
                        "messages": [{"role": "assistant", "widgets": [card_dict]}],
                    },
                )
            body = json.loads(request.content)
            if body.get("expect_status") and card_dict["status"] != body["expect_status"]:
                return httpx.Response(409)
            card_dict.update({k: body[k] for k in ("status", "command_id") if body.get(k)})
            return httpx.Response(200, json={"status": "ok"})

        real = httpx.AsyncClient
        monkeypatch.setattr(
            brain_routes.httpx,
            "AsyncClient",
            lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
        )
        return card_dict

    return _setup


async def hold_spot(pool, instance_id: str, instrument_id: str, qty: str):
    await pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type, quantity,
               filled_qty, avg_fill_price, status)
           VALUES ($1, $2, 'PAPER-ACCOUNT-001', $3, 'BUY', 'MARKET', $4::numeric, $4::numeric, 50000, 'FILLED')""",
        f"hold-{uuid.uuid4().hex[:8]}",
        instance_id,
        instrument_id,
        qty,
    )


async def test_approve_sell_card_with_reduce_50_sizes_order_to_half_holding(h, make_card_session):
    m = await h.mandate(instruments=(BTC,), budget=10000)
    await hold_spot(h.pool, m.instance_id, BTC, "0.01")

    card = make_card_session({
        "kind": "order_card",
        "id": "call-1",
        "instrument_id": BTC,
        "side": "SELL",
        "quantity": None,
        "order_type": "MARKET",
        "limit_price": None,
        "allocation_pct": 5.0,
        "reduce_pct": 50,
        "stop_loss": None,
        "take_profit": None,
        "status": "PENDING_APPROVAL",
        "trace_id": str(uuid.uuid4()),
        "command_id": None,
    })

    # Preflight check
    async with h.client() as c:
        preflight_res = await c.get(PREFLIGHT.format(sid=uuid.uuid4()), headers=h.headers)
    assert preflight_res.status_code == 200, preflight_res.text
    pbody = preflight_res.json()
    assert pbody["ok"] is True
    assert Decimal(pbody["sell_quantity"]) == Decimal("0.005")  # 50% of 0.01
    assert "50% (0.005)" in pbody["summary"]

    # Approve check
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert card["status"] == "APPROVED"

    cmd = await h.pool.fetchrow(
        "SELECT * FROM execution_commands WHERE command_id = $1",
        uuid.UUID(body["command_id"]),
    )
    payload = json.loads(cmd["payload"])
    assert payload["side"] == "SELL"
    assert Decimal(payload["quantity"]) == Decimal("0.005")

    # Check approval summary recorded
    appr = await h.pool.fetchrow("SELECT summary FROM ai_approvals WHERE command_id = $1", cmd["command_id"])
    assert "50% (0.005)" in appr["summary"]


async def test_approve_sell_card_without_reduce_pct_sells_100pct(h, make_card_session):
    m = await h.mandate(instruments=(BTC,), budget=10000)
    await hold_spot(h.pool, m.instance_id, BTC, "0.01")

    make_card_session({
        "kind": "order_card",
        "id": "call-1",
        "instrument_id": BTC,
        "side": "SELL",
        "quantity": None,
        "order_type": "MARKET",
        "limit_price": None,
        "allocation_pct": 5.0,
        "reduce_pct": None,
        "stop_loss": None,
        "take_profit": None,
        "status": "PENDING_APPROVAL",
        "trace_id": str(uuid.uuid4()),
        "command_id": None,
    })

    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 200, res.text
    body = res.json()
    cmd = await h.pool.fetchrow(
        "SELECT * FROM execution_commands WHERE command_id = $1",
        uuid.UUID(body["command_id"]),
    )
    payload = json.loads(cmd["payload"])
    assert Decimal(payload["quantity"]) == Decimal("0.01")  # 100% of 0.01
    appr = await h.pool.fetchrow("SELECT summary FROM ai_approvals WHERE command_id = $1", cmd["command_id"])
    assert "the whole holding of 0.01" in appr["summary"]


async def test_partial_sell_too_small_raises_409(h, make_card_session):
    m = await h.mandate(instruments=(BTC,), budget=10000)
    # MIN_HOLDING_QTY is 1e-9; 50% of 1e-9 is 0.5e-9 < MIN_HOLDING_QTY
    await hold_spot(h.pool, m.instance_id, BTC, "0.000000001")

    make_card_session({
        "kind": "order_card",
        "id": "call-1",
        "instrument_id": BTC,
        "side": "SELL",
        "quantity": None,
        "order_type": "MARKET",
        "limit_price": None,
        "allocation_pct": 5.0,
        "reduce_pct": 50,
        "status": "PENDING_APPROVAL",
        "trace_id": str(uuid.uuid4()),
        "command_id": None,
    })

    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 409
    assert res.json()["detail"].startswith("SPOT_SELL_TOO_SMALL")
