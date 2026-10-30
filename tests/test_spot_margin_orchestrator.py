"""P4.5 orchestrator persistence, interest, safety and exit wiring."""

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, localcontext
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from cryptonite_execution.bridge import EventBridge, EventBridgeWorker
from cryptonite_execution.engine import PaperTradingEngine
from cryptonite_execution.matching import PaperFill
from cryptonite_execution.risk import OrderIntent, SpotMarginAccountState
from cryptonite_execution.spot_margin import interest_for_period
from cryptonite_orchestrator import server
from cryptonite_orchestrator.server import SimpleQuoteCache
from migrated_db import migrated_database
from test_paper_risk_guardrails import make_fresh_dispatcher

MARGIN = "BTCUSDT-MARGIN.BINANCE"
SPOT = "BTCUSDT.BINANCE"
ACCOUNT = "PAPER-ACCOUNT-001"
INSTANCE = "AI_MANDATE_00000000000000000000000000000001"


@pytest.fixture(scope="module")
def margin_db_url():
    with migrated_database("cryptonite_p45") as url:
        yield url


@pytest.fixture
async def margin_orch_ctx(margin_db_url):
    pool = await asyncpg.create_pool(margin_db_url, min_size=1, max_size=2)
    strat_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE execution_commands, paper_execution_events, paper_trades, paper_positions, paper_orders, "
            "paper_margin_interest, paper_margin_accounts, paper_strategy_instances, paper_accounts CASCADE"
        )
        await conn.execute(
            "INSERT INTO strategies (id, slug, name, description, author) VALUES ($1, $2, $2, 'test', 'test')",
            strat_id, f"margin-p45-{strat_id.hex[:8]}",
        )
        await conn.execute(
            """INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit,
                   default_parameters, parameter_schema) VALUES ($1, $1, 1, 'h', 'c', '{}'::jsonb, '{}'::jsonb)""",
            strat_id,
        )
        await conn.execute(
            """INSERT INTO paper_accounts (account_id, venue, currency, starting_balance, cash_balance,
                   total_equity, realized_pnl, daily_realized_pnl, daily_loss_latched, updated_at)
               VALUES ($1, 'BINANCE', 'USDT', 10000, 10000, 10000, 0, 0, FALSE, now())""",
            ACCOUNT,
        )
        await conn.execute(
            """INSERT INTO paper_strategy_instances (instance_id, account_id, strategy_version_id,
                   strategy_source_hash, risk_policy_hash, strategy_name, instrument_id, timeframe,
                   resolved_parameters, allocation_usd, status)
               VALUES ($1, $2, $3, 's', 'r', 'Margin P4.5', $4, '1m', '{}'::jsonb, 1000, 'RUNNING')""",
            INSTANCE, ACCOUNT, strat_id, "BTCUSDT-MARGIN.BINANCE",
        )
    dispatcher, cache = make_fresh_dispatcher()
    engine = PaperTradingEngine(
        account_id=ACCOUNT, trader_id="TRADER-P45", engine_run_id=f"RUN-{uuid.uuid4().hex[:8]}",
        pg_pool=pool, redis_client=None,
    )
    engine.dispatcher = dispatcher
    engine.kill_switch = dispatcher.kill_switch
    engine.bridge = EventBridge(dispatcher.kill_switch)
    worker = EventBridgeWorker(engine.bridge, pool, engine.engine_run_id)

    async def open_long(leverage=2):
        borrowed_at = datetime(2026, 10, 29, 12, 20, tzinfo=UTC)
        intent = OrderIntent(
            client_order_id="P45-OPEN", instance_id=INSTANCE, instrument_id=MARGIN, side="BUY",
            order_type="MARKET", quantity=D("0.01"), leverage=leverage, margin_mode="ISOLATED",
            stop_loss=D("48000"), require_stop=True,
        )
        ok, reason = dispatcher.dispatch(intent, MagicMock(), MagicMock())
        assert ok, reason
        realized = dispatcher.handle_fill("P45-OPEN", D("0.01"), D("50000"), D("0.5"),
                                           side="BUY", instrument_id=MARGIN)
        engine.bridge.enqueue_event({
            "event_type": "ORDER_FILLED", "event_version": 1, "client_order_id": "P45-OPEN",
            "account_id": ACCOUNT, "instance_id": INSTANCE, "instrument_id": MARGIN, "side": "BUY",
            "price": "50000", "quantity": "0.01", "commission": "0.5", "filled_qty": "0.01",
            "avg_fill_price": "50000", "status": "FILLED",
            **dispatcher.fill_event_fields(MARGIN, realized, D("50000"), opened_at=borrowed_at),
        })
        assert await worker.drain_and_commit_batch() == 1
        persisted_opened_at = await pool.fetchval(
            "SELECT opened_at FROM paper_margin_accounts WHERE account_id=$1 AND pair='BTCUSDT' AND status='OPEN'",
            ACCOUNT,
        )
        assert persisted_opened_at == borrowed_at
        return borrowed_at

    async def open_short():
        borrowed_at = datetime(2026, 10, 29, 12, 20, tzinfo=UTC)
        intent = OrderIntent(
            client_order_id="P45-OPEN-SHORT", instance_id=INSTANCE, instrument_id=MARGIN, side="SELL",
            order_type="MARKET", quantity=D("0.01"), leverage=2, margin_mode="ISOLATED",
            stop_loss=D("53000"), require_stop=True,
        )
        ok, reason = dispatcher.dispatch(intent, MagicMock(), MagicMock())
        assert ok, reason
        realized = dispatcher.handle_fill("P45-OPEN-SHORT", D("0.01"), D("50000"), D("0.5"),
                                           side="SELL", instrument_id=MARGIN)
        engine.bridge.enqueue_event({
            "event_type": "ORDER_FILLED", "event_version": 1, "client_order_id": "P45-OPEN-SHORT",
            "account_id": ACCOUNT, "instance_id": INSTANCE, "instrument_id": MARGIN, "side": "SELL",
            "price": "50000", "quantity": "0.01", "commission": "0.5", "filled_qty": "0.01",
            "avg_fill_price": "50000", "status": "FILLED",
            **dispatcher.fill_event_fields(MARGIN, realized, D("50000"), opened_at=borrowed_at),
        })
        assert await worker.drain_and_commit_batch() == 1
        persisted_opened_at = await pool.fetchval(
            "SELECT opened_at FROM paper_margin_accounts WHERE account_id=$1 AND pair='BTCUSDT' AND status='OPEN'",
            ACCOUNT,
        )
        assert persisted_opened_at == borrowed_at
        return borrowed_at

    yield {"pool": pool, "engine": engine, "dispatcher": dispatcher, "cache": cache,
           "worker": worker, "open_long": open_long, "open_short": open_short}
    await pool.close()


