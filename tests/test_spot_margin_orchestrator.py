"""P4.5 orchestrator persistence, interest, safety and exit wiring."""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from cryptonite_execution.bridge import EventBridge, EventBridgeWorker
from cryptonite_execution.engine import PaperTradingEngine
from cryptonite_execution.risk import OrderIntent, SpotMarginAccountState
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
            "TRUNCATE paper_execution_events, paper_trades, paper_positions, paper_orders, "
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

    async def open_long():
        intent = OrderIntent(
            client_order_id="P45-OPEN", instance_id=INSTANCE, instrument_id=MARGIN, side="BUY",
            order_type="MARKET", quantity=D("0.01"), leverage=2, margin_mode="ISOLATED",
            stop_loss=D("47000"), require_stop=True,
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
            **dispatcher.fill_event_fields(MARGIN, realized, D("50000")),
        })
        assert await worker.drain_and_commit_batch() == 1
        borrowed_at = datetime(2026, 10, 29, 12, 20, tzinfo=UTC)
        await pool.execute(
            "UPDATE paper_margin_accounts SET opened_at = $3 WHERE account_id = $1 AND pair = $2 AND status = 'OPEN'",
            ACCOUNT, "BTCUSDT", borrowed_at,
        )
        return borrowed_at

    async def open_short():
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
            **dispatcher.fill_event_fields(MARGIN, realized, D("50000")),
        })
        assert await worker.drain_and_commit_batch() == 1
        borrowed_at = datetime(2026, 10, 29, 12, 20, tzinfo=UTC)
        await pool.execute(
            "UPDATE paper_margin_accounts SET opened_at = $3 WHERE account_id = $1 AND pair = $2 AND status = 'OPEN'",
            ACCOUNT, "BTCUSDT", borrowed_at,
        )
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
    ]
    assert [row["principal"] for row in rows] == [D("250")]
    assert [row["hourly_rate"] for row in rows] == [D("0.00000656")]
    assert sum((row["amount"] for row in rows), D("0")) == D("0.001093333333333333")
    assert engine.dispatcher.margin_accounts[MARGIN].interest_quote == sum(
        (row["amount"] for row in rows), D("0")
    )

    restarted_dispatcher, _ = make_fresh_dispatcher()
    restarted = SimpleNamespace(account_id=ACCOUNT, dispatcher=restarted_dispatcher,
                                kill_switch=restarted_dispatcher.kill_switch)
    await server.rehydrate_dispatcher_state(pool, restarted)
    await server.accrue_margin_interest_once(pool, restarted, now=settle)
    assert await pool.fetchval("SELECT count(*) FROM paper_margin_interest") == 1
    assert restarted_dispatcher.margin_accounts[MARGIN].interest_quote == engine.dispatcher.margin_accounts[MARGIN].interest_quote
    second_settlement = datetime(2026, 10, 29, 14, 0, tzinfo=UTC)
    await server.accrue_margin_interest_once(pool, restarted, now=second_settlement)
    rows = await pool.fetch("SELECT period_start, period_end, amount FROM paper_margin_interest ORDER BY period_end")
    assert [(row["period_start"], row["period_end"]) for row in rows] == [
        (borrowed_at, settle), (settle, second_settlement),
    ]
    assert sum((row["amount"] for row in rows), D("0")) == D("0.002733333333333333")


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
        (datetime(2026, 10, 29, 13, 0, tzinfo=UTC), repay_at),
    ]
    assert sum((row["amount"] for row in rows), D("0")) == interest_repaid

    cash_before = dispatcher.cash_balance
    dispatcher.flatten_margin_position(MARGIN, D("50000"), D("0"), "P45-REPAY")
    assert dispatcher.cash_balance == cash_before + D("250") - interest_repaid
    assert MARGIN not in dispatcher.margin_accounts


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
