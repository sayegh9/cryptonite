"""ADR 0011 P1.5: profile execution, audit contracts and retention safety."""

import asyncio
import json
import uuid
from decimal import Decimal as D
from unittest.mock import AsyncMock, MagicMock

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_execution import profile_lifecycle
from cryptonite_execution.mandates import MandateExposure, MandateOrder, check_mandate
from cryptonite_execution.matching import PaperFill
from cryptonite_execution.risk import OrderIntent
from cryptonite_orchestrator import mandate_gate, server
from cryptonite_schemas.mandate import BREACH_CODES, MandateReject
from cryptonite_schemas.session import SessionVerdict
from migrated_db import migrated_database
from test_ai_mandates import FakeConn, mandate, mandate_row
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_perp_dispatcher
from test_perp_stop_ratchet import PaperOrdersDB, open_perp, pool_for, stop_of, tick

SPOT = "BTCUSDT.BINANCE"
PERP = "BTCUSDT-PERP.BINANCE"
MARGIN = "BTCUSDT-MARGIN.BINANCE"
FILL_KEYS = {"side", "market", "price", "qty", "leverage", "stop", "mode"}
EXIT_KEYS = {"side", "market", "price", "qty", "realized_pnl", "r_multiple"}


def decisions(conn):
    """Decode the actual profile-log INSERT arguments from a command test connection."""
    return [dict(instrument_id=a[1], stage=a[2], outcome=a[3], detail=json.loads(a[5]))
            for sql, a in conn.executed if "INSERT INTO ai_profile_decisions" in sql]


async def run_command(m, payload, *, dispatcher=None, command_type="SUBMIT_ORDER", positions=(), conn_type=FakeConn):
    """Execute one real poller claim with a real dispatcher and an isolated SQL double."""
    d = dispatcher or make_perp_dispatcher()
    engine = make_engine(d)
    engine.bridge = MagicMock()
    quotes = server.SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000, 50010)
    quotes.set_quote(PERP, 50000, 50010)
    cid = uuid.uuid4()
    approval_id = uuid.uuid4()
    payload = {"mandate_id": str(m.id), "approval_id": str(approval_id), **payload}
    row = {"command_id": cid, "command_type": command_type, "target_instance_id": m.instance_id,
           "requester_id": "operator:test", "mandate_id": m.id, "payload": json.dumps(payload)}
    approval = {"mandate_id": m.id, "action_class": "ADD_RISK", "status": "APPROVED", "command_id": cid}
    conn = conn_type(row, mandate_row(m), positions, approval)
    pool = pool_for(conn)
    d.dispatch = MagicMock(wraps=d.dispatch)
    profile_lifecycle.reset_switch_cache()
    with pytest.raises(asyncio.CancelledError):
        await server.command_poller(pool, engine, quotes)
    profile_lifecycle.reset_switch_cache()
    return conn, engine


@pytest.mark.parametrize("instrument", [SPOT, PERP])
async def test_dry_run_records_the_sizing_plan_without_dispatch(instrument):
    m = mandate(dry_run=True, instruments=[SPOT, PERP], markets=["SPOT", "PERP"], max_leverage=3)
    conn, engine = await run_command(m, {"instrument_id": instrument, "side": "BUY", "allocation_pct": 5,
                                          "stop_loss": "49750", "leverage": 2, "setup_grade": "A"})
    engine.dispatcher.dispatch.assert_not_called()
    assert conn.outcome() == ("COMPLETED", None)
    assert engine.dispatcher.open_orders == {}
    row, = [r for r in decisions(conn) if r["stage"] == "ORDER"]
    assert row["outcome"] == "DRY_RUN" and row["instrument_id"] == instrument
    plan = row["detail"]["plan"]
    assert D(plan["quantity"]) > 0 and D(plan["entry_price"]) > 0
    if instrument == PERP:
        assert plan["leverage"] >= 1 and "ceilings" in plan
    saved = [a for sql, a in conn.executed if "dry_run_plan" in sql]
    assert saved and json.loads(saved[0][1])["quantity"] == plan["quantity"]


@pytest.mark.parametrize("instrument", [SPOT, PERP])
async def test_dry_run_reductions_still_dispatch(instrument):
    m = mandate(dry_run=True, instruments=[SPOT, PERP], markets=["SPOT", "PERP"], max_leverage=3)
    d = make_perp_dispatcher()
    holdings = []
    if instrument == PERP:
        d.set_perp_position(PERP, D("0.02"), D("50000"), 2, "ISOLATED", m.instance_id)
    else:
        d.positions[SPOT] = D("0.02")
        holdings = [{"instance_id": m.instance_id, "instrument_id": SPOT,
                     "quantity": D("0.02"), "current_price": D("50000")}]
    conn, engine = await run_command(m, {"instrument_id": instrument, "side": "SELL", "quantity": "0.01",
                                          "reduce_only": instrument == PERP}, dispatcher=d, positions=holdings)
    engine.dispatcher.dispatch.assert_called_once()
    assert conn.outcome() == ("COMPLETED", None)
    assert not [r for r in decisions(conn) if r["outcome"] == "DRY_RUN"]


