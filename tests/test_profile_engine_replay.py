"""ADR 0011 P1.6: deterministic scanner-engine-to-paper replay with real SQL and Redis.

Only the debate verdict is supplied locally. The scanner evaluates synthetic closed
bars, and the production gate, routing, command poller, guarded dispatcher, bridge,
exit monitor and notification consumer execute their normal paths. No lake, model,
exchange connection or running application service is needed. Profile scanner worker
publication and preset selection are covered by the P1.2 worker tests.
"""

import asyncio
import importlib
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_execution import profile_lifecycle
from cryptonite_execution.bridge import EventBridge, EventBridgeWorker
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_orchestrator import mandate_monitor, server
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.models import RiskConfig, RuleCondition, RuleGroup, RuleOperator
from cryptonite_scanner_service import exit_monitor as exits
from migrated_db import migrated_database
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_perp_dispatcher

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/debate_worker"))
gate = importlib.import_module("gate")
routing = importlib.import_module("mandate_routing")
BTC = "BTCUSDT.BINANCE"
PERP = "BTCUSDT-PERP.BINANCE"
ETH = "ETHUSDT.BINANCE"
ACCOUNT = "PAPER-ACCOUNT-001"


@pytest.fixture(scope="module")
def replay_database():
    """Migrate an independently named database, including when run in the full suite."""
    with migrated_database("cryptonite_ci_p16_replay") as url:
        yield url


@pytest.fixture
async def replay(replay_database):
    """Reset only this module's database and the configured isolated test Redis DB."""
    h = await make_harness(replay_database)
    await h.pool.execute(
        "TRUNCATE ai_mandates, debate_runs, execution_commands, paper_orders, paper_positions, "
        "paper_trades, paper_execution_events, notifications_outbox CASCADE"
    )
    await h.pool.execute(
        "UPDATE paper_accounts SET starting_balance=10000, cash_balance=10000, total_equity=10000, "
        "realized_pnl=0, daily_realized_pnl=0, daily_loss_latched=false"
    )
    await h.pool.execute("DELETE FROM llm_cost_ledger")
    await h.pool.execute(
        "INSERT INTO system_settings(key,value) VALUES('autotrade.enabled','1') "
        "ON CONFLICT(key) DO UPDATE SET value='1'"
    )
    await h.redis.flushdb()
    profile_lifecycle.reset_switch_cache()
    exits.clear_profile_exit_cache()
    d = make_perp_dispatcher()
    engine = make_engine(d)
    engine.pg_pool = h.pool
    engine.redis = h.redis
    engine.bridge = EventBridge(engine.kill_switch)
    worker = EventBridgeWorker(engine.bridge, h.pool, engine_run_id=f"P16-{uuid.uuid4().hex}")
    quotes = server.SimpleQuoteCache()
    for instrument in (BTC, PERP, ETH):
        quotes.set_quote(instrument, 50000, 50000)
    d.dispatch = MagicMock(wraps=d.dispatch)
    ctx = SimpleNamespace(h=h, pool=h.pool, redis=h.redis, engine=engine, bridge=worker, quotes=quotes)
    await mark(ctx, D("50000"))
    try:
        yield ctx
    finally:
        profile_lifecycle.reset_switch_cache()
        exits.clear_profile_exit_cache()
        await close_harness(h)


async def mark(ctx, price: D) -> None:
    """Supply fresh local mark/quote data to every production price consumer."""
    ctx.engine.dispatcher.update_mark_price(PERP, price)
    await ctx.redis.set("perp:mark:BTCUSDT", json.dumps({
        "mark_price": str(price), "funding_rate": "0", "ts_event_ns": time.time_ns(),
    }))
    ctx.quotes.set_quote(PERP, float(price), float(price))


async def profile(ctx, *, coin=BTC, market="PERP", direction="BOTH", level=3,
                  dry_run=False, start=True, budget=3000):
    """Create stopped profiles and acquire capital/coin locks through lifecycle Start."""
    m = await ctx.h.mandate(level, instruments=(PERP if market == "PERP" and coin == BTC else coin,), budget=budget, status="STOPPED")
    await ctx.pool.execute(
        """UPDATE ai_mandates SET markets=ARRAY[$2]::text[], allow_perps=$3, direction=$4,
           max_leverage=$5, daily_loss_limit_pct=3, dry_run=$6, notify_level='ALL',
           exit_tp_r=2, exit_tp1_scale_pct=100, breaker_max_loss_streak=2 WHERE id=$1""",
        m.id, market, market == "PERP", direction, 3 if market == "PERP" else 1, dry_run,
    )
    async with ctx.pool.acquire() as conn:
        if start:
            return await profile_lifecycle.start(
                conn, m.id, actor="operator:p16", totp_ok=True,
                settings={"autotrade.enabled": 1, "autotrade.max_allocated_pct": 90},
            )
        return await load_mandate(conn, m.id)


