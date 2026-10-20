"""Stage 1.4 (leverage plan): paper resting stops. A reduce-only STOP_MARKET rests in the dispatcher, triggers on
the mark and fills at the worse of the stop and the mark (gap aware), before the liquidation check, and needs no
exit monitor."""

import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptonite_orchestrator.server import SimpleQuoteCache, consume_perp_marks, fire_resting_stops
from test_orchestrator_perp_wiring import (
    command_outcomes,
    make_engine,
    mark_tick,
    publish_until_seen,
    redis_bus,  # noqa: F401 - fixture
    run_commands,
)
from test_paper_risk_guardrails import (
    PERP,
    make_fresh_dispatcher,
    make_perp_dispatcher,
    perp_intent,
)

D = Decimal


def long_10x(entry="50000", qty="0.04"):
    """0.04 BTC 10x ISOLATED long from 50,000: IM 200, liquidation ~45,180.72."""
    d = make_perp_dispatcher()
    d.set_perp_position(PERP, D(qty), D(entry), leverage=10, margin_mode="ISOLATED", instance_id="INST-001")
    return d


def stop_intent(side, qty, stop, cid="STOP-1", **flags):
    return perp_intent(side, qty, cid, order_type="STOP_MARKET", stop_price=D(stop), **{"reduce_only": True, **flags})


def test_resting_stop_is_accepted_and_does_not_fill_on_quotes():
    from cryptonite_execution.matching import match_working_orders

    d = long_10x()
    ok, reason = d.dispatch(stop_intent("SELL", "0.04", "48000"), MagicMock(), MagicMock())
    assert ok, reason
    assert d.reserved_cash == 0  # an exit reserves nothing
    assert match_working_orders(d.open_orders, PERP, D("47000"), D("47010")) == []  # quotes never fill a stop


@pytest.mark.parametrize("intent,code", [
    (stop_intent("SELL", "0.04", "48000", reduce_only=False), "STOP_MUST_BE_REDUCE_ONLY"),
    (stop_intent("SELL", "0.04", "50500"), "STOP_WOULD_TRIGGER_IMMEDIATELY"),
    (stop_intent("BUY", "0.04", "52000"), "REDUCE_ONLY_INVALID_DIRECTION"),
    (stop_intent("SELL", "0.05", "48000"), "REDUCE_ONLY_OVERSIZE"),
    (perp_intent("SELL", "0.04", "X", stop_price=D("48000"), reduce_only=True), "STOP_PRICE_ONLY_ON_STOP_MARKET"),
])
def test_invalid_stops_are_refused(intent, code):
    ok, reason, _ = long_10x().evaluate_intent(intent)
    assert not ok and reason.startswith(code)


def test_spot_stop_is_refused_and_spot_orders_unchanged():
    d, _ = make_fresh_dispatcher()
    spot = perp_intent("BUY", "0.01", "S-1", order_type="STOP_MARKET", stop_price=D("49000"))
    spot.instrument_id = "BTCUSDT.BINANCE"
    ok, reason, _ = d.evaluate_intent(spot)
    assert not ok and reason.startswith("SPOT_DERIVATIVE_FLAG_NOT_ALLOWED")
    plain = perp_intent("BUY", "0.01", "S-2")
    plain.instrument_id = "BTCUSDT.BINANCE"
    assert d.evaluate_intent(plain)[:2] == (True, "APPROVED")


def test_stop_does_not_block_other_reduce_only_orders():
    d = long_10x()
    assert d.dispatch(stop_intent("SELL", "0.04", "48000"), MagicMock(), MagicMock())[0]
    # a TP1 half and then the rest can still be placed while the full-size stop rests
    assert d.dispatch(perp_intent("SELL", "0.02", "TP1", order_type="LIMIT", price=D("52000"), reduce_only=True),
                      MagicMock(), MagicMock())[0]
    assert d.evaluate_intent(perp_intent("SELL", "0.02", "R-2", reduce_only=True))[0]


def test_place_resting_stop_replaces_the_owners_previous_stop():
    d = long_10x()
    assert d.place_resting_stop("STOP-A", PERP, D("48000"))[0]
    d.set_perp_position(PERP, D("0.06"), D("50000"), leverage=10, margin_mode="ISOLATED", instance_id="INST-001")
    assert d.place_resting_stop("STOP-B", PERP, D("48500"))[0]
    assert list(d.resting_stops(PERP)) == ["STOP-B"]
    assert d.open_orders["STOP-B"]["quantity"] == D("0.06")


def test_refused_replacement_keeps_the_old_stop():
    d = long_10x()
    assert d.place_resting_stop("STOP-A", PERP, D("48000"))[0]
    ok, reason = d.place_resting_stop("STOP-B", PERP, D("50500"))  # above the mark: would trigger at once
    assert not ok and reason.startswith("STOP_WOULD_TRIGGER_IMMEDIATELY")
    assert list(d.resting_stops(PERP)) == ["STOP-A"]


def test_stop_triggers_on_mark_and_fills_at_the_stop_when_touched():
    d = long_10x()
    d.place_resting_stop("STOP-1", PERP, D("48000"))
    assert d.triggered_stops({PERP: D("48000.01")}) == ([], [])
    fills, _ = d.triggered_stops({PERP: D("48000")})
    assert [(f.side, f.quantity, f.price) for f in fills] == [("SELL", D("0.04"), D("48000"))]


