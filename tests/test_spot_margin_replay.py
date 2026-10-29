"""P4.8 deterministic replays through profile routing and isolated paper margin.

These tests use migrated throwaway Postgres databases, the isolated test Redis DB,
the real mandate router and command poller, and the guarded paper dispatcher. The
debate verdict is deterministic input; no exchange or model service is contacted.
"""

import asyncio
import json
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_execution import profile_lifecycle
from cryptonite_execution.bridge import EventBridge, EventBridgeWorker
from cryptonite_orchestrator import server
from cryptonite_orchestrator.server import SimpleQuoteCache
from cryptonite_scanner_service import exit_monitor as exits
from migrated_db import migrated_database
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_perp_dispatcher

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/debate_worker"))
import mandate_routing as routing  # noqa: E402

ACCOUNT = "PAPER-ACCOUNT-001"
BTC_SPOT = "BTCUSDT.BINANCE"
BTC_MARGIN = "BTCUSDT-MARGIN.BINANCE"
ETH_SPOT = "ETHUSDT.BINANCE"
ETH_MARGIN = "ETHUSDT-MARGIN.BINANCE"
@pytest.fixture(scope="module")
def replay_database():
    """Create one named, migrated database for this module's isolated replays."""
    with migrated_database("cryptonite_ci_p48_replay") as url:
        yield url


@pytest.fixture
async def replay(replay_database):
    """Reset a throwaway database and test Redis before each full-path replay."""
    h = await make_harness(replay_database)
    await h.pool.execute(
        "TRUNCATE ai_mandates, debate_runs, execution_commands, ai_approvals, ai_profile_decisions, "
        "paper_orders, paper_positions, paper_trades, paper_execution_events, paper_margin_interest, "
        "paper_margin_accounts, notifications_outbox CASCADE"
    )
    await h.pool.execute(
        "UPDATE paper_accounts SET starting_balance=10000, cash_balance=10000, total_equity=10000, "
        "realized_pnl=0, daily_realized_pnl=0, daily_loss_latched=false"
    )
    await h.pool.execute("DELETE FROM llm_cost_ledger")
    await h.pool.execute(
        "INSERT INTO system_settings(key,value) VALUES('autotrade.enabled','1'),"
        "('autotrade.max_allocated_pct','90') ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"
    )
    await h.redis.flushdb()
    profile_lifecycle.reset_switch_cache()
    exits.clear_profile_exit_cache()

    dispatcher = make_perp_dispatcher()
    quotes = SimpleQuoteCache()
    dispatcher.set_node_cache(quotes)
    dispatcher.dispatch = MagicMock(wraps=dispatcher.dispatch)
    engine = make_engine(dispatcher)
    engine.pg_pool = h.pool
    engine.redis = h.redis
    engine.bridge = EventBridge(dispatcher.kill_switch)
    worker = EventBridgeWorker(engine.bridge, h.pool, engine_run_id=f"P48-{uuid.uuid4().hex}")
    ctx = SimpleNamespace(h=h, pool=h.pool, redis=h.redis, engine=engine, bridge=worker, quotes=quotes)
    for instrument in (BTC_SPOT, ETH_SPOT):
        await mark(ctx, instrument, D("50000"))
    try:
        yield ctx
    finally:
        profile_lifecycle.reset_switch_cache()
        exits.clear_profile_exit_cache()
        await close_harness(h)


async def mark(ctx, spot_instrument: str, price: D, spread: D = D("1")) -> None:
    """Publish the same fresh underlying spot quote to execution and risk consumers."""
    ctx.quotes.set_quote(spot_instrument, float(price), float(price + spread))
    ctx.engine.dispatcher.update_mark_price(spot_instrument, price, time.time_ns())