class RehydratePool:
    """Small asyncpg-shaped pool containing one persisted isolated short."""

    async def fetchrow(self, query, *args):
        if "paper_accounts" in query:
            return {"cash_balance": D("9499.5")}
        return None

    async def fetch(self, query, *args):
        if "instrument_id NOT LIKE '%-PERP.%'" in query and "instrument_id NOT LIKE '%-MARGIN.%'" not in query:
            return [{"instrument_id": MARGIN, "quantity": D("0.01")}]
        if "paper_positions" in query and "LIKE '%-PERP.%'" in query:
            return []
        if "paper_margin_accounts" in query:
            return [{
                "pair": "BTCUSDT", "side": "SHORT", "base_qty": D("0"), "quote_qty": D("1000"),
                "borrowed_base": D("0.01"), "borrowed_quote": D("0"), "interest_base": D("0.000001"),
                "interest_quote": D("0"), "collateral_usdt": D("500"), "entry_price": D("50000"),
                "leverage": 2, "instance_id": "AI_MANDATE_TEST", "status": "OPEN",
            }]
        return []


@pytest.mark.asyncio
async def test_rehydrate_restores_margin_debt_separately_from_spot_holdings():
    dispatcher, _ = make_fresh_dispatcher()
    engine = SimpleNamespace(account_id="PAPER-ACCOUNT-001", dispatcher=dispatcher,
                             kill_switch=dispatcher.kill_switch)

    await server.rehydrate_dispatcher_state(RehydratePool(), engine)

    assert dispatcher.positions.get(MARGIN, D("0")) == 0
    assert MARGIN not in dispatcher.positions
    account = dispatcher.margin_accounts[MARGIN]
    assert account == SpotMarginAccountState(
        "SHORT", D("0"), D("1000"), D("0.01"), D("0"), D("0.000001"), D("0"),
        D("500"), D("50000"), 2, "AI_MANDATE_TEST",
    )


def test_margin_reduce_only_close_passes_latches_and_entry_cannot_masquerade_as_exit():
    dispatcher, _ = make_fresh_dispatcher()
    entry = OrderIntent(
        client_order_id="OPEN", instance_id="AI_MANDATE_TEST", instrument_id=MARGIN,
        side="BUY", order_type="MARKET", quantity=D("0.01"), leverage=2,
        margin_mode="ISOLATED", stop_loss=D("47000"), require_stop=True,
    )
    assert dispatcher.dispatch(entry, MagicMock(), MagicMock())[0]
    dispatcher.handle_fill("OPEN", D("0.01"), D("50000"), D("0.5"), side="BUY", instrument_id=MARGIN)

    dispatcher.kill_switch.trip("test", "latched")
    dispatcher.daily_loss_latched = True
    close = OrderIntent(
        client_order_id="CLOSE", instance_id="AI_MANDATE_TEST", instrument_id=MARGIN,
        side="SELL", order_type="MARKET", quantity=D("0.005"), leverage=2,
        margin_mode="ISOLATED", reduce_only=True,
    )
    assert dispatcher.evaluate_intent(close) == (True, "APPROVED", D("0"))
    assert not dispatcher.is_entry_order({"is_margin": True, "reduce_only": True})

    disguised_entry = OrderIntent(
        client_order_id="NOT-CLOSE", instance_id="AI_MANDATE_TEST", instrument_id=MARGIN,
        side="BUY", order_type="MARKET", quantity=D("0.005"), leverage=2,
        margin_mode="ISOLATED", reduce_only=True,
    )
    assert dispatcher.evaluate_intent(disguised_entry)[1] == "REDUCE_ONLY_INVALID_DIRECTION"