async def test_dry_run_enabled_between_snapshot_and_gate_never_dispatches():
    m = mandate(dry_run=False)

    class DryRunRaceConn(FakeConn):
        def __init__(self, *args):
            super().__init__(*args)
            self.mandate_reads = 0

        async def fetchrow(self, sql, *args):
            if "FROM ai_mandates" in sql:
                self.mandate_reads += 1
                fresh = m.model_copy(update={"dry_run": self.mandate_reads > 1})
                return mandate_row(fresh)
            return await super().fetchrow(sql, *args)

    conn, engine = await run_command(
        m, {"instrument_id": SPOT, "side": "BUY", "allocation_pct": 5, "stop_loss": "49750"},
        conn_type=DryRunRaceConn,
    )
    engine.dispatcher.dispatch.assert_not_called()
    assert conn.mandate_reads >= 2
    assert conn.outcome() == ("COMPLETED", None)
    row, = [r for r in decisions(conn) if r["stage"] == "ORDER"]
    assert row["outcome"] == "DRY_RUN" and D(row["detail"]["plan"]["quantity"]) > 0


@pytest.mark.parametrize("reason,outcome", [("TAKE_PROFIT_1", "TP1"), ("TRAILING_STOP", "TRAIL"),
                                            ("MAX_HOLD", "MAX_HOLD"), ("MANUAL", "MANUAL")])
async def test_dry_run_close_runs_and_logs_the_reason_with_unknown_entry_risk(reason, outcome):
    m = mandate(dry_run=True, status="HALTED")
    d = make_perp_dispatcher()
    d.positions[SPOT] = D("0.02")
    holdings = [{"instance_id": m.instance_id, "instrument_id": SPOT,
                 "quantity": D("0.02"), "current_price": D("49000")}]
    conn, engine = await run_command(m, {"instrument_id": SPOT, "reason": reason}, dispatcher=d,
                                    command_type="CLOSE_POSITION", positions=holdings)
    assert engine.dispatcher.get_position_qty(SPOT) == 0 and conn.outcome() == ("COMPLETED", None)
    row, = [r for r in decisions(conn) if r["stage"] == "EXIT"]
    assert row["outcome"] == outcome and row["instrument_id"] == SPOT and set(row["detail"]) == EXIT_KEYS
    assert row["detail"]["r_multiple"] is None and D(row["detail"]["realized_pnl"]) > 0


@pytest.mark.parametrize("instrument,markets,side,direction,code", [
    (SPOT, ["PERP"], "BUY", "LONG", "MANDATE_MARKET_NOT_ALLOWED"),
    (MARGIN, ["SPOT_MARGIN"], "BUY", "LONG", "MANDATE_MARKET_NOT_ALLOWED"),
    (PERP, ["PERP"], "SELL", "LONG", "MANDATE_DIRECTION_NOT_ALLOWED"),
    (PERP, ["PERP"], "BUY", "SHORT", "MANDATE_DIRECTION_NOT_ALLOWED"),
])
def test_market_and_direction_refusals_are_not_breaches(instrument, markets, side, direction, code):
    m = mandate(instruments=[instrument], markets=markets, direction=direction)
    order = MandateOrder(instrument, side, D("0.01"), D("50000"), order_type="LIMIT")
    reject = check_mandate(m, order, MandateExposure(), approval_ok=True, autotrade_on=True)
    assert reject == code
    assert reject not in BREACH_CODES


def test_perp_opt_in_refusal_keeps_its_existing_breach_contract():
    m = mandate(instruments=[PERP], markets=["SPOT"])
    order = MandateOrder(PERP, "BUY", D("0.01"), D("50000"))
    reject = check_mandate(m, order, MandateExposure(), approval_ok=True, autotrade_on=True)
    assert reject is MandateReject.PERPS_NOT_ALLOWED and reject in BREACH_CODES


def test_short_perp_entry_is_refused_for_a_long_only_profile_without_a_breach():
    m = mandate(instruments=[PERP], markets=["PERP"], direction="LONG", allow_perps=True, max_leverage=3)
    order = MandateOrder(PERP, "SELL", D("0.01"), D("50000"), order_type="LIMIT", leverage=2)
    reject = check_mandate(m, order, MandateExposure(), approval_ok=True, autotrade_on=True)
    assert reject is MandateReject.DIRECTION_NOT_ALLOWED
    assert reject not in BREACH_CODES


@pytest.mark.parametrize("instrument,side,owned_side", [(SPOT, "SELL", "LONG"), (PERP, "SELL", "LONG"),
                                                         (PERP, "BUY", "SHORT"), (MARGIN, "SELL", "LONG")])
def test_market_and_direction_guards_never_refuse_an_owned_reduction(instrument, side, owned_side):
    m = mandate(instruments=[instrument], markets=["PERP"], direction="SHORT", status="HALTED", dry_run=True)
    order = MandateOrder(instrument, side, D("0.01"), D("50000"), reduce_only=True)
    held = MandateExposure(instrument_qty=D("0.01"), instrument_side=owned_side)
    assert check_mandate(m, order, held, approval_ok=False, autotrade_on=False) is None


