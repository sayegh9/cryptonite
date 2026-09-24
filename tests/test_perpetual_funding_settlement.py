"""Perpetual funding settlement: formula, dispatcher application, engine sweep, bridge, orchestrator hook.

cash_flow = -signed_size * mark * rate. Account: 10,000 USDT (make_perp_dispatcher).
"""

import asyncio
import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptonite_execution.bridge import EventBridgeWorker
from cryptonite_execution.engine import FUNDING_CHANNEL, PaperTradingEngine
from cryptonite_execution.margin import calculate_funding_payment
from cryptonite_orchestrator.server import consume_perp_marks
from test_bridge_perpetuals_persistence import (  # noqa: F401 - ctx/db_url are fixtures
    ACCOUNT,
    account,
    ctx,
    db_url,
    fill,
    open_position,
)
from test_orchestrator_perp_wiring import (  # noqa: F401 - redis_bus is a fixture
    publish_until_seen,
    redis_bus,
)
from test_paper_risk_guardrails import PERP, make_perp_dispatcher

D = Decimal
T0 = 1_790_265_600 * 10**9  # a funding timestamp (ns)
HOUR = 3_600 * 10**9


def with_position(signed_qty: str, margin_mode: str = "CROSS"):
    d = make_perp_dispatcher()
    d.set_perp_position(PERP, D(signed_qty), D("50000"), leverage=10, margin_mode=margin_mode)
    return d


def make_engine(dispatcher) -> PaperTradingEngine:
    engine = PaperTradingEngine(
        account_id="PAPER-ACCOUNT-001", trader_id="TRADER-FUNDING", engine_run_id="RUN-FUNDING",
        pg_pool=MagicMock(), redis_client=AsyncMock(),
    )
    engine.dispatcher = dispatcher
    return engine


# --- Formula -----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("size", "rate", "expected"),
    [
        ("0.04", "0.0001", "-0.2"),  # positive rate: long pays
        ("-0.04", "0.0001", "0.2"),  # positive rate: short receives
        ("0.04", "-0.0001", "0.2"),  # negative rate: long receives
        ("-0.04", "-0.0001", "-0.2"),  # negative rate: short pays
        ("0", "0.0001", "0"),
    ],
)
def test_funding_payment_formula(size, rate, expected):
    assert calculate_funding_payment(D(size), D("50000"), D(rate)) == D(expected)


# --- Dispatcher application -----------------------------------------------------------

def test_long_pays_short_when_rate_positive():
    long_d, short_d = with_position("0.04"), with_position("-0.04")
    (p_long,) = long_d.apply_funding_settlement(PERP, D("50000"), D("0.0001"), T0)
    (p_short,) = short_d.apply_funding_settlement(PERP, D("50000"), D("0.0001"), T0)
    assert (p_long.cash_flow, p_short.cash_flow) == (D("-0.2"), D("0.2"))
    assert long_d.cash_balance == D("9999.8") and short_d.cash_balance == D("10000.2")


def test_short_pays_long_when_rate_negative():
    long_d, short_d = with_position("0.04"), with_position("-0.04")
    long_d.apply_funding_settlement(PERP, D("50000"), D("-0.00025"), T0)
    short_d.apply_funding_settlement(PERP, D("50000"), D("-0.00025"), T0)
    assert long_d.cash_balance == D("10000.5") and short_d.cash_balance == D("9999.5")


def test_cumulative_funding_equals_sum_of_settlements():
    d = with_position("0.04")
    ticks = [("50000", "0.0001", T0), ("51000", "0.00025", T0 + 8 * HOUR), ("49000", "-0.0003", T0 + 16 * HOUR)]
    flows = [d.apply_funding_settlement(PERP, D(m), D(r), t)[0].cash_flow for m, r, t in ticks]
    assert flows == [D("-0.2"), D("-0.51"), D("0.588")]
    assert d.perp_positions[PERP].cumulative_funding == sum(flows) == D("-0.122")
    assert d.cash_balance == D("10000") + sum(flows)
    assert d.daily_realized_pnl == sum(flows)


def test_settlement_is_idempotent_and_monotonic_per_instrument():
    d = with_position("0.04")
    assert len(d.apply_funding_settlement(PERP, D("50000"), D("0.0001"), T0)) == 1
    assert d.apply_funding_settlement(PERP, D("50000"), D("0.0001"), T0) == []  # replay
    assert d.apply_funding_settlement(PERP, D("50000"), D("0.0001"), T0 - HOUR) == []  # older
    assert d.cash_balance == D("9999.8")


def test_flat_instrument_pays_nothing():
    d = make_perp_dispatcher()
    assert d.apply_funding_settlement(PERP, D("50000"), D("0.0001"), T0) == []
    assert d.cash_balance == D("10000")


def test_isolated_funding_moves_isolated_margin_not_available_cash():
    d = with_position("0.04", margin_mode="ISOLATED")  # IM 200
    available = d.available_cash
    d.apply_funding_settlement(PERP, D("50000"), D("0.001"), T0)  # -2.0
    pos = d.perp_positions[PERP]
    assert pos.initial_margin == D("198") and d.locked_margin == D("198")
    assert d.cash_balance == D("9998") and d.available_cash == available