@pytest.mark.asyncio
async def test_margin_interest_is_exact_and_idempotent_across_concurrent_passes_and_restart(margin_orch_ctx):
    borrowed_at = await margin_orch_ctx["open_long"]()
    pool = margin_orch_ctx["pool"]
    engine = margin_orch_ctx["engine"]
    settle = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)

    assert hasattr(server, "accrue_margin_interest_once"), "P4.5 hourly interest accrual is not implemented"
    await asyncio.gather(*(
        server.accrue_margin_interest_once(pool, engine, now=settle)
        for _ in range(4)
    ))

    rows = await pool.fetch(
        "SELECT period_start, period_end, asset, principal, hourly_rate, amount "
        "FROM paper_margin_interest ORDER BY period_end"
    )
    assert [(row["period_start"], row["period_end"], row["asset"]) for row in rows] == [
        (borrowed_at, settle, "USDT"),
        (settle, datetime(2026, 10, 29, 14, 0, tzinfo=UTC), "USDT"),
    ]
    assert [row["principal"] for row in rows] == [D("250"), D("250")]
    assert [row["hourly_rate"] for row in rows] == [D("0.00000656"), D("0.00000656")]
    first_charge = interest_for_period(D("250"), D("0.00000656"), D("2400"))
    first_full_hour = interest_for_period(D("250"), D("0.00000656"), D("3600"))
    assert [row["amount"] for row in rows] == [first_charge, first_full_hour]
    with localcontext() as context:
        context.prec = 38
        ledger_total = sum((row["amount"] for row in rows), D("0"))
        assert ledger_total == first_charge + first_full_hour
        assert engine.dispatcher.margin_accounts[MARGIN].interest_quote == ledger_total

    restarted_dispatcher, _ = make_fresh_dispatcher()
    restarted = SimpleNamespace(account_id=ACCOUNT, dispatcher=restarted_dispatcher,
                                kill_switch=restarted_dispatcher.kill_switch)
    await server.rehydrate_dispatcher_state(pool, restarted)
    await server.accrue_margin_interest_once(pool, restarted, now=settle)
    assert await pool.fetchval("SELECT count(*) FROM paper_margin_interest") == 2
    assert restarted_dispatcher.margin_accounts[MARGIN].interest_quote == engine.dispatcher.margin_accounts[MARGIN].interest_quote
    second_settlement = datetime(2026, 10, 29, 14, 0, tzinfo=UTC)
    await server.accrue_margin_interest_once(pool, restarted, now=second_settlement)
    rows = await pool.fetch("SELECT period_start, period_end, amount FROM paper_margin_interest ORDER BY period_end")
    assert [(row["period_start"], row["period_end"]) for row in rows] == [
        (borrowed_at, settle), (settle, second_settlement),
        (second_settlement, datetime(2026, 10, 29, 15, 0, tzinfo=UTC)),
    ]
    third_charge = interest_for_period(D("250"), D("0.00000656"), D("3600"))
    with localcontext() as context:
        context.prec = 38
        assert sum((row["amount"] for row in rows), D("0")) == first_charge + first_full_hour + third_charge


@pytest.mark.asyncio
async def test_mid_hour_repay_uses_settled_interest_and_ledger_matches_interest_repaid(margin_orch_ctx):
    borrowed_at = await margin_orch_ctx["open_long"]()
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    dispatcher = engine.dispatcher
    repay_at = datetime(2026, 10, 29, 13, 20, tzinfo=UTC)

    await server.accrue_margin_interest_once(pool, engine, now=repay_at, repaid_instrument_id=MARGIN)

    rows = await pool.fetch("SELECT * FROM paper_margin_interest ORDER BY period_end")
    account = dispatcher.margin_accounts[MARGIN]
    interest_repaid = account.interest_quote
    assert [(row["period_start"], row["period_end"]) for row in rows] == [
        (borrowed_at, datetime(2026, 10, 29, 13, 0, tzinfo=UTC)),
        (datetime(2026, 10, 29, 13, 0, tzinfo=UTC), datetime(2026, 10, 29, 14, 0, tzinfo=UTC)),
    ]
    with localcontext() as context:
        context.prec = 38
        ledger_total = sum((row["amount"] for row in rows), D("0"))
        assert ledger_total == interest_repaid
        assert interest_repaid == (
            interest_for_period(D("250"), D("0.00000656"), D("2400"))
            + interest_for_period(D("250"), D("0.00000656"), D("3600"))
        )

    cash_before = dispatcher.cash_balance
    dispatcher.flatten_margin_position(MARGIN, D("50000"), D("0"), "P45-REPAY")
    with localcontext() as context:
        context.prec = 38
        expected_cash = (cash_before + D("250") - interest_repaid).quantize(
            D("0.000000000000000001"), rounding=ROUND_HALF_UP,
        )
    assert dispatcher.cash_balance == expected_cash
    assert MARGIN not in dispatcher.margin_accounts


@pytest.mark.parametrize("leverage", [3, 7])
@pytest.mark.asyncio
async def test_repeating_decimal_borrow_persists_exactly_and_interest_does_not_skip(margin_orch_ctx, leverage):
    await margin_orch_ctx["open_long"](leverage)
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    account = engine.dispatcher.margin_accounts[MARGIN]
    persisted = await pool.fetchrow(
        "SELECT borrowed_quote FROM paper_margin_accounts WHERE account_id=$1 AND pair='BTCUSDT' AND status='OPEN'",
        ACCOUNT,
    )
    assert persisted["borrowed_quote"] == account.borrowed_quote

    settle = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)
    await server.accrue_margin_interest_once(pool, engine, now=settle)

    assert await pool.fetchval("SELECT count(*) FROM paper_margin_interest") == 2
    persisted_interest = await pool.fetchval(
        "SELECT interest_quote FROM paper_margin_accounts WHERE account_id=$1 AND pair='BTCUSDT' AND status='OPEN'",
        ACCOUNT,
    )
    assert persisted_interest == account.interest_quote > 0


@pytest.mark.asyncio
async def test_paper_fill_accrues_prorated_interest_before_full_repay(margin_orch_ctx):
    borrowed_at = await margin_orch_ctx["open_long"]()
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    dispatcher = engine.dispatcher
    repay_at = datetime(2026, 10, 29, 12, 30, tzinfo=UTC)
    intent = OrderIntent(
        client_order_id="P45-PRE-SETTLEMENT-REPAY", instance_id=INSTANCE, instrument_id=MARGIN,
        side="SELL", order_type="MARKET", quantity=D("0.01"), leverage=2,
        margin_mode="ISOLATED", reduce_only=True,
    )
    ok, reason = dispatcher.dispatch(intent, MagicMock(), MagicMock())
    assert ok, reason

    async with pool.acquire() as conn:
        await server.apply_paper_fill(
            conn, engine,
            PaperFill("P45-PRE-SETTLEMENT-REPAY", MARGIN, "SELL", D("0.01"), D("50000"), "TAKER"),
            INSTANCE, "MARKET", None, now=repay_at,
        )
    assert await margin_orch_ctx["worker"].drain_and_commit_batch() == 1

    row = await pool.fetchrow("SELECT period_start, period_end, asset, amount FROM paper_margin_interest")
    assert row is not None
    assert (row["period_start"], row["period_end"], row["asset"]) == (borrowed_at, repay_at, "USDT")
    assert row["amount"] == interest_for_period(D("250"), D("0.00000656"), D("600"))
    assert await pool.fetchval("SELECT status FROM paper_margin_accounts WHERE account_id=$1 AND pair='BTCUSDT'",
                               ACCOUNT) == "CLOSED"