async def test_cash_guard_is_per_profile_with_durable_and_working_reservations():
    first = mandate(capital_budget_usd=D("1000"))
    second = mandate(id=uuid.uuid4(), capital_budget_usd=D("1000"))
    d = make_perp_dispatcher()
    engine = make_engine(d)
    # The first profile already reserved $900; the second's cash remains its own.
    conn = AsyncMock()
    conn.fetch.return_value = []
    conn.fetchval.side_effect = lambda sql, *a: D("900") if a[0] == first.id else D("0")
    order = MandateOrder(SPOT, "BUY", D("0.004"), D("50000"), order_type="LIMIT")
    assert await mandate_gate.profile_cash_reject(conn, engine, first, order) == "PROFILE_CASH_EXHAUSTED"
    assert await mandate_gate.profile_cash_reject(conn, engine, second, order) is None
    assert "PROFILE_CASH_EXHAUSTED" not in BREACH_CODES


async def test_cash_guard_counts_spot_cost_and_perp_margin_without_double_counting():
    m = mandate(capital_budget_usd=D("1000.10"), instruments=[SPOT, PERP], markets=["SPOT", "PERP"])
    d = make_perp_dispatcher()
    d.set_perp_position(PERP, D("0.02"), D("50000"), 2, "ISOLATED", m.instance_id)
    engine = make_engine(d)
    conn = AsyncMock()
    conn.fetch.return_value = [{"instrument_id": SPOT, "side": "BUY", "filled_qty": D("0.008"),
                               "fill_price": D("50000")}]
    conn.fetchval.return_value = D("0")
    order = MandateOrder(PERP, "BUY", D("0.004"), D("50000"), order_type="LIMIT", leverage=2)
    # $400 spot + $500 margin + $100 new margin + $0.10 fee = the $1,000.10 budget.
    assert await mandate_gate.profile_cash_reject(conn, engine, m, order) is None
    larger = MandateOrder(PERP, "BUY", D("0.0041"), D("50000"), order_type="LIMIT", leverage=2)
    assert await mandate_gate.profile_cash_reject(conn, engine, m, larger) == "PROFILE_CASH_EXHAUSTED"


async def test_gate_maps_budget_limited_exposure_to_non_breach_cash_refusal(monkeypatch):
    m = mandate(capital_budget_usd=D("1000"))
    other_coin = "ETHUSDT.BINANCE"
    approval = {"mandate_id": m.id, "action_class": "ADD_RISK", "status": "APPROVED",
                "command_id": uuid.uuid4()}
    class CashLimitConn(FakeConn):
        async def fetch(self, sql, *args):
            if "SELECT instrument_id, side, filled_qty" in sql:
                return [{"instrument_id": other_coin, "side": "BUY", "filled_qty": D("0.018"),
                         "fill_price": D("50000")}]
            return await super().fetch(sql, *args)

    conn = CashLimitConn(None, mandate_row(m),
                         [{"instance_id": m.instance_id, "instrument_id": other_coin,
                           "quantity": D("0.018"), "current_price": D("50000")}], approval)
    monkeypatch.setattr(mandate_gate.profile_lifecycle, "autotrade_switch_on", AsyncMock(return_value=True))
    monkeypatch.setattr(mandate_gate, "session_verdict_for", AsyncMock(return_value=SessionVerdict.ACTIVE))
    intent = OrderIntent(client_order_id="cash-cap", instance_id=m.instance_id, instrument_id=SPOT,
                         side="BUY", order_type="LIMIT", quantity=D("0.004"), price=D("50000"))
    reject = await mandate_gate.gate_ai_order(conn, make_engine(make_perp_dispatcher()),
                                              {"command_id": approval["command_id"]},
                                              {"approval_id": str(uuid.uuid4())}, intent, m, None)
    assert reject == "PROFILE_CASH_EXHAUSTED"
    assert conn.events == []


async def test_fill_uses_the_order_mode_after_profile_mode_and_switch_change(monkeypatch):
    m = mandate()
    command_id = uuid.uuid4()

    class StoredModeConn(FakeConn):
        async def fetchrow(self, sql, *args):
            if "FROM execution_commands" in sql:
                return {"command_id": command_id, "payload": {"profile_execution_mode": "AUTO"}}
            return await super().fetchrow(sql, *args)

    conn = StoredModeConn(None, mandate_row(m))
    monkeypatch.setattr(profile_lifecycle, "autotrade_switch_on", AsyncMock(return_value=False))
    await server.record_profile_trade(conn, make_engine(), m.instance_id, SPOT, "BUY", D("0.01"), D("50000"),
                                      entry=True, command_id=command_id)
    row, = decisions(conn)
    assert row["stage"] == "FILL" and row["detail"]["mode"] == "AUTO"


async def test_successful_dispatch_persists_the_effective_profile_mode(monkeypatch):
    m = mandate(autonomy_level=3)
    monkeypatch.setattr(profile_lifecycle, "autotrade_switch_on", AsyncMock(return_value=True))
    conn, engine = await run_command(
        m, {"instrument_id": SPOT, "side": "BUY", "quantity": "0.001", "stop_loss": "49750"})
    engine.dispatcher.dispatch.assert_called_once()
    persisted = [args for sql, args in conn.executed
                 if "profile_execution_mode" in sql and "UPDATE execution_commands" in sql]
    assert persisted and persisted[0][1] == "AUTO"
    reserves = [args for sql, args in conn.executed
                if "profile_cash_reserve_usd" in sql and "UPDATE execution_commands" in sql]
    assert reserves and D(str(reserves[0][1])) > 0