# --- Engine: events, notification, liquidation sweep -------------------------------------

async def test_engine_emits_funding_event_and_publishes_summary():
    engine = make_engine(with_position("-0.04"))
    payments, liquidations = await engine.process_funding_settlement(PERP, D("50000"), D("0.0001"), T0)
    assert [p.cash_flow for p in payments] == [D("0.2")] and liquidations == []

    evt = engine.bridge.queue.get_nowait()
    assert evt["event_type"] == "FUNDING_SETTLED" and D(evt["cash_flow"]) == D("0.2")
    assert evt["idempotency_key"] == f"FUNDING:PAPER-ACCOUNT-001:{PERP}:{T0}"
    channel, body = engine.redis.publish.await_args.args
    assert channel == FUNDING_CHANNEL == "channel:perp_funding_events"
    assert json.loads(body)["type"] == "perp.funding"


async def test_funding_debit_that_breaches_maintenance_triggers_liquidation():
    d = with_position("0.04", margin_mode="ISOLATED")  # IM 200, P_liq ~45,180.72
    d.update_mark_price(PERP, D("45300"))
    engine = make_engine(d)
    assert d.check_liquidations(d.fresh_mark_prices()) == []  # safe before funding

    # 5% funding at 45,300: -90.6 -> isolated margin 109.4 -> P_liq ~47,454.8 > mark
    payments, liquidations = await engine.process_funding_settlement(PERP, D("45300"), D("0.05"), T0)
    assert payments[0].cash_flow == D("-90.600")
    assert len(liquidations) == 1 and liquidations[0].liquidation_price > D("47000")
    assert PERP not in d.perp_positions
    assert d.cash_balance == D("9800")  # funding + liquidation never exceed the original 200 margin


# --- Bridge persistence ------------------------------------------------------------------

async def test_funding_persists_once_across_engine_restarts(ctx):  # noqa: F811 - fixture
    await fill(ctx, "S-1", "SELL", "0.04", "50000", leverage=10)
    engine = make_engine(ctx["dispatcher"])
    engine.bridge = ctx["bridge"]
    ctx["dispatcher"].account_id = ACCOUNT
    cash_before = (await account(ctx))["cash_balance"]

    await engine.process_funding_settlement(PERP, D("50000"), D("0.0001"), T0)
    evt = dict(ctx["bridge"].queue.queue[0])
    assert await ctx["worker"].drain_and_commit_batch() == 1

    acct = await account(ctx)
    assert acct["cash_balance"] == cash_before + D("0.2") == ctx["dispatcher"].cash_balance
    assert (await open_position(ctx))["realized_pnl"] == D("0.2")

    # A restarted engine (new run id) replaying the same settlement must not book it twice
    ctx["bridge"].enqueue_event(evt)
    await EventBridgeWorker(ctx["bridge"], ctx["pool"], engine_run_id="RUN-AFTER-RESTART").drain_and_commit_batch()
    assert (await account(ctx))["cash_balance"] == cash_before + D("0.2")
    journal = await ctx["pool"].fetchval(
        "SELECT count(*) FROM paper_execution_events WHERE event_type = 'FUNDING_SETTLED'"
    )
    assert journal == 1


# --- Orchestrator hook -------------------------------------------------------------------

def mark_tick(price: str, rate: str, next_funding_ns: int) -> str:
    return json.dumps({
        "type": "perp.mark", "v": 1, "instrument_id": PERP, "symbol": "BTCUSDT", "ts_event_ns": time.time_ns(),
        "mark_price": price, "index_price": price, "funding_rate": rate, "next_funding_time_ns": next_funding_ns,
    })


async def test_consumer_settles_funding_when_next_funding_time_rolls(redis_bus):  # noqa: F811 - fixture
    d = with_position("0.04")
    engine = make_engine(d)
    task = asyncio.create_task(consume_perp_marks(
        redis_bus["redis"], engine, channel=redis_bus["channel"], key_pattern=redis_bus["key_pattern"],
        liquidation_interval_s=0.05,
    ))
    try:
        await publish_until_seen(redis_bus, mark_tick("50000", "0.0001", T0), engine)
        await publish_until_seen(redis_bus, mark_tick("50000", "0.0001", T0), engine)  # same period: no settlement
        assert d.cash_balance == D("10000")
        # Rollover: settle at T0 with the pre-rollover rate/mark
        await publish_until_seen(redis_bus, mark_tick("50100", "0.0002", T0 + 8 * HOUR), engine)
        deadline = time.monotonic() + 5
        while d.cash_balance == D("10000"):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert d.cash_balance == D("9999.8")  # -0.04 * 50,000 * 0.0001
    assert d.last_funding_time_ns[PERP] == T0
    assert engine.bridge.queue.get_nowait()["event_type"] == "FUNDING_SETTLED"