def candidate(coin: str, direction: str):
    """Evaluate an actual rule on a closed synthetic bar with known structural levels."""
    end = (time.time_ns() // (3600 * 10**9)) * (3600 * 10**9)
    short = direction == "SHORT"
    bars = pl.DataFrame({
        "ts_event_ns": [end], "bar_open_ns": [end - 3600 * 10**9],
        "bar_close_inclusive_ns": [end - 1], "interval_end_exclusive_ns": [end], "open": [50100. if short else 49900.],
        "high": [50200.], "low": [49800.], "close": [50000.], "volume": [10.],
        "rsi": [50.], "ema_200": [49000.], "atr": [250.],
        "donchian_upper": [50500.], "donchian_lower": [49500.],
    })
    rule = RuleGroup(category="trigger", conditions=[RuleCondition(
        field="close", operator=RuleOperator.LT if short else RuleOperator.GT, value="open",
    )])
    found = ScannerEngine.evaluate(
        bars, [rule], RiskConfig(direction=direction, fallback_target_atr_mult=4),
        coin, "1h", uuid.uuid4(), uuid.uuid4(), price_precision=2,
    )
    assert len(found) == 1
    result = found[0]
    if short:
        assert result.suggested_target < result.suggested_entry < result.suggested_stop
    else:
        assert result.suggested_stop < result.suggested_entry < result.suggested_target
    return result


async def debate(ctx, m, *, direction="SHORT", coin=None):
    """Pass the real pre-debate gate, then supply a deterministic verdict to routing."""
    coin = coin or (ETH if ETH in m.instruments else BTC)
    c = candidate(coin, direction)
    payload = {"instrument_id": coin, "mandate_id": str(m.id), "trigger_source": "scanner",
               "candidate_payload": c.model_dump_json()}
    # A unique scanner window permits each replay step without waiting out production cooldowns.
    await ctx.redis.delete(f"debate:cooldown:{coin}:1h")
    run = uuid.uuid4()
    await ctx.pool.execute(
        "INSERT INTO debate_runs(id,instrument_id,status,trigger_source) VALUES($1,$2,'RUNNING','test')",
        run, coin,
    )
    decision = await gate.pre_debate_gate(
        ctx.pool, ctx.redis, payload, None, "Synthetic closed-bar market brief",
        now_s=time.time(), run_id=run,
    )
    assert decision.allowed, decision.reason
    trend = "BEAR" if direction == "SHORT" else "BULL"
    context = {"trend_4h": trend, "trend_1h": trend, "trend_15m": trend,
               "adx": 30, "plus_di": 10 if direction == "SHORT" else 30,
               "minus_di": 30 if direction == "SHORT" else 10, "atr_pct": .5, "atr_pct_q90": 2}
    async with ctx.pool.acquire() as conn:
        async with conn.transaction():
            status, owner = await routing.route_entry(
                conn, run, coin, "SELL" if direction == "SHORT" else "BUY", .9, 5,
                mandate_id=str(m.id), redis=ctx.redis, now=datetime.now(UTC),
                stop_loss=float(c.suggested_stop), take_profit=float(c.suggested_target),
                reference_price=float(c.suggested_entry), grade_context=context,
            )
    assert owner.id == m.id
    return status, run


async def command_for(ctx, run):
    """Find the command that the real router linked to its stub debate."""
    row = await ctx.pool.fetchrow(
        "SELECT * FROM execution_commands WHERE payload->>'run_id'=$1", str(run),
    )
    assert row is not None
    return row


async def poll(ctx, command_id):
    """Run the production poller until a committed terminal outcome, then stop our task."""
    task = asyncio.create_task(server.command_poller(ctx.pool, ctx.engine, ctx.quotes))
    try:
        async with asyncio.timeout(10):
            while True:
                row = await ctx.pool.fetchrow(
                    "SELECT status, command_outcome, error_message, payload FROM execution_commands WHERE command_id=$1",
                    command_id,
                )
                if row["status"] in {"COMPLETED", "REJECTED", "FAILED"}:
                    return row
                await asyncio.sleep(.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await ctx.bridge.drain_and_commit_batch()


async def enter_short(ctx, m):
    """Execute an AUTO short and verify the SQL ledger and resting protective order."""
    status, run = await debate(ctx, m)
    assert status == "COMPLETED"
    cmd = await command_for(ctx, run)
    settled = await poll(ctx, cmd["command_id"])
    assert settled["status"] == "COMPLETED", dict(settled)
    position = ctx.engine.dispatcher.perp_positions[PERP]
    assert position.signed_qty < 0 and position.instance_id == m.instance_id
    assert position.margin_mode == "ISOLATED"
    assert D("1") <= position.leverage <= D("3")
    assert abs(position.signed_qty) * position.entry_price <= min(
        D("0.25") * D("10000"), m.capital_budget_usd * m.max_position_pct / D("100"),
    )
    ctx.engine.dispatcher.dispatch.assert_called()
    stops = ctx.engine.dispatcher.resting_stops(PERP, m.instance_id)
    assert len(stops) == 1
    stop = next(iter(stops.values()))
    assert stop["reduce_only"] and stop["side"] == "BUY"
    assert stop["intent"].stop_price == D("50500")
    assert stop["quantity"] == abs(position.signed_qty)
    assert await ctx.pool.fetchval(
        "SELECT count(*) FROM paper_positions WHERE instance_id=$1 AND status='OPEN' AND side='SHORT'",
        m.instance_id,
    ) == 1
    return run, cmd["command_id"]


async def exit_at_tp(ctx, m):
    """Load the persisted position/profile exit policy and execute its generated TP close."""
    await mark(ctx, D("49000"))
    async with ctx.pool.acquire() as conn:
        positions = await exits.load_positions(conn, ctx.redis, exits.QuoteBook())
        pos, = [p for p in positions if p.owner_instance == m.instance_id]
        assert pos.profile_tp_r == 2 and pos.profile_tp1_scale_pct == 100
        assert pos.take_profit_price == 49000
        action, = exits.evaluate_exit(pos, best_price=49000, debate_armed=False)
        assert (action.kind, action.reason) == ("REDUCE_TP1", "TAKE_PROFIT_1")
        assert await exits.enqueue_tp1_reduce(conn, pos, action.reason)
    cid = await ctx.pool.fetchval(
        "SELECT command_id FROM execution_commands WHERE target_instance_id=$1 "
        "AND requester_id='risk:exit_monitor' ORDER BY created_at DESC LIMIT 1", m.instance_id,
    )
    settled = await poll(ctx, cid)
    assert settled["status"] == "COMPLETED", dict(settled)
    assert ctx.engine.dispatcher.get_perp_position_qty(PERP) == 0
    assert not ctx.engine.dispatcher.resting_stops(PERP, m.instance_id)
    assert await ctx.pool.fetchval(
        "SELECT count(*) FROM paper_positions WHERE instance_id=$1 AND status='OPEN'", m.instance_id,
    ) == 0
    return cid


async def test_a_auto_short_to_resting_stop_tp_audit_and_notification(replay):
    """P1.6 scenario 1: an AUTO perpetual short survives every real pipeline boundary."""
    ctx = replay
    m = await profile(ctx)
    run, entry_command = await enter_short(ctx, m)
    exit_command = await exit_at_tp(ctx, m)
    rows = await ctx.pool.fetch(
        "SELECT stage,outcome,detail,run_id,command_id FROM ai_profile_decisions WHERE mandate_id=$1 "
        "AND stage IN ('GATE','DEBATE','ROUTE','FILL','EXIT') ORDER BY id", m.id,
    )
    assert [r["stage"] for r in rows] == ["GATE", "DEBATE", "ROUTE", "FILL", "EXIT"]
    assert all(r["run_id"] == run for r in rows[:3])
    assert rows[3]["command_id"] == entry_command
    assert rows[4]["command_id"] == exit_command
    assert [(r["stage"], r["outcome"]) for r in rows[3:]] == [("FILL", "FILLED"), ("EXIT", "TP1")]
    fill, close = [json.loads(r["detail"]) for r in rows[3:]]
    assert (fill["side"], fill["market"], fill["mode"]) == ("SHORT", "PERP", "AUTO")
    assert D(close["realized_pnl"]) > 0 and D(close["r_multiple"]) > 0
    async with ctx.pool.acquire() as conn:
        await mandate_monitor.notify_profile_decisions(conn, ctx.redis)
        await mandate_monitor.notify_profile_decisions(conn, ctx.redis)
    kinds = await ctx.pool.fetch("SELECT kind FROM notifications_outbox ORDER BY created_at")
    assert [r["kind"] for r in kinds] == ["PROFILE_FILL", "PROFILE_EXIT"]
    assert await ctx.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0


async def test_b_spot_ask_creates_approval_and_refuses_short(replay):
    """P1.6 scenario 2: LONG ASK produces an approval; SHORT cannot create work."""
    ctx = replay
    m = await profile(ctx, coin=ETH, market="SPOT", direction="LONG", level=1)
    status, run = await debate(ctx, m, direction="LONG")
    assert status == "PENDING_APPROVAL"
    approval = await ctx.pool.fetchrow("SELECT * FROM ai_approvals WHERE source_ref=$1", str(run))
    assert approval["status"] == "PENDING" and approval["mandate_id"] == m.id
    assert json.loads(approval["payload"])["instrument_id"] == ETH
    status, run = await debate(ctx, m, direction="SHORT")
    assert status == "NO_TRADE"
    assert await ctx.pool.fetchval(
        "SELECT reason FROM ai_profile_decisions WHERE run_id=$1 AND stage='ROUTE'", run,
    ) == "DIRECTION_NOT_ALLOWED"
    assert await ctx.pool.fetchval("SELECT count(*) FROM ai_approvals") == 1
    assert await ctx.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
    ctx.engine.dispatcher.dispatch.assert_not_called()


async def test_master_switch_off_turns_auto_perp_entry_into_ask(replay):
    """P1.6 scenario 3: the stored switch downgrades the next AUTO entry to approval."""
    ctx = replay
    m = await profile(ctx)
    await ctx.pool.execute("UPDATE system_settings SET value='0' WHERE key='autotrade.enabled'")
    profile_lifecycle.reset_switch_cache()
    status, run = await debate(ctx, m)
    assert status == "PENDING_APPROVAL"
    assert await ctx.pool.fetchval("SELECT count(*) FROM ai_approvals WHERE source_ref=$1", str(run)) == 1
    assert await ctx.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
    ctx.engine.dispatcher.dispatch.assert_not_called()


async def test_c_dry_run_persists_sizing_plan_without_dispatch(replay):
    """P1.6 scenario 4: a routed dry run completes normal sizing without any order."""
    ctx = replay
    m = await profile(ctx, coin=ETH, market="SPOT", direction="LONG", dry_run=True)
    status, run = await debate(ctx, m, direction="LONG")
    assert status == "COMPLETED"
    cmd = await command_for(ctx, run)
    settled = await poll(ctx, cmd["command_id"])
    assert settled["status"] == "COMPLETED", dict(settled)
    plan = json.loads(settled["payload"])["dry_run_plan"]
    assert D(plan["quantity"]) > 0 and D(plan["entry_price"]) == 50000
    row = await ctx.pool.fetchrow(
        "SELECT outcome,detail FROM ai_profile_decisions WHERE mandate_id=$1 AND stage='ORDER'", m.id,
    )
    assert row["outcome"] == "DRY_RUN" and json.loads(row["detail"])["plan"] == plan
    ctx.engine.dispatcher.dispatch.assert_not_called()
    assert not ctx.engine.dispatcher.open_orders
    assert await ctx.pool.fetchval("SELECT count(*) FROM paper_orders") == 0
    assert await ctx.pool.fetchval("SELECT count(*) FROM paper_positions") == 0


async def test_loss_streak_pauses_profile_but_open_short_still_exits(replay):
    """P1.6 scenario 5: the breaker persists PAUSED while a real TP command still closes."""
    ctx = replay
    m = await profile(ctx)
    await enter_short(ctx, m)
    for i in range(2):
        await ctx.pool.execute(
            """INSERT INTO paper_positions(position_id,account_id,instance_id,instrument_id,side,
               entry_price,quantity,current_price,status,realized_pnl,closed_at)
               VALUES($1,$2,$3,$4,'SHORT',50000,0,50100,'CLOSED',-1,now())""",
            f"p16-loss-{i}", ACCOUNT, m.instance_id, PERP,
        )
    health = {BTC: {"arrival_ts_ns": time.time_ns()}}
    async with ctx.pool.acquire() as conn:
        await mandate_monitor.track_profile_breakers(conn, ctx.redis, m, datetime.now(UTC), health)
        paused = await load_mandate(conn, m.id)
        await mandate_monitor.track_profile_breakers(conn, ctx.redis, paused, datetime.now(UTC), health)
    assert paused.status.value == "PAUSED" and paused.paused_reason.startswith("BREAKER_LOSS_STREAK")
    assert await ctx.pool.fetchval(
        "SELECT count(*) FROM ai_profile_decisions WHERE mandate_id=$1 AND stage='BREAKER'", m.id,
    ) == 1
    assert await ctx.pool.fetchval(
        "SELECT count(*) FROM notifications_outbox WHERE kind='PROFILE_BREAKER'",
    ) == 1
    await exit_at_tp(ctx, paused)
    assert (await load_mandate(ctx.pool, m.id)).status.value == "PAUSED"


async def test_third_profile_cannot_start_on_btc(replay):
    """P1.6 scenario 6: the real lifecycle transaction preserves the first owner's lock."""
    ctx = replay
    owner = await profile(ctx)
    await profile(ctx, coin=ETH, market="SPOT", direction="LONG", level=1)
    third = await profile(ctx, start=False)
    async with ctx.pool.acquire() as conn:
        with pytest.raises(profile_lifecycle.LifecycleError) as refused:
            await profile_lifecycle.start(conn, third.id, actor="operator:p16", totp_ok=True,
                                         settings={"autotrade.enabled": 1})
    assert refused.value.code == "COIN_LOCKED"
    assert (await load_mandate(ctx.pool, third.id)).status.value == "STOPPED"
    assert await ctx.pool.fetchval("SELECT mandate_id FROM ai_profile_coin_locks WHERE base_asset='BTC'") == owner.id


async def test_two_profiles_cannot_dispatch_over_their_reserved_cash(replay):
    """P1.6 scenario 7: durable cash claims independently refuse both profiles before dispatch."""
    ctx = replay
    profiles = [await profile(ctx, market="SPOT", direction="LONG"),
                await profile(ctx, coin=ETH, market="SPOT", direction="LONG")]
    for m, coin in zip(profiles, (BTC, ETH), strict=True):
        claim = uuid.uuid4()
        await ctx.pool.execute(
            """INSERT INTO execution_commands(command_id,idempotency_key,command_type,mandate_id,payload,status)
               VALUES($1,$2,'SUBMIT_ORDER',$3,'{}','COMPLETED')""", claim, str(claim), m.id,
        )
        oid = f"p16-reserved-{claim.hex}"
        await ctx.pool.execute(
            """INSERT INTO trading_orders(environment,account_id,client_order_id,strategy_id,instrument_id,
               side,order_type,quantity,price,status) VALUES('PAPER',$1,$2,$3,$4,'BUY','LIMIT',.058,50000,'SUBMITTED')""",
            ACCOUNT, oid, m.instance_id, coin,
        )
        await ctx.pool.execute(
            """INSERT INTO execution_reservations(command_id,client_order_id,environment,account_id,asset,
               amount,fee_slippage_headroom,state) VALUES($1,$2,'PAPER',$3,'USDT',2899,1,'PRE_SEND')""",
            claim, oid, ACCOUNT,
        )
        status, run = await debate(ctx, m, direction="LONG", coin=coin)
        assert status == "COMPLETED"
        cmd = await command_for(ctx, run)
        settled = await poll(ctx, cmd["command_id"])
        assert settled["status"] == "REJECTED", dict(settled)
        assert settled["error_message"] == "PROFILE_CASH_EXHAUSTED"
        assert await ctx.pool.fetchval(
            "SELECT count(*) FROM execution_reservations WHERE command_id=$1 AND state='PRE_SEND'",
            claim,
        ) == 1
    ctx.engine.dispatcher.dispatch.assert_not_called()
    assert await ctx.pool.fetchval("SELECT count(*) FROM paper_orders") == 0
    assert await ctx.pool.fetchval("SELECT count(*) FROM ai_autonomy_events WHERE kind='BREACH'") == 0