async def test_successful_dispatch_persists_effective_mode_in_postgres(h):
    """Exercise the real command SQL so PostgreSQL validates the stored execution-mode parameter."""
    m = await h.mandate(level=3, instruments=(SPOT,))
    command_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    await h.pool.execute(
        "INSERT INTO system_settings (key, value) VALUES ('autotrade.enabled', '1') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    await h.pool.execute(
        """INSERT INTO ai_approvals (id, mandate_id, action_class, command_type, payload, summary, source, source_ref,
                   level_at_creation, timeout_policy, expires_at, status, command_id)
           VALUES ($1, $2, 'ADD_RISK', 'SUBMIT_ORDER', '{}'::jsonb, 'buy', 'debate', $3, 3, 'DENY',
                   now() + interval '15 minutes', 'APPROVED', $4)""",
        approval_id, m.id, str(uuid.uuid4()), command_id)
    await h.pool.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id,
                   requester_id, payload, status, environment, account_id, mandate_id)
           VALUES ($1, $2, 'SUBMIT_ORDER', $3, 'ai:debate_worker', $4::jsonb, 'PENDING', 'PAPER',
                   'PAPER-ACCOUNT-001', $5)""",
        command_id, f"mode-persist:{command_id}", m.instance_id,
        json.dumps({"mandate_id": str(m.id), "approval_id": str(approval_id), "instrument_id": SPOT,
                    "side": "BUY", "quantity": "0.001", "stop_loss": "49750"}), m.id)

    dispatcher = make_perp_dispatcher()
    engine = make_engine(dispatcher)
    engine.bridge = MagicMock()
    quotes = server.SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000, 50010)
    profile_lifecycle.reset_switch_cache()
    poller = asyncio.create_task(server.command_poller(h.pool, engine, quotes))
    try:
        for _ in range(200):
            status = await h.pool.fetchval(
                "SELECT status FROM execution_commands WHERE command_id = $1", command_id)
            if status in ("COMPLETED", "REJECTED", "FAILED"):
                break
            await asyncio.sleep(0.05)
        row = await h.pool.fetchrow(
            "SELECT status, payload FROM execution_commands WHERE command_id = $1", command_id)
    finally:
        poller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poller
        profile_lifecycle.reset_switch_cache()

    assert row["status"] == "COMPLETED"
    assert json.loads(row["payload"])["profile_execution_mode"] == "AUTO"
    assert await h.pool.fetchval(
        "SELECT count(*) FROM paper_orders WHERE instance_id = $1", m.instance_id) == 1


async def test_spot_cash_gate_uses_cost_basis_since_last_flat(h):
    m = await h.mandate(budget=1000)
    await h.pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
                   quantity, price, filled_qty, avg_fill_price, status, created_at, updated_at)
           VALUES ('roundtrip-buy', $1, 'PAPER-ACCOUNT-001', 'ETHUSDT.BINANCE', 'BUY', 'LIMIT', 1, 100, 1, 100,
                   'FILLED', now() - interval '3 days', now() - interval '3 days'),
                  ('roundtrip-sell', $1, 'PAPER-ACCOUNT-001', 'ETHUSDT.BINANCE', 'SELL', 'LIMIT', 1, 100, 1, 100,
                   'FILLED', now() - interval '2 days', now() - interval '2 days'),
                  ('current-buy', $1, 'PAPER-ACCOUNT-001', 'ETHUSDT.BINANCE', 'BUY', 'LIMIT', 1, 900, 1, 900,
                   'FILLED', now() - interval '1 day', now() - interval '1 day')""", m.instance_id)
    engine = make_engine(make_perp_dispatcher())
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            reject = await mandate_gate.profile_cash_reject(
                conn, engine, m, MandateOrder(SPOT, "BUY", D("0.004"), D("50000"), order_type="LIMIT"))
    assert reject == "PROFILE_CASH_EXHAUSTED"