async def create_profile(ctx, *, spot_instrument: str, market: str, direction: str,
                         level: int = 3, dry_run: bool = False, budget: int = 3000):
    """Create, configure, and lifecycle-start an isolated test profile."""
    m = await ctx.h.mandate(level, instruments=(spot_instrument,), budget=budget, status="STOPPED")
    leverage = 2 if market == "SPOT_MARGIN" else 1
    allowed_instruments = [spot_instrument]
    if market == "SPOT_MARGIN":
        # Routing begins with the spot symbol; execution gates the selected margin alias.
        allowed_instruments.append(spot_instrument.replace(".BINANCE", "-MARGIN.BINANCE"))
    await ctx.pool.execute(
        """UPDATE ai_mandates SET instruments=$2::text[], markets=ARRAY[$3]::text[], allow_perps=false,
           direction=$4, max_leverage=$5, daily_loss_limit_pct=3, dry_run=$6, notify_level='ALL',
           exit_tp_r=2, exit_tp1_scale_pct=100, breaker_max_loss_streak=2 WHERE id=$1""",
        m.id, allowed_instruments, market, direction, leverage, dry_run,
    )
    async with ctx.pool.acquire() as conn:
        started = await profile_lifecycle.start(
            conn, m.id, actor="operator:p48", totp_ok=True,
            settings={"autotrade.enabled": 1, "autotrade.max_allocated_pct": 90},
        )
    return started


async def route_verdict(ctx, mandate, *, spot_instrument: str, side: str,
                        entry: D = D("50000"), stop: D | None = None, target: D | None = None):
    """Submit a deterministic debate verdict to the production mandate routing path."""
    if stop is None:
        stop = entry * (D("1.04") if side == "SELL" else D("0.96"))
    if target is None:
        target = entry * (D("0.90") if side == "SELL" else D("1.10"))
    run_id = uuid.uuid4()
    await ctx.pool.execute(
        "INSERT INTO debate_runs(id,instrument_id,status,trigger_source) VALUES($1,$2,'RUNNING','p48-stub')",
        run_id, spot_instrument,
    )
    async with ctx.pool.acquire() as conn:
        async with conn.transaction():
            status, owner = await routing.route_entry(
                conn, run_id, spot_instrument, side, .9, 5,
                mandate_id=str(mandate.id), redis=ctx.redis, now=datetime.now(UTC),
                stop_loss=float(stop), take_profit=float(target), reference_price=float(entry),
            )
    assert owner is not None and owner.id == mandate.id
    return status, run_id


async def command_for(ctx, run_id):
    row = await ctx.pool.fetchrow(
        "SELECT * FROM execution_commands WHERE payload->>'run_id'=$1 ORDER BY created_at DESC LIMIT 1",
        str(run_id),
    )
    assert row is not None
    return row


