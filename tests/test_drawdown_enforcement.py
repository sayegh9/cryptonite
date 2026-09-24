"""Orchestrator risk monitor: daily drawdown circuit breaker, UTC day rollover, DB kill-switch sync.

Account 10,000 USDT with a CROSS long 0.1 BTC @ 50,000 (no liquidation in range):
mark 44,000 -> uPnL -600 -> equity 9,400 = 6% below the 10,000 high-water mark (limit 5%).
"""

import datetime
import json
from decimal import Decimal
from unittest.mock import AsyncMock

from cryptonite_orchestrator.server import (
    DAILY_DRAWDOWN_REASON,
    SYSTEM_ALERTS_CHANNEL,
    risk_monitor_once,
)
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import PERP, perp_intent

D = Decimal
DAY1 = datetime.datetime(2026, 9, 24, 23, 59, tzinfo=datetime.UTC)
DAY2 = datetime.datetime(2026, 9, 25, 0, 0, 1, tzinfo=datetime.UTC)


def setup(mark="50000"):
    engine = make_engine()
    engine.dispatcher.kill_switch = engine.kill_switch
    engine.dispatcher.set_perp_position(PERP, D("0.1"), D("50000"), leverage=10, instance_id="INST-001")
    engine.dispatcher.update_mark_price(PERP, D(mark))
    pool = AsyncMock()
    pool.fetchrow.return_value = None  # no DB latch
    redis = AsyncMock()
    return engine, pool, redis


def executed_sql(pool):
    return [c.args[0] for c in pool.execute.await_args_list]


async def test_drawdown_over_limit_trips_kill_switch_persists_and_alerts_once():
    engine, pool, redis = setup()
    await risk_monitor_once(engine, pool, redis, now=DAY1)  # base: equity 10,000
    assert not engine.kill_switch.is_latched

    engine.dispatcher.update_mark_price(PERP, D("44000"))  # equity 9,400 (-6%)
    await risk_monitor_once(engine, pool, redis, now=DAY1)

    assert engine.kill_switch.is_latched and engine.dispatcher.daily_loss_latched
    pool.fetchval.assert_awaited_once_with(
        "SELECT trip_execution_kill_switch($1, $2, $3, $4)", "PAPER", engine.account_id, DAILY_DRAWDOWN_REASON, "risk-monitor"
    )
    assert any("INSERT INTO paper_risk_events" in s for s in executed_sql(pool))
    channel, body = redis.publish.await_args.args
    alert = json.loads(body)
    assert channel == SYSTEM_ALERTS_CHANNEL and alert["kind"] == DAILY_DRAWDOWN_REASON
    assert D(alert["drawdown_pct"]) == D("0.06")

    # Entries are blocked; a reduce-only exit may still shrink the position
    assert engine.dispatcher.evaluate_intent(perp_intent("BUY", "0.01"))[1] == "KILL_SWITCH_ENGAGED"
    assert engine.dispatcher.evaluate_intent(perp_intent("SELL", "0.1", reduce_only=True))[:2] == (True, "APPROVED")

    await risk_monitor_once(engine, pool, redis, now=DAY1)  # still breached: no second trip or alert
    assert pool.fetchval.await_count == 1 and redis.publish.await_count == 1


async def test_drawdown_under_limit_does_nothing():
    engine, pool, redis = setup()
    await risk_monitor_once(engine, pool, redis, now=DAY1)
    engine.dispatcher.update_mark_price(PERP, D("45100"))  # equity 9,510 (-4.9%)
    await risk_monitor_once(engine, pool, redis, now=DAY1)
    assert not engine.kill_switch.is_latched and not engine.dispatcher.daily_loss_latched
    pool.fetchval.assert_not_awaited()
    redis.publish.assert_not_awaited()


async def test_utc_midnight_rebases_high_water_mark_and_clears_daily_latch():
    engine, pool, redis = setup()
    d = engine.dispatcher
    await risk_monitor_once(engine, pool, redis, now=DAY1)
    d.update_mark_price(PERP, D("44000"))
    await risk_monitor_once(engine, pool, redis, now=DAY1)
    assert d.daily_loss_latched and d.high_water_mark == D("10000")

    await risk_monitor_once(engine, pool, redis, now=DAY2)
    assert not d.daily_loss_latched and d.daily_realized_pnl == 0
    assert d.high_water_mark == D("9400")  # new day's base is the current equity
    assert any("daily_realized_pnl = 0" in s for s in executed_sql(pool))
    assert engine.kill_switch.is_latched  # the kill switch itself still needs an operator reset


async def test_db_latch_from_control_endpoint_stops_the_engine():
    engine, pool, redis = setup()
    pool.fetchrow.return_value = {"is_latched": True, "latched_by": "OPERATOR:alice", "reason": "Manual operator emergency trip via UI"}
    await risk_monitor_once(engine, pool, redis, now=DAY1)
    assert engine.kill_switch.is_latched
    assert engine.dispatcher.evaluate_intent(perp_intent("BUY", "0.01"))[1] == "KILL_SWITCH_ENGAGED"