async def test_two_connections_share_the_committed_working_order_claim(h):
    m = await h.mandate(budget=1000)
    await h.pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
                   quantity, price, filled_qty, avg_fill_price, status)
           VALUES ('existing-spot-holding', $1, 'PAPER-ACCOUNT-001', 'ETHUSDT.BINANCE', 'BUY', 'LIMIT',
                   0.014, 50000, 0.014, 50000, 'FILLED')""", m.instance_id)
    first_engine = make_engine(make_perp_dispatcher())
    second_engine = make_engine(make_perp_dispatcher())
    order = MandateOrder(SPOT, "BUY", D("0.004"), D("50000"), order_type="LIMIT")
    first_checked = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def first_claim():
        async with h.pool.acquire() as conn:
            async with conn.transaction():
                assert await mandate_gate.profile_cash_reject(conn, first_engine, m, order) is None
                first_checked.set()
                await release_first.wait()
                await conn.execute(
                    """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id,
                               side, order_type, quantity, price, filled_qty, status)
                       VALUES ('first-working-order', $1, 'PAPER-ACCOUNT-001', $2, 'BUY', 'LIMIT', .004, 50000, 0,
                               'ACCEPTED')""", m.instance_id, SPOT)

    async def second_claim():
        await first_checked.wait()
        second_started.set()
        async with h.pool.acquire() as conn:
            async with conn.transaction():
                return await mandate_gate.profile_cash_reject(conn, second_engine, m, order)

    first = asyncio.create_task(first_claim())
    second = asyncio.create_task(second_claim())
    await second_started.wait()
    await asyncio.sleep(0.05)
    release_first.set()
    await first
    assert await second == "PROFILE_CASH_EXHAUSTED"


async def test_durable_order_and_reservation_are_counted_once(h):
    m = await h.mandate(budget=1000)
    cid = uuid.uuid4()
    oid = f"AI-{cid.hex[:12].upper()}"
    await h.pool.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, mandate_id, payload)
           VALUES ($1, $2, 'SUBMIT_ORDER', $3, jsonb_build_object('client_order_id', $4::text))""",
        cid, str(cid), m.id, oid)
    await h.pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
                   quantity, price, filled_qty, status)
           VALUES ($1, $2, 'PAPER-ACCOUNT-001', $3, 'BUY', 'LIMIT', .01, 50000, 0, 'ACCEPTED')""",
        oid, m.instance_id, SPOT)
    await h.pool.execute(
        """INSERT INTO trading_orders (environment, account_id, client_order_id, strategy_id, instrument_id,
                   side, order_type, quantity, price, status)
           VALUES ('PAPER', 'PAPER-ACCOUNT-001', $1, $2, $3, 'BUY', 'LIMIT', .01, 50000, 'SUBMITTED')""",
        oid, m.instance_id, SPOT)
    await h.pool.execute(
        """INSERT INTO execution_reservations (command_id, client_order_id, environment, account_id, asset,
                   amount, fee_slippage_headroom, state)
           VALUES ($1, $2, 'PAPER', 'PAPER-ACCOUNT-001', 'USDT', 500, 0, 'ACKED_UNREFLECTED')""",
        cid, oid)
    engine = make_engine(make_perp_dispatcher())
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            reject = await mandate_gate.profile_cash_reject(
                conn, engine, m, MandateOrder(SPOT, "BUY", D("0.008"), D("50000"), order_type="LIMIT"))
    assert reject is None


async def test_two_connections_claim_unpriced_working_market_order(h):
    m = await h.mandate(budget=1000)
    await h.pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
                   quantity, price, filled_qty, avg_fill_price, status)
           VALUES ('market-profile-holding', $1, 'PAPER-ACCOUNT-001', 'ETHUSDT.BINANCE', 'BUY', 'LIMIT',
                   .014, 50000, .014, 50000, 'FILLED')""", m.instance_id)
    cid = uuid.uuid4()
    oid = f"AI-{cid.hex[:12].upper()}"
    await h.pool.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, mandate_id, payload)
           VALUES ($1, $2, 'SUBMIT_ORDER', $3,
                   jsonb_build_object('client_order_id', $4::text, 'profile_cash_reserve_usd', 205.2))""",
        cid, str(cid), m.id, oid)
    engine_a = make_engine(make_perp_dispatcher())
    engine_b = make_engine(make_perp_dispatcher())
    new_order = MandateOrder(SPOT, "BUY", D("0.004"), D("50000"), order_type="MARKET")
    checked = asyncio.Event()
    release = asyncio.Event()
    second_started = asyncio.Event()

    async def first_claim():
        async with h.pool.acquire() as conn:
            async with conn.transaction():
                assert await mandate_gate.profile_cash_reject(conn, engine_a, m, new_order) is None
                checked.set()
                await release.wait()
                await conn.execute(
                    """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side,
                               order_type, quantity, price, filled_qty, status)
                       VALUES ($1, $2, 'PAPER-ACCOUNT-001', $3, 'BUY', 'MARKET', .004, NULL, 0, 'ACCEPTED')""",
                    oid, m.instance_id, SPOT)

    async def second_claim():
        await checked.wait()
        second_started.set()
        async with h.pool.acquire() as conn:
            async with conn.transaction():
                return await mandate_gate.profile_cash_reject(conn, engine_b, m, new_order)

    first = asyncio.create_task(first_claim())
    second = asyncio.create_task(second_claim())
    await second_started.wait()
    await asyncio.sleep(0.05)
    release.set()
    await first
    assert await second == "PROFILE_CASH_EXHAUSTED"


async def test_two_connections_value_working_perp_at_initial_margin(h):
    m = await h.mandate(budget=1000, instruments=(PERP,))
    await h.pool.execute(
        "UPDATE ai_mandates SET markets = ARRAY['PERP'], allow_perps = true, direction = 'BOTH', max_leverage = 2 WHERE id = $1",
        m.id)
    cid = uuid.uuid4()
    oid = f"AI-{cid.hex[:12].upper()}"
    await h.pool.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, mandate_id, payload)
           VALUES ($1, $2, 'SUBMIT_ORDER', $3,
                   jsonb_build_object('client_order_id', $4::text, 'profile_cash_reserve_usd', 450.45))""",
        cid, str(cid), m.id, oid)
    engine_a = make_engine(make_perp_dispatcher())
    engine_b = make_engine(make_perp_dispatcher())
    next_order = MandateOrder(PERP, "BUY", D("0.011"), D("50000"), order_type="LIMIT", leverage=2)
    checked = asyncio.Event()
    release = asyncio.Event()
    second_started = asyncio.Event()

    async def first_claim():
        async with h.pool.acquire() as conn:
            async with conn.transaction():
                assert await mandate_gate.profile_cash_reject(conn, engine_a, m, next_order) is None
                checked.set()
                await release.wait()
                await conn.execute(
                    """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side,
                               order_type, quantity, price, filled_qty, status)
                       VALUES ($1, $2, 'PAPER-ACCOUNT-001', $3, 'BUY', 'LIMIT', .018, 50000, 0, 'ACCEPTED')""",
                    oid, m.instance_id, PERP)

    async def second_claim():
        await checked.wait()
        second_started.set()
        async with h.pool.acquire() as conn:
            async with conn.transaction():
                return await mandate_gate.profile_cash_reject(conn, engine_b, m, next_order)

    first = asyncio.create_task(first_claim())
    second = asyncio.create_task(second_claim())
    await second_started.wait()
    await asyncio.sleep(0.05)
    release.set()
    await first
    assert await second is None