@pytest.mark.asyncio
async def test_margin_reduce_fill_waits_for_previous_bridge_snapshot(margin_orch_ctx):
    await margin_orch_ctx["open_long"]()
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    dispatcher = engine.dispatcher
    account = dispatcher.margin_accounts[MARGIN]
    account.quote_qty += D("1")  # Simulate a newer in-memory fill whose bridge event is still queued.
    intent = OrderIntent(
        client_order_id="P45-REDUCE-AFTER-QUEUED-FILL", instance_id=INSTANCE, instrument_id=MARGIN,
        side="SELL", order_type="MARKET", quantity=D("0.01"), reduce_only=True,
    )
    ok, reason = dispatcher.dispatch(intent, MagicMock(), MagicMock())
    assert ok, reason

    async with pool.acquire() as conn:
        booked = await server.apply_paper_fill(
            conn, engine,
            PaperFill(intent.client_order_id, MARGIN, "SELL", D("0.01"), D("50000"), "TAKER"),
            INSTANCE, "MARKET", None, now=datetime(2026, 10, 29, 12, 30, tzinfo=UTC),
        )

    assert not booked
    assert MARGIN in dispatcher.margin_accounts
    assert intent.client_order_id in dispatcher.open_orders
    assert await pool.fetchval("SELECT count(*) FROM paper_margin_interest") == 0


@pytest.mark.asyncio
async def test_margin_fill_queue_rejection_rolls_back_and_keeps_order_working(margin_orch_ctx, monkeypatch):
    await margin_orch_ctx["open_long"]()
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    dispatcher = engine.dispatcher
    intent = OrderIntent(
        client_order_id="P45-QUEUE-FULL-CLOSE", instance_id=INSTANCE, instrument_id=MARGIN,
        side="SELL", order_type="MARKET", quantity=D("0.01"), reduce_only=True,
    )
    ok, reason = dispatcher.dispatch(intent, MagicMock(), MagicMock())
    assert ok, reason
    await server.accrue_margin_interest_once(
        pool, engine, now=datetime(2026, 10, 29, 12, 25, tzinfo=UTC), repaid_instrument_id=MARGIN,
    )
    before = replace(dispatcher.margin_accounts[MARGIN])
    monkeypatch.setattr(engine.bridge, "enqueue_event", lambda _: False)

    async with pool.acquire() as conn:
        booked = await server.apply_paper_fill(
            conn, engine,
            PaperFill(intent.client_order_id, MARGIN, "SELL", D("0.01"), D("50000"), "TAKER"),
            INSTANCE, "MARKET", None, now=datetime(2026, 10, 29, 12, 25, tzinfo=UTC),
        )

    assert not booked
    assert dispatcher.margin_accounts[MARGIN] == before
    assert intent.client_order_id in dispatcher.open_orders
    assert await pool.fetchval(
        "SELECT status FROM paper_margin_accounts WHERE account_id=$1 AND pair='BTCUSDT' AND status='OPEN'",
        ACCOUNT,
    ) == "OPEN"


@pytest.mark.asyncio
async def test_margin_resting_entry_does_not_fill_during_ingress_gap(margin_orch_ctx):
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    dispatcher = engine.dispatcher
    intent = OrderIntent(
        client_order_id="P45-ENTRY-INGRESS-GAP", instance_id=INSTANCE, instrument_id=MARGIN,
        side="BUY", order_type="LIMIT", quantity=D("0.01"), price=D("50000"), leverage=2,
        margin_mode="ISOLATED", stop_loss=D("48000"), require_stop=True,
    )
    ok, reason = dispatcher.dispatch(intent, MagicMock(), MagicMock())
    assert ok, reason
    cash_before = dispatcher.cash_balance
    dispatcher.ingress_health.is_fresh.return_value = False  # recent-arrival quote, discontinuous sequence

    await server.match_resting_orders(pool, engine, SPOT, 49_000, 49_001)

    assert MARGIN not in dispatcher.margin_accounts
    assert intent.client_order_id in dispatcher.open_orders
    assert dispatcher.cash_balance == cash_before