def test_short_stop_fills_at_the_worse_price_on_a_gap_up():
    d = make_perp_dispatcher()
    d.set_perp_position(PERP, D("-0.04"), D("50000"), leverage=10, margin_mode="ISOLATED", instance_id="INST-001")
    d.place_resting_stop("STOP-S", PERP, D("52000"))
    fills, _ = d.triggered_stops({PERP: D("53000")})
    assert [(f.side, f.price) for f in fills] == [("BUY", D("53000"))]


def test_flash_crash_resting_stop_fills_at_the_gap_and_liquidation_does_not_fire_first():
    d = long_10x()
    d.place_resting_stop("STOP-1", PERP, D("48000"))
    _, liq = d._perp_risk_levels(PERP)
    assert D("45000") < liq < D("46000")
    gap = D("46000")  # one tick: through the stop, most of the way to liquidation
    fills, _ = d.triggered_stops({PERP: gap})
    assert [f.price for f in fills] == [gap]
    f = fills[0]
    d.handle_fill(f.client_order_id, f.quantity, f.price, D("0"))
    assert PERP not in d.perp_positions
    assert d.check_liquidations({PERP: gap}) == []
    assert d.cash_balance == D("10000") - D("160")  # 0.04 * (46,000 - 50,000), loss below the 200 margin
    assert d.locked_margin == 0 and d.open_orders == {}


def test_mark_beyond_liquidation_is_left_to_the_liquidation_engine():
    d = long_10x()
    d.place_resting_stop("STOP-1", PERP, D("48000"))
    assert d.triggered_stops({PERP: D("45000")}) == ([], [])
    (liq,) = d.check_liquidations({PERP: D("45000")})
    assert liq.instrument_id == PERP
    assert d.resting_stops(PERP) == {}  # cancelled with the position


def test_stop_outliving_its_position_is_cancelled():
    d = long_10x()
    d.place_resting_stop("STOP-1", PERP, D("48000"))
    d.flatten_perp_position(PERP, D("50000"), D("0"), "CLOSE-1")
    assert d.triggered_stops({PERP: D("47000")}) == ([], ["STOP-1"])
    assert d.open_orders == {}


def test_partial_close_shrinks_what_the_stop_fills():
    d = long_10x()
    d.place_resting_stop("STOP-1", PERP, D("48000"))
    assert d.dispatch(perp_intent("SELL", "0.01", "R-1", reduce_only=True), MagicMock(), MagicMock())[0]
    d.handle_fill("R-1", D("0.01"), D("50000"), D("0"))
    fills, _ = d.triggered_stops({PERP: D("47900")})
    assert [(f.quantity, f.price) for f in fills] == [(D("0.03"), D("47900"))]


async def test_fire_resting_stops_books_the_fill():
    engine = make_engine(long_10x())
    engine.bridge = MagicMock()
    d = engine.dispatcher
    d.place_resting_stop("STOP-1", PERP, D("48000"))
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    assert await fire_resting_stops(pool, engine, {PERP: D("47500")}) == 1
    evt = engine.bridge.enqueue_event.call_args.args[0]
    assert (evt["side"], evt["price"], evt["order_type"], evt["limit_price"]) == ("SELL", "47500", "STOP_MARKET", "48000")
    assert evt["position"] == {"signed_qty": "0"}
    assert PERP not in d.perp_positions


async def test_round_trip_entry_with_stop_then_stop_fires_without_the_exit_monitor(redis_bus):  # noqa: F811
    """A perp entry carrying stop_loss gets a resting stop on fill; the mark loop alone fires it."""
    engine = make_engine()
    d = engine.dispatcher
    quotes = SimpleQuoteCache()
    quotes.set_quote(PERP, 50000.0, 50010.0)
    conn, _ = await run_commands(engine, [
        {"client_order_id": "E-1", "instrument_id": "BTCUSDT-PERP", "side": "BUY", "quantity": "0.04",
         "leverage": 5, "margin_mode": "ISOLATED", "stop_loss": "48500"},
    ], quotes)
    assert command_outcomes(conn) == [("COMPLETED", None)]
    assert d.get_perp_position_qty(PERP) == D("0.04")
    (stop_id,) = d.resting_stops(PERP)
    assert d.open_orders[stop_id]["intent"].stop_price == D("48500")
    stop_rows = [c for c in conn.execute.call_args_list if "STOP_MARKET" in c.args[0]]
    assert stop_rows and stop_rows[0].args[5] == "SELL"

    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = AsyncMock()
    task = asyncio.create_task(consume_perp_marks(
        redis_bus["redis"], engine, channel=redis_bus["channel"], key_pattern=redis_bus["key_pattern"],
        liquidation_interval_s=0.05, pool=pool,
    ))
    try:
        await publish_until_seen(redis_bus, mark_tick("48200"), engine)  # gap through the stop
        deadline = time.monotonic() + 5
        while PERP in d.perp_positions:
            assert time.monotonic() < deadline, "resting stop never fired"
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert d.resting_stops(PERP) == {}
    # filled at the gap price 48,200 (worse than the 48,500 stop): realized 0.04 * (48,200 - 50,010)
    assert d.cash_balance < D("10000") - D("0.04") * (D("50010") - D("48200")) + D("0.01")