async def poll(ctx, command_id):
    """Drive the real command poller until it persists a terminal outcome."""
    task = asyncio.create_task(server.command_poller(ctx.pool, ctx.engine, ctx.quotes))
    try:
        async with asyncio.timeout(12):
            while True:
                row = await ctx.pool.fetchrow(
                    "SELECT status, command_outcome, error_message, payload FROM execution_commands WHERE command_id=$1",
                    command_id,
                )
                if row and row["status"] in {"COMPLETED", "REJECTED", "FAILED"}:
                    return row
                await asyncio.sleep(.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await ctx.bridge.drain_and_commit_batch()


async def execute_verdict(ctx, mandate, *, spot_instrument: str, side: str,
                          entry: D = D("50000"), stop: D | None = None, target: D | None = None):
    status, run_id = await route_verdict(
        ctx, mandate, spot_instrument=spot_instrument, side=side, entry=entry, stop=stop, target=target,
    )
    assert status == "COMPLETED", await ctx.pool.fetchrow(
        "SELECT outcome,reason,detail FROM ai_profile_decisions WHERE run_id=$1 AND stage='ROUTE' ORDER BY id DESC LIMIT 1",
        run_id,
    )
    command = await command_for(ctx, run_id)
    result = await poll(ctx, command["command_id"])
    if result["status"] != "COMPLETED":
        route = await ctx.pool.fetchrow(
            "SELECT outcome,reason FROM ai_profile_decisions WHERE run_id=$1 AND stage='ROUTE' ORDER BY id DESC LIMIT 1",
            run_id,
        )
        raise AssertionError(f"command did not complete: {dict(result)}; route={dict(route) if route else None}")
    return run_id, command, result


async def open_margin(ctx, *, direction: str, pair_spot: str = BTC_SPOT, budget: int = 3000,
                      dry_run: bool = False):
    market = "SPOT_MARGIN"
    mandate = await create_profile(ctx, spot_instrument=pair_spot, market=market,
                                   direction=direction, dry_run=dry_run, budget=budget)
    side = "SELL" if direction == "SHORT" else "BUY"
    run, command, result = await execute_verdict(ctx, mandate, spot_instrument=pair_spot, side=side)
    return mandate, run, command, result


async def test_auto_margin_short_interest_ratchet_and_tp_repay(replay, monkeypatch):
    """A routed AUTO short accrues exactly, trails, then closes through TP with debt repaid."""
    ctx = replay
    await mark(ctx, BTC_SPOT, D("50000"))
    mandate, _, _, _ = await open_margin(ctx, direction="SHORT")
    account = ctx.engine.dispatcher.margin_accounts[BTC_MARGIN]
    assert account.side == "SHORT" and account.instance_id == mandate.instance_id
    assert account.borrowed_base > 0 and account.borrowed_quote == 0
    stops = ctx.engine.dispatcher.resting_stops(BTC_MARGIN, mandate.instance_id)
    assert len(stops) == 1
    original_stop = next(iter(stops.values()))["intent"].stop_price

    borrowed_at = datetime(2026, 10, 29, 12, 20, tzinfo=UTC)
    await ctx.pool.execute(
        "UPDATE paper_margin_accounts SET opened_at=$2 WHERE account_id=$1 AND pair='BTCUSDT' AND status='OPEN'",
        ACCOUNT, borrowed_at,
    )
    await server.accrue_margin_interest_once(ctx.pool, ctx.engine,
                                             now=datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
    ledger = await ctx.pool.fetch(
        "SELECT period_start,period_end,asset,principal,hourly_rate,amount FROM paper_margin_interest "
        "ORDER BY period_end,asset"
    )
    base_rows = [row for row in ledger if row["asset"] == "BTC"]
    assert [(r["period_start"], r["period_end"]) for r in base_rows] == [
        (borrowed_at, datetime(2026, 10, 29, 13, 0, tzinfo=UTC)),
        (datetime(2026, 10, 29, 13, 0, tzinfo=UTC), datetime(2026, 10, 29, 14, 0, tzinfo=UTC)),
    ]
    assert sum((r["amount"] for r in base_rows), D("0")) == account.interest_base

    monkeypatch.setattr(server, "price_tick", lambda *args, **kwargs: asyncio.sleep(0, result=D("1")))
    await mark(ctx, BTC_SPOT, D("48000"))
    moved = await server.ratchet_resting_stops(
        ctx.pool, ctx.engine, ctx.redis, {BTC_SPOT: D("48000")}, now=1000,
    )
    trailed = ctx.engine.dispatcher.resting_stops(BTC_MARGIN, mandate.instance_id)
    assert moved == 1 and len(trailed) == 1
    new_stop = next(iter(trailed.values()))["intent"].stop_price
    assert new_stop < original_stop and new_stop > D("48000")

    # Exercise the ordinary exit monitor: it derives TP from persisted profile trade data
    # and enqueues a target-instance CLOSE_POSITION, which P4.5 executes reduce-only.
    from cryptonite_scanner_service import exit_monitor as exit_service

    quote_book = exit_service.QuoteBook()
    quote_book.apply({"instrument_id": BTC_SPOT, "bid_price": 45000, "ask_price": 45001})
    async with ctx.pool.acquire() as conn:
        positions = await exit_service.load_positions(conn, ctx.redis, quote_book)
        position, = [p for p in positions if p.owner_instance == mandate.instance_id]
        assert position.instrument_id == BTC_MARGIN
        action, = exit_service.evaluate_exit(position, best_price=45000, debate_armed=False)
        assert action.kind == "REDUCE_TP1" and action.reason == "TAKE_PROFIT_1"
        assert await exit_service.enqueue_tp1_reduce(conn, position, action.reason)
    close_id = await ctx.pool.fetchval(
        "SELECT command_id FROM execution_commands WHERE target_instance_id=$1 "
        "AND requester_id='risk:exit_monitor' ORDER BY created_at DESC LIMIT 1", mandate.instance_id,
    )
    await mark(ctx, BTC_SPOT, D("45000"))
    closed = await poll(ctx, close_id)
    assert closed["status"] == "COMPLETED", dict(closed)
    await ctx.bridge.drain_and_commit_batch()
    assert BTC_MARGIN not in ctx.engine.dispatcher.margin_accounts
    stored = await ctx.pool.fetchrow(
        "SELECT status,borrowed_base,borrowed_quote FROM paper_margin_accounts "
        "WHERE account_id=$1 AND pair='BTCUSDT' ORDER BY id DESC LIMIT 1", ACCOUNT,
    )
    assert stored["status"] == "CLOSED"
    assert stored["borrowed_base"] == 0 and stored["borrowed_quote"] == 0


async def test_long_margin_call_then_buffer_close_before_liquidation(replay):
    """A long alerts at call, queues one buffer close, and never reaches liquidation."""
    ctx = replay
    await mark(ctx, BTC_SPOT, D("50000"))
    mandate, _, _, _ = await open_margin(ctx, direction="LONG")
    account = ctx.engine.dispatcher.margin_accounts[BTC_MARGIN]
    assert account.side == "LONG" and account.borrowed_quote > 0
    # A routed profile normally has its structural stop as an earlier loss exit. Remove
    # that resting order here to exercise the distinct account-level call/buffer monitor.
    for cid, info in list(ctx.engine.dispatcher.open_orders.items()):
        if info.get("instrument_id") == BTC_MARGIN and info.get("reduce_only"):
            ctx.engine.dispatcher.open_orders.pop(cid)
            ctx.engine.dispatcher.release_reservation(cid)
    await ctx.pool.execute(
        "UPDATE paper_orders SET status='CANCELED',updated_at=now() WHERE instrument_id=$1 "
        "AND instance_id=$2 AND order_type='STOP_MARKET' AND status='ACCEPTED'",
        BTC_MARGIN, mandate.instance_id,
    )
    borrowed_at = datetime.now(UTC)
    await ctx.pool.execute(
        "UPDATE paper_margin_accounts SET opened_at=$2 WHERE account_id=$1 AND pair='BTCUSDT' AND status='OPEN'",
        ACCOUNT, borrowed_at,
    )

    call_time = borrowed_at + timedelta(seconds=10)
    await mark(ctx, BTC_SPOT, D("27500"))
    alerted = await server.margin_risk_monitor_once(ctx.pool, ctx.engine, ctx.redis, now=call_time)
    assert alerted == 1
    assert await ctx.pool.fetchval("SELECT count(*) FROM notifications_outbox WHERE kind='MARGIN_CALL'") == 1
    assert await ctx.pool.fetchval("SELECT count(*) FROM notifications_outbox WHERE kind='MARGIN_LIQUIDATION'") == 0

    buffer_time = borrowed_at + timedelta(seconds=20)
    await mark(ctx, BTC_SPOT, D("26800"))
    await server.margin_risk_monitor_once(ctx.pool, ctx.engine, ctx.redis, now=buffer_time)
    close = await ctx.pool.fetchrow(
        "SELECT command_id,command_type,target_instance_id,payload FROM execution_commands "
        "WHERE idempotency_key LIKE 'margin-buffer-close:%'"
    )
    assert close is not None and close["command_type"] == "CLOSE_POSITION"
    assert json.loads(close["payload"])["reason"] == "LIQUIDATION_PROXIMITY"
    await server.margin_risk_monitor_once(ctx.pool, ctx.engine, ctx.redis, now=buffer_time)
    assert await ctx.pool.fetchval(
        "SELECT count(*) FROM execution_commands WHERE idempotency_key LIKE 'margin-buffer-close:%'"
    ) == 1
    closed = await poll(ctx, close["command_id"])
    assert closed["status"] == "COMPLETED", dict(closed)
    assert await ctx.pool.fetchval("SELECT count(*) FROM notifications_outbox WHERE kind='MARGIN_LIQUIDATION'") == 0
    account_row = await ctx.pool.fetchrow(
        "SELECT status FROM paper_margin_accounts WHERE account_id=$1 AND pair='BTCUSDT' ORDER BY id DESC LIMIT 1",
        ACCOUNT,
    )
    assert account_row["status"] == "CLOSED"
    assert mandate.instance_id == close["target_instance_id"]


async def test_margin_dry_run_has_plan_but_never_borrows(replay):
    """A profile dry run records the exact order plan without dispatch or margin rows."""
    ctx = replay
    await mark(ctx, BTC_SPOT, D("50000"))
    _, run, command, result = await open_margin(ctx, direction="SHORT", dry_run=True)
    payload = json.loads(result["payload"])
    plan = payload["dry_run_plan"]
    assert D(plan["quantity"]) > 0 and D(plan["entry_price"]) == D("50000")
    outcome = await ctx.pool.fetchrow(
        "SELECT outcome,detail FROM ai_profile_decisions WHERE mandate_id=$1 AND stage='ORDER' ORDER BY id DESC LIMIT 1",
        (await ctx.pool.fetchval("SELECT mandate_id FROM execution_commands WHERE command_id=$1", command["command_id"])),
    )
    assert outcome["outcome"] == "DRY_RUN"
    assert json.loads(outcome["detail"])["plan"] == plan
    assert await ctx.pool.fetchval("SELECT count(*) FROM paper_margin_accounts") == 0
    assert await ctx.pool.fetchval("SELECT count(*) FROM paper_margin_interest") == 0
    assert not ctx.engine.dispatcher.margin_accounts and not ctx.engine.dispatcher.open_orders
    assert ctx.engine.dispatcher.dispatch.call_count == 0
    assert await ctx.pool.fetchval("SELECT count(*) FROM execution_commands WHERE payload->>'run_id'=$1", str(run)) == 1


async def test_switch_off_routes_margin_add_risk_to_approval(replay):
    """The stored master switch downgrades an AUTO margin short to a pending approval."""
    ctx = replay
    await mark(ctx, BTC_SPOT, D("50000"))
    mandate = await create_profile(ctx, spot_instrument=BTC_SPOT, market="SPOT_MARGIN", direction="SHORT")
    await ctx.pool.execute("UPDATE system_settings SET value='0' WHERE key='autotrade.enabled'")
    profile_lifecycle.reset_switch_cache()
    status, run = await route_verdict(ctx, mandate, spot_instrument=BTC_SPOT, side="SELL")
    assert status == "PENDING_APPROVAL"
    approval = await ctx.pool.fetchrow("SELECT status,action_class,payload FROM ai_approvals WHERE source_ref=$1", str(run))
    assert approval["status"] == "PENDING" and approval["action_class"] == "ADD_RISK"
    assert await ctx.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
    assert await ctx.pool.fetchval("SELECT count(*) FROM paper_margin_accounts") == 0
    assert ctx.engine.dispatcher.dispatch.call_count == 0


async def test_restart_restores_margin_stop_without_duplicate_interest(replay):
    """A new dispatcher restores the persisted protective stop and idempotent interest ledger."""
    ctx = replay
    await mark(ctx, BTC_SPOT, D("50000"))
    mandate, _, _, _ = await open_margin(ctx, direction="SHORT")
    borrowed_at = datetime(2026, 10, 29, 12, 20, tzinfo=UTC)
    await ctx.pool.execute(
        "UPDATE paper_margin_accounts SET opened_at=$2 WHERE account_id=$1 AND pair='BTCUSDT' AND status='OPEN'",
        ACCOUNT, borrowed_at,
    )
    settle = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)
    await server.accrue_margin_interest_once(ctx.pool, ctx.engine, now=settle)
    before = await ctx.pool.fetchval("SELECT count(*) FROM paper_margin_interest")
    assert before > 0

    restarted_dispatcher = make_perp_dispatcher()
    restarted_dispatcher.set_node_cache(ctx.quotes)
    restarted = make_engine(restarted_dispatcher)
    restarted.pg_pool = ctx.pool
    restarted.redis = ctx.redis
    restarted.bridge = EventBridge(restarted_dispatcher.kill_switch)
    await server.rehydrate_dispatcher_state(ctx.pool, restarted)
    assert BTC_MARGIN in restarted_dispatcher.margin_accounts
    assert await server.load_stop_restores(ctx.pool, restarted) == 1
    ctx.quotes.set_quote(BTC_SPOT, 50000, 50001)
    restarted_dispatcher.update_mark_price(BTC_SPOT, D("50000"), time.time_ns())
    assert await server.restore_pending_stops(ctx.pool, restarted) == 1
    restored = restarted_dispatcher.resting_stops(BTC_MARGIN, mandate.instance_id)
    assert len(restored) == 1
    await server.accrue_margin_interest_once(ctx.pool, restarted, now=settle)
    assert await ctx.pool.fetchval("SELECT count(*) FROM paper_margin_interest") == before
    assert restarted_dispatcher.margin_accounts[BTC_MARGIN].interest_base == \
        ctx.engine.dispatcher.margin_accounts[BTC_MARGIN].interest_base


async def test_spot_and_margin_profiles_keep_coin_locks_cash_and_positions_separate(replay):
    """Two owners trade different assets; spot holdings and margin debt stay isolated."""
    ctx = replay
    await mark(ctx, BTC_SPOT, D("50000"))
    await mark(ctx, ETH_SPOT, D("50000"))
    spot = await create_profile(ctx, spot_instrument=ETH_SPOT, market="SPOT", direction="LONG", budget=1500)
    margin = await create_profile(ctx, spot_instrument=BTC_SPOT, market="SPOT_MARGIN", direction="SHORT", budget=3000)

    await execute_verdict(ctx, spot, spot_instrument=ETH_SPOT, side="BUY")
    await execute_verdict(ctx, margin, spot_instrument=BTC_SPOT, side="SELL")

    locks = await ctx.pool.fetch("SELECT mandate_id,base_asset FROM ai_profile_coin_locks ORDER BY base_asset")
    assert {(row["mandate_id"], row["base_asset"]) for row in locks} >= {
        (spot.id, "ETH"), (margin.id, "BTC"),
    }
    assert ctx.engine.dispatcher.get_position_qty(ETH_SPOT) > 0
    assert ETH_MARGIN not in ctx.engine.dispatcher.margin_accounts
    btc_margin = ctx.engine.dispatcher.margin_accounts[BTC_MARGIN]
    assert btc_margin.instance_id == margin.instance_id and btc_margin.side == "SHORT"
    assert BTC_MARGIN not in ctx.engine.dispatcher.positions
    assert ctx.engine.dispatcher.cash_balance > 0
    assert await ctx.pool.fetchval("SELECT cash_balance FROM paper_accounts WHERE account_id=$1", ACCOUNT) == \
        ctx.engine.dispatcher.cash_balance
    persisted = await ctx.pool.fetchrow(
        "SELECT pair,status FROM paper_margin_accounts WHERE account_id=$1 AND pair='BTCUSDT'",
        ACCOUNT,
    )
    assert persisted["status"] == "OPEN"
    assert await ctx.pool.fetchval(
        "SELECT count(*) FROM paper_positions WHERE instance_id=$1 AND instrument_id=$2 AND status='OPEN'",
        spot.instance_id, ETH_SPOT,
    ) == 1