async def test_legacy_unpriced_market_order_claims_full_profile_budget(h):
    m = await h.mandate(budget=1000)
    cid = uuid.uuid4()
    oid = f"AI-{cid.hex[:12].upper()}"
    await h.pool.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, mandate_id, payload)
           VALUES ($1, $2, 'SUBMIT_ORDER', $3, '{}'::jsonb)""",
        cid, str(cid), m.id)
    await h.pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
                   quantity, price, filled_qty, avg_fill_price, status)
           VALUES ($1, $2, 'PAPER-ACCOUNT-001', $3, 'BUY', 'MARKET', .004, NULL, 0, NULL, 'ACCEPTED')""",
        oid, m.instance_id, SPOT)

    # This fresh connection represents another orchestrator after restart: the legacy order has no memory claim
    # and its command payload predates profile_cash_reserve_usd.
    second_engine = make_engine(make_perp_dispatcher())
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            reject = await mandate_gate.profile_cash_reject(
                conn, second_engine, m,
                MandateOrder(SPOT, "BUY", D("0.004"), D("50000"), order_type="MARKET"))
    assert reject == "PROFILE_CASH_EXHAUSTED"


async def test_delayed_fill_keeps_execution_mode_from_the_order(h, monkeypatch):
    m = await h.mandate()
    cid = uuid.uuid4()
    await h.pool.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, mandate_id, payload)
           VALUES ($1, $2, 'SUBMIT_ORDER', $3, '{"profile_execution_mode":"AUTO"}'::jsonb)""",
        cid, str(cid), m.id)
    await h.pool.execute("UPDATE ai_mandates SET autonomy_level = 1 WHERE id = $1", m.id)
    monkeypatch.setattr(profile_lifecycle, "autotrade_switch_on", AsyncMock(return_value=False))
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            await server.record_profile_trade(conn, make_engine(), m.instance_id, SPOT, "BUY", D("0.01"),
                                              D("50000"), entry=True, command_id=cid)
    row = await h.pool.fetchrow(
        "SELECT instrument_id, detail FROM ai_profile_decisions WHERE mandate_id = $1 AND stage = 'FILL'", m.id)
    assert row["instrument_id"] == SPOT and json.loads(row["detail"])["mode"] == "AUTO"


async def test_entry_gate_reloads_and_locks_status_before_risk_check(monkeypatch):
    m = mandate()
    row = mandate_row(m)
    row["status"] = "PAUSED"

    class PausedConn(FakeConn):
        async def fetchrow(self, sql, *args):
            if "FROM ai_mandates" in sql:
                self.locked_status_query = "FOR UPDATE" in sql
                return row
            return await super().fetchrow(sql, *args)

    conn = PausedConn(None, mandate_row(m))
    monkeypatch.setattr(mandate_gate.profile_lifecycle, "autotrade_switch_on", AsyncMock(return_value=True))
    monkeypatch.setattr(mandate_gate, "session_verdict_for", AsyncMock(return_value=SessionVerdict.ACTIVE))
    intent = OrderIntent(client_order_id="stale-active", instance_id=m.instance_id, instrument_id=SPOT,
                         side="BUY", order_type="LIMIT", quantity=D("0.001"), price=D("50000"))
    reject = await mandate_gate.gate_ai_order(
        conn, make_engine(make_perp_dispatcher()), {"command_id": uuid.uuid4()}, {}, intent, m, None)
    assert reject == MandateReject.HALTED.value
    assert conn.locked_status_query is True


async def test_spot_exit_pnl_uses_the_remaining_cost_after_earlier_round_trips():
    m = mandate()

    class HistoryConn(FakeConn):
        """A completed trade, then a new position with a partial sale and an additional entry."""

        async def fetchrow(self, sql, *args):
            if "FROM ai_profile_decisions" in sql:
                return {"detail": {"price": "42000", "stop": "40750"}}
            return await super().fetchrow(sql, *args)

        async def fetch(self, sql, *args):
            if "SELECT side, filled_qty" in sql:
                return [{"side": side, "filled_qty": D(qty), "fill_price": D(price)}
                        for side, qty, price in [("BUY", "0.01", "50000"), ("SELL", "0.01", "51000"),
                                                 ("BUY", "0.02", "40000"), ("SELL", "0.01", "41000"),
                                                 ("BUY", "0.01", "42000")]]
            return await super().fetch(sql, *args)

    conn = HistoryConn(None, mandate_row(m))
    await server.record_profile_trade(conn, make_engine(), m.instance_id, SPOT, "SELL", D("0.02"), D("43000"),
                                      entry=False, commission=D("0.86"))
    row, = decisions(conn)
    assert D(row["detail"]["realized_pnl"]) == D("39.14")
    assert D(row["detail"]["r_multiple"]) == D("7.828")


class ProfileRatchetDB(PaperOrdersDB):
    """Supply profile exit overrides alongside the existing ratchet persistence double."""

    def __init__(self, activate, trail):
        super().__init__()
        self.profile = {"exit_trail_activate_pct": activate, "exit_trail_pct": trail}
        self.reads = 0

    async def fetchrow(self, sql, *args):
        if "FROM ai_mandates" in sql:
            self.reads += 1
            return self.profile
        return await super().fetchrow(sql, *args)


@pytest.mark.parametrize("activate,trail,moved,stop", [(D("1"), D("0.5"), 1, D("50745.00")),
                                                      (None, None, 0, D("48000")),
                                                      (None, D("0.5"), 0, D("48000"))])
async def test_ratchet_uses_profile_values_and_global_fallback(activate, trail, moved, stop):
    db = ProfileRatchetDB(activate, trail)
    engine = await open_perp(db)
    redis = AsyncMock()
    redis.hgetall.return_value = {}
    redis.sismember.return_value = False
    assert await tick(engine, redis, db, "51000", now=0) == moved
    assert stop_of(engine)[1] == stop
    await tick(engine, redis, db, "51000", now=15)
    assert db.reads == 1
    await tick(engine, redis, db, "51000", now=31)
    assert db.reads == 2


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_p15_profile") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED', name = id::text")
    await harness.pool.execute("DELETE FROM ai_profile_coin_locks")
    await harness.pool.execute("TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_orders, paper_positions CASCADE")
    try:
        yield harness
    finally:
        await close_harness(harness)


async def test_retention_deletes_only_older_than_90_days_in_bounded_batches(h):
    m = await h.mandate()
    await h.pool.execute(
        """INSERT INTO ai_profile_decisions (mandate_id, stage, outcome, ts)
           SELECT $1, 'ORDER', 'OLD', now() - interval '91 days' FROM generate_series(1, 10001)""", m.id)
    await h.pool.execute(
        """INSERT INTO ai_profile_decisions (mandate_id, stage, outcome, ts) VALUES
           ($1, 'ORDER', 'KEEP', now() - interval '89 days'), ($1, 'ORDER', 'KEEP', now())""", m.id)
    assert await server.prune_profile_decisions(h.pool) == 10001
    assert await h.pool.fetchval("SELECT count(*) FROM ai_profile_decisions WHERE mandate_id = $1", m.id) == 2


@pytest.mark.parametrize("method", ["post", "patch"])
async def test_api_refuses_margin_before_spending_totp(h, method):
    m = await h.mandate(status="STOPPED")
    url = "/api/v1/ai/mandates" + (f"/{m.id}" if method == "patch" else "")
    body = {"markets": ["SPOT_MARGIN"]}
    if method == "post":
        body.update(name="margin-rejected", capital_budget_usd="1000", instruments=[SPOT], max_position_pct="25")
    async with h.client() as client:
        response = await client.request(method.upper(), url, json=body, headers=h.headers)
    assert response.status_code == 422 and response.json()["detail"] == "MARGIN_NOT_AVAILABLE"


async def test_real_reservations_are_scoped_to_the_owning_profile_and_counted_once(h):
    first = await h.mandate(budget=1000)
    second = await h.mandate(budget=1000, instruments=("ETHUSDT.BINANCE",))
    cid = uuid.uuid4()
    oid = f"reserved-{cid.hex}"
    await h.pool.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, mandate_id, payload)
           VALUES ($1, $2, 'SUBMIT_ORDER', $3, '{}'::jsonb)""", cid, str(cid), first.id)
    await h.pool.execute(
        """INSERT INTO trading_orders (environment, account_id, client_order_id, strategy_id, instrument_id,
               side, order_type, quantity, price, status)
           VALUES ('PAPER', 'PAPER-ACCOUNT-001', $1, $2, $3, 'BUY', 'LIMIT', 0.018, 50000, 'SUBMITTED')""",
        oid, first.instance_id, SPOT)
    await h.pool.execute(
        """INSERT INTO execution_reservations (command_id, client_order_id, environment, account_id, asset,
               amount, fee_slippage_headroom, state) VALUES ($1, $2, 'PAPER', 'PAPER-ACCOUNT-001', 'USDT', 899, 1, 'PRE_SEND')""",
        cid, oid)
    d = make_perp_dispatcher()
    engine = make_engine(d)
    order = MandateOrder(SPOT, "BUY", D("0.004"), D("50000"), order_type="LIMIT")
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            assert await mandate_gate.profile_cash_reject(conn, engine, first, order) == "PROFILE_CASH_EXHAUSTED"
            assert await mandate_gate.profile_cash_reject(conn, engine, second, order) is None
            d.open_orders[oid] = {"instance_id": first.instance_id, "side": "BUY", "reserved_notional": D("900")}
            smaller = MandateOrder(SPOT, "BUY", D("0.001"), D("50000"), order_type="LIMIT")
            assert await mandate_gate.profile_cash_reject(conn, engine, first, smaller) is None