@pytest.mark.asyncio
async def test_resting_margin_entry_cancels_when_fill_crosses_structural_stop(margin_orch_ctx):
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    dispatcher = engine.dispatcher
    intent = OrderIntent(
        client_order_id="P45-ENTRY-GAPPED-THROUGH-STOP", instance_id=INSTANCE, instrument_id=MARGIN,
        side="BUY", order_type="LIMIT", quantity=D("0.01"), price=D("50000"), leverage=2,
        margin_mode="ISOLATED", stop_loss=D("49000"), require_stop=True, risk_budget_usd=D("100"),
    )
    ok, reason = dispatcher.dispatch(intent, MagicMock(), MagicMock())
    assert ok, reason
    await pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side,
                  order_type, quantity, price, filled_qty, status, last_event_version)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0, 'ACCEPTED', 0)""",
        intent.client_order_id, INSTANCE, ACCOUNT, MARGIN, "BUY", "LIMIT", D("0.01"), D("50000"),
    )
    cash_before = dispatcher.cash_balance
    dispatcher.node_cache.quote.return_value = SimpleNamespace(
        bid_price=D("48900"), ask_price=D("50000"), ts_init=1_800_000_000_000_000_000,
    )

    await server.match_resting_orders(pool, engine, SPOT, 48_900, 50_000)

    assert MARGIN not in dispatcher.margin_accounts
    assert intent.client_order_id not in dispatcher.open_orders
    assert dispatcher.cash_balance == cash_before
    assert await pool.fetchval(
        "SELECT status FROM paper_orders WHERE client_order_id=$1", intent.client_order_id,
    ) == "CANCELED"


@pytest.mark.asyncio
async def test_margin_entry_stop_attachment_failure_queues_durable_close(margin_orch_ctx, monkeypatch):
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    dispatcher = engine.dispatcher
    intent = OrderIntent(
        client_order_id="P45-ENTRY-STOP-ATTACH-FAIL", instance_id=INSTANCE, instrument_id=MARGIN,
        side="BUY", order_type="MARKET", quantity=D("0.01"), leverage=2,
        margin_mode="ISOLATED", stop_loss=D("47000"), require_stop=True, risk_budget_usd=D("100"),
    )
    ok, reason = dispatcher.dispatch(intent, MagicMock(), MagicMock())
    assert ok, reason
    dispatcher.open_orders[intent.client_order_id]["attach_stop"] = D("47000")
    monkeypatch.setattr(server, "attach_resting_stop", AsyncMock(return_value=False))

    async with pool.acquire() as conn:
        booked = await server.apply_paper_fill(
            conn, engine, PaperFill(intent.client_order_id, MARGIN, "BUY", D("0.01"), D("50000"), "TAKER"),
            INSTANCE, "MARKET", None, now=datetime(2026, 10, 29, 12, 20, tzinfo=UTC),
        )

    assert booked
    assert engine.dispatcher.margin_accounts[MARGIN].position_qty == D("0.01")
    close = await pool.fetchrow(
        "SELECT command_type, status, payload FROM execution_commands WHERE idempotency_key=$1",
        f"margin-stop-not-restored:{ACCOUNT}:{intent.client_order_id}",
    )
    assert close is not None and (close["command_type"], close["status"]) == ("CLOSE_POSITION", "PENDING")
    assert json.loads(close["payload"])["reason"] == "STOP_NOT_RESTORED"


@pytest.mark.asyncio
async def test_manual_margin_entry_refuses_quote_already_through_structural_stop(margin_orch_ctx):
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    quote_cache = SimpleQuoteCache()
    # The buy fills at the ask above 49,000, but the adverse bid is already through the stop.
    quote_cache.set_quote(SPOT, 48_900, 50_000)
    command_id = uuid.uuid4()
    cash_before = engine.dispatcher.cash_balance
    await pool.execute(
        """INSERT INTO execution_commands
               (command_id, idempotency_key, command_type, target_instance_id, requester_id, payload,
                status, environment, account_id)
           VALUES ($1, $2, 'MANUAL_ORDER', 'MANUAL_TRADE', 'test', $3::jsonb, 'PENDING', 'PAPER', $4)""",
        command_id, f"p45-manual-entry-through-stop:{command_id}",
        json.dumps({"instrument_id": MARGIN, "side": "BUY", "order_type": "MARKET",
                    "quantity": "0.01", "leverage": 2, "stop_loss": "49000"}), ACCOUNT,
    )

    poller = asyncio.create_task(server.command_poller(pool, engine, quote_cache))
    try:
        for _ in range(100):
            state = await pool.fetchrow(
                "SELECT status, error_message FROM execution_commands WHERE command_id=$1", command_id,
            )
            if state["status"] in ("COMPLETED", "REJECTED", "FAILED"):
                break
            await asyncio.sleep(0.02)
    finally:
        poller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poller

    assert state["status"] == "REJECTED" and state["error_message"] == "STOP_REQUIRED"
    assert MARGIN not in engine.dispatcher.margin_accounts
    assert engine.dispatcher.cash_balance == cash_before


@pytest.mark.asyncio
async def test_margin_close_restores_memory_when_bridge_queue_rejects_fill(margin_orch_ctx, monkeypatch):
    await margin_orch_ctx["open_long"]()
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    dispatcher = engine.dispatcher
    margin_id = await pool.fetchval(
        "SELECT id FROM paper_margin_accounts WHERE account_id=$1 AND pair='BTCUSDT' AND status='OPEN'", ACCOUNT,
    )
    account_before = replace(dispatcher.margin_accounts[MARGIN])
    cash_before = dispatcher.cash_balance
    monkeypatch.setattr(engine.bridge, "enqueue_event", lambda _: False)

    closed = await server.force_close_margin_position(
        pool, engine, MARGIN, D("50000"), reason="TEST_QUEUE_FULL", margin_account_id=margin_id,
        now=datetime(2026, 10, 29, 12, 20, tzinfo=UTC),
    )

    assert not closed
    assert dispatcher.margin_accounts[MARGIN].position_qty == account_before.position_qty
    assert dispatcher.cash_balance == cash_before
    assert await pool.fetchval(
        "SELECT status FROM paper_margin_accounts WHERE id=$1", margin_id,
    ) == "OPEN"


@pytest.mark.asyncio
async def test_margin_call_buffer_queues_one_reduce_only_close(margin_orch_ctx):
    await margin_orch_ctx["open_long"]()
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    engine.dispatcher.node_cache.quote.return_value = SimpleNamespace(
        bid_price=D("26800"), ask_price=D("26801"), ts_init=1_800_000_000_000_000_000,
    )
    now = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)

    assert hasattr(server, "margin_risk_monitor_once"), "P4.5 margin-level monitor is not implemented"
    await server.margin_risk_monitor_once(pool, engine, None, now=now)
    await server.margin_risk_monitor_once(pool, engine, None, now=now)

    close = await pool.fetchrow(
        "SELECT command_type, payload FROM execution_commands WHERE idempotency_key LIKE 'margin-buffer-close:%'"
    )
    assert close is not None and close["command_type"] == "CLOSE_POSITION"
    payload = json.loads(close["payload"])
    assert payload["instrument_id"] == MARGIN
    assert payload["position_side"] == "LONG"
    assert await pool.fetchval(
        "SELECT count(*) FROM execution_commands WHERE idempotency_key LIKE 'margin-buffer-close:%'"
    ) == 1
    assert await pool.fetchval(
        "SELECT count(*) FROM notifications_outbox WHERE kind = 'MARGIN_CALL'"
    ) == 1


@pytest.mark.parametrize(
    ("opener", "side", "initial_stop", "mark", "expected_tighter"),
    [
        ("open_long", "LONG", D("47000"), D("53000"), True),
        ("open_short", "SHORT", D("53000"), D("47000"), True),
    ],
)
@pytest.mark.asyncio
async def test_margin_stops_restore_from_persistence_and_ratchet_on_spot(
    margin_orch_ctx, monkeypatch, opener, side, initial_stop, mark, expected_tighter,
):
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    await margin_orch_ctx[opener]()
    assert await server.attach_resting_stop(pool, engine, MARGIN, initial_stop, INSTANCE)

    restored_dispatcher, _ = make_fresh_dispatcher()
    restarted = SimpleNamespace(account_id=ACCOUNT, dispatcher=restored_dispatcher,
                                kill_switch=restored_dispatcher.kill_switch)
    await server.rehydrate_dispatcher_state(pool, restarted)
    assert await server.load_stop_restores(pool, restarted) == 1
    restored_dispatcher.node_cache.quote.return_value = SimpleNamespace(
        bid_price=mark, ask_price=mark + D("1"), ts_init=1_800_000_000_000_000_000,
    )
    prices = server._fresh_margin_prices(restarted)
    assert prices[SPOT] == mark if side == "LONG" else mark + D("1")
    assert await server.restore_pending_stops(pool, restarted) == 1
    stops = restored_dispatcher.resting_stops(MARGIN, INSTANCE)
    assert len(stops) == 1
    old_price = next(iter(stops.values()))["intent"].stop_price
    assert old_price == initial_stop

    class RedisStub:
        async def hgetall(self, _key):
            return {}

        async def hset(self, *_args, **_kwargs):
            return 1

        async def expire(self, *_args, **_kwargs):
            return True

        async def sismember(self, *_args, **_kwargs):
            return False

        async def delete(self, *_args, **_kwargs):
            return 1

    monkeypatch.setattr(server, "price_tick", AsyncMock(return_value=D("0.01")))
    assert await server.ratchet_resting_stops(pool, restarted, RedisStub(), {SPOT: mark}, now=100.0) == 1
    new_stops = restored_dispatcher.resting_stops(MARGIN, INSTANCE)
    assert len(new_stops) == 1
    new_price = next(iter(new_stops.values()))["intent"].stop_price
    assert (new_price > old_price) if side == "LONG" else (new_price < old_price)


@pytest.mark.parametrize(
    ("opener", "stop_price", "trigger_price", "expected_side"),
    [
        ("open_long", D("47000"), D("46000"), "SELL"),
        ("open_short", D("53000"), D("54000"), "BUY"),
    ],
)
@pytest.mark.asyncio
async def test_margin_resting_stop_fills_on_underlying_spot_and_persists_close(
    margin_orch_ctx, opener, stop_price, trigger_price, expected_side,
):
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    await margin_orch_ctx[opener]()
    assert await server.attach_resting_stop(pool, engine, MARGIN, stop_price, INSTANCE)

    assert await server.fire_resting_stops(pool, engine, {SPOT: trigger_price}) == 1
    assert MARGIN not in engine.dispatcher.margin_accounts
    assert engine.bridge.queue.qsize() == 1
    assert await margin_orch_ctx["worker"].drain_and_commit_batch() == 1
    closed = await pool.fetchrow(
        "SELECT status FROM paper_margin_accounts WHERE account_id = $1 AND pair = 'BTCUSDT'",
        ACCOUNT,
    )
    paper = await pool.fetchrow("SELECT cash_balance FROM paper_accounts WHERE account_id = $1", ACCOUNT)
    assert closed["status"] == "CLOSED"
    assert paper["cash_balance"] == engine.dispatcher.cash_balance
    fill = await pool.fetchrow(
        "SELECT side, instrument_id, order_type FROM paper_orders WHERE account_id = $1 AND status = 'FILLED' "
        "AND order_type = 'STOP_MARKET' ORDER BY updated_at DESC LIMIT 1",
        ACCOUNT,
    )
    assert (fill["side"], fill["instrument_id"]) == (expected_side, MARGIN)


@pytest.mark.parametrize("command_type", ["STOP", "TRIP_KILL_SWITCH"])
@pytest.mark.asyncio
async def test_stop_and_kill_switch_close_margin_via_durable_fill(margin_orch_ctx, command_type):
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    await margin_orch_ctx["open_long"]()
    quote_cache = SimpleQuoteCache()
    quote_cache.set_quote(SPOT, 49_000, 49_001)
    engine.dispatcher.node_cache.quote.return_value = SimpleNamespace(
        bid_price=D("49000"), ask_price=D("49001"), ts_init=1_800_000_000_000_000_000,
    )
    command_id = uuid.uuid4()
    target = None
    payload = {"reason": "test-stop"} if command_type == "TRIP_KILL_SWITCH" else {}
    await pool.execute(
        """INSERT INTO execution_commands
               (command_id, idempotency_key, command_type, target_instance_id, requester_id, payload,
                status, environment, account_id)
           VALUES ($1, $2, $3, $4, 'test', $5::jsonb, 'PENDING', 'PAPER', $6)""",
        command_id, f"p45-{command_type.lower()}:{command_id}", command_type, target,
        json.dumps(payload), ACCOUNT,
    )

    poller = asyncio.create_task(server.command_poller(pool, engine, quote_cache))
    try:
        for _ in range(100):
            status = await pool.fetchval("SELECT status FROM execution_commands WHERE command_id = $1", command_id)
            if status in ("COMPLETED", "REJECTED", "FAILED"):
                break
            await asyncio.sleep(0.05)
    finally:
        poller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poller

    assert status == "COMPLETED"
    if command_type == "TRIP_KILL_SWITCH":
        assert engine.kill_switch.is_latched
    assert MARGIN not in engine.dispatcher.margin_accounts
    assert await margin_orch_ctx["worker"].drain_and_commit_batch() == 1
    row = await pool.fetchrow(
        "SELECT status FROM paper_margin_accounts WHERE account_id = $1 AND pair = 'BTCUSDT'",
        ACCOUNT,
    )
    paper = await pool.fetchrow("SELECT cash_balance FROM paper_accounts WHERE account_id = $1", ACCOUNT)
    assert row["status"] == "CLOSED"
    assert paper["cash_balance"] == engine.dispatcher.cash_balance


@pytest.mark.asyncio
async def test_stop_command_retries_when_margin_close_cannot_be_persisted(margin_orch_ctx, monkeypatch):
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    await margin_orch_ctx["open_long"]()
    quote_cache = SimpleQuoteCache()
    quote_cache.set_quote(SPOT, 49_000, 49_001)
    engine.dispatcher.node_cache.quote.return_value = SimpleNamespace(
        bid_price=D("49000"), ask_price=D("49001"), ts_init=1_800_000_000_000_000_000,
    )
    monkeypatch.setattr(engine.bridge, "enqueue_event", lambda _: False)
    command_id = uuid.uuid4()
    await pool.execute(
        """INSERT INTO execution_commands
               (command_id, idempotency_key, command_type, target_instance_id, requester_id, payload,
                status, environment, account_id)
           VALUES ($1, $2, 'STOP', $3, 'test', '{}'::jsonb, 'PENDING', 'PAPER', $4)""",
        command_id, f"p45-stop-deferred:{command_id}", None, ACCOUNT,
    )

    poller = asyncio.create_task(server.command_poller(pool, engine, quote_cache))
    try:
        for _ in range(100):
            state = await pool.fetchrow(
                "SELECT status, error_message FROM execution_commands WHERE command_id=$1", command_id,
            )
            if state["error_message"] == "MARGIN_STOP_CLOSE_DEFERRED":
                break
            await asyncio.sleep(0.02)
    finally:
        poller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poller

    assert state["status"] == "PENDING"
    assert state["error_message"] == "MARGIN_STOP_CLOSE_DEFERRED"
    assert engine.dispatcher.margin_accounts[MARGIN].position_qty == D("0.01")
    assert await pool.fetchval(
        "SELECT status FROM paper_strategy_instances WHERE instance_id=$1", INSTANCE,
    ) == "RUNNING"


@pytest.mark.asyncio
async def test_deferred_manual_margin_close_keeps_command_pending_for_restart(margin_orch_ctx, monkeypatch):
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    await margin_orch_ctx["open_long"]()
    # Use a non-mandate owner so MANUAL_ORDER exercises the shared dispatcher path directly.
    engine.dispatcher.margin_accounts[MARGIN].instance_id = "MANUAL_TRADE"
    quote_cache = SimpleQuoteCache()
    quote_cache.set_quote(SPOT, 49_000, 49_001)
    engine.dispatcher.node_cache.quote.return_value = SimpleNamespace(
        bid_price=D("49000"), ask_price=D("49001"), ts_init=1_800_000_000_000_000_000,
    )
    monkeypatch.setattr(engine.bridge, "enqueue_event", lambda _: False)
    command_id = uuid.uuid4()
    await pool.execute(
        """INSERT INTO execution_commands
               (command_id, idempotency_key, command_type, target_instance_id, requester_id, payload,
                status, environment, account_id)
           VALUES ($1, $2, 'MANUAL_ORDER', $3, 'test', $4::jsonb, 'PENDING', 'PAPER', $5)""",
        command_id, f"p45-margin-close-pending:{command_id}", "MANUAL_TRADE",
        json.dumps({"instrument_id": MARGIN, "side": "SELL", "order_type": "MARKET",
                    "quantity": "0.01", "reduce_only": True}), ACCOUNT,
    )

    poller = asyncio.create_task(server.command_poller(pool, engine, quote_cache))
    try:
        for _ in range(100):
            state = await pool.fetchrow(
                "SELECT status, error_message FROM execution_commands WHERE command_id=$1", command_id,
            )
            if state["error_message"] == "MARGIN_FILL_DEFERRED":
                break
            await asyncio.sleep(0.02)
    finally:
        poller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poller

    client_oid = f"MANUAL-{str(command_id).replace('-', '')[:12].upper()}"
    assert state["status"] == "PENDING"
    assert state["error_message"] == "MARGIN_FILL_DEFERRED"
    assert client_oid not in engine.dispatcher.open_orders
    assert await pool.fetchval(
        "SELECT count(*) FROM paper_orders WHERE client_order_id=$1", client_oid,
    ) == 0
    assert engine.dispatcher.margin_accounts[MARGIN].position_qty == D("0.01")


@pytest.mark.asyncio
async def test_margin_close_waits_for_quote_when_dispatch_data_is_stale(margin_orch_ctx):
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    await margin_orch_ctx["open_long"]()
    engine.dispatcher.margin_accounts[MARGIN].instance_id = "MANUAL_TRADE"
    engine.dispatcher.ingress_health.is_fresh.return_value = False
    quote_cache = SimpleQuoteCache()  # no underlying quote is available to price a fill
    command_id = uuid.uuid4()
    await pool.execute(
        """INSERT INTO execution_commands
               (command_id, idempotency_key, command_type, target_instance_id, requester_id, payload,
                status, environment, account_id)
           VALUES ($1, $2, 'MANUAL_ORDER', $3, 'test', $4::jsonb, 'PENDING', 'PAPER', $5)""",
        command_id, f"p45-margin-close-stale:{command_id}", "MANUAL_TRADE",
        json.dumps({"instrument_id": MARGIN, "side": "SELL", "order_type": "MARKET",
                    "quantity": "0.01", "reduce_only": True}), ACCOUNT,
    )

    poller = asyncio.create_task(server.command_poller(pool, engine, quote_cache))
    try:
        for _ in range(100):
            state = await pool.fetchrow(
                "SELECT status, error_message FROM execution_commands WHERE command_id=$1", command_id,
            )
            if state["error_message"] == "WAITING_FOR_MARGIN_EXIT_QUOTE":
                break
            await asyncio.sleep(0.02)
    finally:
        poller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poller

    client_oid = f"MANUAL-{str(command_id).replace('-', '')[:12].upper()}"
    assert state["status"] == "PENDING"
    assert state["error_message"] == "WAITING_FOR_MARGIN_EXIT_QUOTE"
    assert client_oid not in engine.dispatcher.open_orders
    assert engine.dispatcher.margin_accounts[MARGIN].position_qty == D("0.01")


@pytest.mark.asyncio
async def test_margin_close_waits_for_fresh_quote_instead_of_rejecting(margin_orch_ctx):
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    await margin_orch_ctx["open_long"]()
    command_id = uuid.uuid4()
    await pool.execute(
        """INSERT INTO execution_commands
               (command_id, idempotency_key, command_type, target_instance_id, requester_id, payload,
                status, environment, account_id)
           VALUES ($1, $2, 'CLOSE_POSITION', $3, 'test', $4::jsonb, 'PENDING', 'PAPER', $5)""",
        command_id, f"p45-stale-close:{command_id}", INSTANCE,
        json.dumps({"instrument_id": MARGIN, "position_side": "LONG", "quantity": "0.01"}), ACCOUNT,
    )
    poller = asyncio.create_task(server.command_poller(pool, engine, SimpleQuoteCache()))
    try:
        for _ in range(100):
            state = await pool.fetchrow(
                "SELECT status, error_message FROM execution_commands WHERE command_id=$1", command_id,
            )
            if state["error_message"] == "WAITING_FOR_FRESH_MARGIN_QUOTE":
                break
            await asyncio.sleep(0.02)
    finally:
        poller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poller

    assert state["status"] == "PENDING"
    assert state["error_message"] == "WAITING_FOR_FRESH_MARGIN_QUOTE"
    assert MARGIN in engine.dispatcher.margin_accounts


@pytest.mark.asyncio
async def test_margin_liquidation_is_once_and_persists_the_fee(margin_orch_ctx):
    await margin_orch_ctx["open_long"]()
    pool, engine, worker = margin_orch_ctx["pool"], margin_orch_ctx["engine"], margin_orch_ctx["worker"]
    dispatcher = engine.dispatcher
    dispatcher.node_cache.quote.return_value = SimpleNamespace(
        bid_price=D("26000"), ask_price=D("26001"), ts_init=1_800_000_000_000_000_000,
    )
    now = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)

    assert hasattr(server, "margin_risk_monitor_once"), "P4.5 margin liquidation monitor is not implemented"
    await server.margin_risk_monitor_once(pool, engine, None, now=now)
    assert MARGIN not in dispatcher.margin_accounts
    assert engine.bridge.queue.qsize() == 1
    assert await worker.drain_and_commit_batch() == 1

    account = await pool.fetchrow(
        "SELECT status, interest_quote FROM paper_margin_accounts WHERE account_id = $1 AND pair = 'BTCUSDT'",
        ACCOUNT,
    )
    paper = await pool.fetchrow("SELECT cash_balance FROM paper_accounts WHERE account_id = $1", ACCOUNT)
    assert account["status"] == "CLOSED"
    assert paper["cash_balance"] == dispatcher.cash_balance
    assert await pool.fetchval(
        "SELECT count(*) FROM paper_execution_events WHERE idempotency_key LIKE 'MARGIN_LIQUIDATION:%'"
    ) == 1

    await server.margin_risk_monitor_once(pool, engine, None, now=now)
    assert engine.bridge.queue.qsize() == 0
    assert await pool.fetchval("SELECT count(*) FROM paper_trades WHERE account_id = $1", ACCOUNT) == 2


@pytest.mark.asyncio
async def test_deferred_liquidation_suppresses_resting_stop_for_same_mark(margin_orch_ctx, monkeypatch):
    await margin_orch_ctx["open_long"]()
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    dispatcher = engine.dispatcher
    assert await server.attach_resting_stop(pool, engine, MARGIN, D("47000"), INSTANCE)
    dispatcher.node_cache.quote.return_value = SimpleNamespace(
        bid_price=D("26000"), ask_price=D("26001"), ts_init=1_800_000_000_000_000_000,
    )
    attempted = []
    monkeypatch.setattr(engine.bridge, "enqueue_event", lambda event: attempted.append(event) or False)

    await server.margin_risk_monitor_once(pool, engine, None, now=datetime(2026, 10, 29, 13, 0, tzinfo=UTC))

    assert len(attempted) == 1
    assert attempted[0]["liquidation"] is True
    assert MARGIN in dispatcher.margin_accounts
    assert any(order.get("instrument_id") == MARGIN for order in dispatcher.open_orders.values())


@pytest.mark.asyncio
async def test_liquidation_crossing_blocks_stop_while_open_snapshot_is_unprojected(margin_orch_ctx, monkeypatch):
    await margin_orch_ctx["open_long"]()
    pool, engine = margin_orch_ctx["pool"], margin_orch_ctx["engine"]
    dispatcher = engine.dispatcher
    assert await server.attach_resting_stop(pool, engine, MARGIN, D("47000"), INSTANCE)
    dispatcher.node_cache.quote.return_value = SimpleNamespace(
        bid_price=D("26000"), ask_price=D("26001"), ts_init=1_800_000_000_000_000_000,
    )
    class ProjectionLagPool:
        """Pool facade that hides the OPEN account id while leaving projection reads intact."""

        def acquire(self):
            return pool.acquire()

        async def fetchval(self, query, *args):
            if "FROM paper_margin_accounts" in query and "status = 'OPEN'" in query:
                return None
            return await pool.fetchval(query, *args)

    lagged_pool = ProjectionLagPool()

    await server.margin_risk_monitor_once(
        lagged_pool, engine, None, now=datetime(2026, 10, 29, 13, 0, tzinfo=UTC),
    )

    assert MARGIN in dispatcher.margin_accounts
    assert engine.bridge.queue.qsize() == 0
    assert any(order.get("instrument_id") == MARGIN for order in dispatcher.open_orders.values())