async def test_entry_log_reads_the_spot_stop_from_its_execution_command(h):
    m = await h.mandate()
    cid = uuid.uuid4()
    await h.pool.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, mandate_id, payload)
           VALUES ($1, $2, 'SUBMIT_ORDER', $3, '{"stop_loss":"49500"}'::jsonb)""", cid, str(cid), m.id)
    d = make_perp_dispatcher()
    engine = make_engine(d)
    engine.bridge = MagicMock()
    oid = f"AI-{cid.hex[:12].upper()}"
    intent = OrderIntent(client_order_id=oid, instance_id=m.instance_id, instrument_id=SPOT, side="BUY",
                         order_type="LIMIT", quantity=D("0.02"), price=D("50000"))
    assert d.dispatch(intent, lambda _: None, nautilus_order=None)[0]
    server.remember_dispatched(engine, oid, cid)
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            await server.apply_paper_fill(conn, engine, PaperFill(oid, SPOT, "BUY", D("0.02"), D("50000"), "MAKER"),
                                          m.instance_id, "LIMIT", D("50000"))
    raw = await h.pool.fetchval("SELECT detail FROM ai_profile_decisions WHERE mandate_id = $1 AND stage = 'FILL'", m.id)
    assert json.loads(raw)["stop"] == "49500"


@pytest.mark.parametrize("instrument,short", [(SPOT, False), (PERP, False), (PERP, True)])
async def test_fill_exit_decision_contract_and_no_notifications(h, instrument, short):
    m = await h.mandate(instruments=(SPOT, PERP))
    await h.pool.execute("UPDATE ai_mandates SET markets = ARRAY['SPOT','PERP'], allow_perps = true, direction = 'BOTH' WHERE id = $1", m.id)
    d = make_perp_dispatcher()
    engine = make_engine(d)
    engine.bridge = MagicMock()
    side = "SELL" if short else "BUY"
    entry = OrderIntent(client_order_id=f"entry-{uuid.uuid4().hex}", instance_id=m.instance_id,
                        instrument_id=instrument, side=side, order_type="LIMIT", quantity=D("0.02"),
                        price=D("50000"), leverage=2 if instrument == PERP else None,
                        margin_mode="ISOLATED" if instrument == PERP else None)
    assert d.dispatch(entry, lambda _: None, nautilus_order=None)[0]
    d.open_orders[entry.client_order_id]["attach_stop"] = D("50500") if short else D("49500")
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            await server.apply_paper_fill(conn, engine, PaperFill(entry.client_order_id, instrument, side, D("0.02"), D("50000"), "MAKER"),
                                          m.instance_id, "LIMIT", D("50000"))
        row = await conn.fetchrow("SELECT instrument_id, stage, outcome, detail FROM ai_profile_decisions WHERE mandate_id = $1 AND stage = 'FILL'", m.id)
        assert row is not None and row["instrument_id"] == instrument and row["outcome"] == "FILLED"
        detail = json.loads(row["detail"])
        assert set(detail) == FILL_KEYS
        assert detail["side"] == ("SHORT" if short else "LONG") and detail["mode"] == "ASK"
        assert detail["market"] == ("PERP" if instrument == PERP else "SPOT")
        assert D(detail["stop"]) == (D("50500") if short else D("49500"))
        exit_side = "BUY" if short else "SELL"
        exit_price = D("49000") if short else D("51000")
        close = OrderIntent(client_order_id=f"exit-{uuid.uuid4().hex}", instance_id=m.instance_id,
                            instrument_id=instrument, side=exit_side, order_type="LIMIT", quantity=D("0.02"),
                            price=exit_price, reduce_only=instrument == PERP)
        assert d.dispatch(close, lambda _: None, nautilus_order=None)[0]
        async with conn.transaction():
            await server.apply_paper_fill(conn, engine, PaperFill(close.client_order_id, instrument, exit_side, D("0.02"), exit_price, "MAKER"),
                                          m.instance_id, "LIMIT", exit_price)
        row = await conn.fetchrow("SELECT instrument_id, stage, outcome, detail FROM ai_profile_decisions WHERE mandate_id = $1 AND stage = 'EXIT'", m.id)
        assert row is not None and row["instrument_id"] == instrument and row["outcome"] == "MANUAL"
        detail = json.loads(row["detail"])
        assert set(detail) == EXIT_KEYS and detail["side"] == ("SHORT" if short else "LONG")
        assert D(detail["realized_pnl"]) > 0 and D(detail["r_multiple"]) > 0
    assert await h.pool.fetchval("SELECT count(*) FROM notifications_outbox") == 0
