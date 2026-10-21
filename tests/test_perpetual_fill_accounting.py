"""Dual-directional perpetual fill accounting and direction-aware operational controls.

Account: 10,000 USDT, mark 50,000 (see make_perp_dispatcher). Cash moves only by realized PnL
and fees; initial margin (IM) is locked out of available cash while a position is open.
"""

from decimal import Decimal
from unittest.mock import MagicMock

from cryptonite_execution.engine import PaperTradingEngine
from test_paper_risk_guardrails import PERP, make_perp_dispatcher, perp_intent

D = Decimal


def open_via_dispatch(dispatcher, side, qty, price, cid, leverage=10, fee="0"):
    ok, status = dispatcher.dispatch(perp_intent(side, qty, cid, leverage=leverage), MagicMock(), MagicMock())
    assert ok, status
    dispatcher.handle_fill(cid, D(qty), D(price), D(fee))


def position(dispatcher):
    return dispatcher.perp_positions.get(PERP)


def test_open_short_moves_reservation_into_locked_margin():
    dispatcher = make_perp_dispatcher()
    ok, _ = dispatcher.dispatch(perp_intent("SELL", "0.04", "S-1", leverage=10), MagicMock(), MagicMock())
    assert ok and dispatcher.reserved_cash == D("201")  # IM 200 + USD-M fee estimate 1

    dispatcher.handle_fill("S-1", D("0.04"), D("50000"), D("2"))

    pos = position(dispatcher)
    assert (pos.signed_qty, pos.entry_price, pos.leverage, pos.initial_margin) == (D("-0.04"), D("50000"), 10, D("200"))
    assert dispatcher.reserved_cash == 0
    assert dispatcher.locked_margin == D("200")
    assert dispatcher.cash_balance == D("9998")  # only the fee left the wallet
    assert dispatcher.available_cash == D("9798")


def test_adding_to_short_averages_entry_price():
    dispatcher = make_perp_dispatcher()
    open_via_dispatch(dispatcher, "SELL", "0.02", "50000", "S-1")
    open_via_dispatch(dispatcher, "SELL", "0.02", "52000", "S-2")
    pos = position(dispatcher)
    assert pos.signed_qty == D("-0.04")
    assert pos.entry_price == D("51000")  # (0.02*50000 + 0.02*52000) / 0.04
    assert dispatcher.locked_margin == D("204")  # 100 + 104


def test_profitable_short_close_credits_realized_pnl_and_releases_margin():
    dispatcher = make_perp_dispatcher()
    open_via_dispatch(dispatcher, "SELL", "0.02", "50000", "S-1", fee="1")
    open_via_dispatch(dispatcher, "SELL", "0.02", "52000", "S-2", fee="1.04")
    ok, _ = dispatcher.dispatch(perp_intent("BUY", "0.04", "C-1", reduce_only=True), MagicMock(), MagicMock())
    assert ok
    dispatcher.handle_fill("C-1", D("0.04"), D("49000"), D("1.96"))

    assert position(dispatcher) is None
    assert dispatcher.locked_margin == 0 and dispatcher.reserved_cash == 0
    # realized = 0.04 * (51,000 - 49,000) = 80; fees 1 + 1.04 + 1.96 = 4
    assert dispatcher.cash_balance == D("10000") + D("80") - D("4")
    assert dispatcher.daily_realized_pnl == D("76")


def test_losing_short_close_debits_realized_loss():
    dispatcher = make_perp_dispatcher()
    open_via_dispatch(dispatcher, "SELL", "0.04", "50000", "S-1")
    dispatcher.flatten_perp_position(PERP, D("51000"), D("0"), "C-1")
    assert position(dispatcher) is None
    assert dispatcher.cash_balance == D("10000") - D("40")  # 0.04 * (50,000 - 51,000)


def test_partial_short_reduce_keeps_entry_and_releases_margin_pro_rata():
    dispatcher = make_perp_dispatcher()
    open_via_dispatch(dispatcher, "SELL", "0.04", "50000", "S-1")
    ok, _ = dispatcher.dispatch(perp_intent("BUY", "0.01", "C-1", reduce_only=True), MagicMock(), MagicMock())
    assert ok
    dispatcher.handle_fill("C-1", D("0.01"), D("48000"), D("0"))
    pos = position(dispatcher)
    assert (pos.signed_qty, pos.entry_price) == (D("-0.03"), D("50000"))
    assert pos.initial_margin == D("150") and dispatcher.locked_margin == D("150")
    assert dispatcher.cash_balance == D("10020")  # 0.01 * (50,000 - 48,000)


def test_long_to_short_flip_is_close_leg_then_open_leg():
    dispatcher = make_perp_dispatcher()
    open_via_dispatch(dispatcher, "BUY", "0.02", "50000", "L-1")  # IM 100
    open_via_dispatch(dispatcher, "SELL", "0.05", "51000", "F-1")  # closes 0.02, opens 0.03 short
    pos = position(dispatcher)
    assert (pos.signed_qty, pos.entry_price) == (D("-0.03"), D("51000"))
    assert pos.initial_margin == D("153") and dispatcher.locked_margin == D("153")
    assert dispatcher.cash_balance == D("10020")  # long leg realized 0.02 * (51,000 - 50,000)


def test_short_to_long_flip_is_close_leg_then_open_leg():
    dispatcher = make_perp_dispatcher()
    open_via_dispatch(dispatcher, "SELL", "0.02", "50000", "S-1")
    open_via_dispatch(dispatcher, "BUY", "0.05", "51000", "F-1")
    pos = position(dispatcher)
    assert (pos.signed_qty, pos.entry_price) == (D("0.03"), D("51000"))
    assert dispatcher.locked_margin == D("153")
    assert dispatcher.cash_balance == D("9980")  # short leg realized 0.02 * (50,000 - 51,000)


def test_reduce_only_fill_never_opens_the_opposite_side():
    dispatcher = make_perp_dispatcher()
    dispatcher.set_perp_position(PERP, D("-0.02"), D("50000"), leverage=10)
    dispatcher.open_orders["RO-1"] = {
        "intent": None, "reserved_notional": D("0"), "side": "BUY", "quantity": D("0.02"),
        "instrument_id": PERP, "is_perp": True, "reduce_only": True,
    }
    dispatcher.handle_fill("RO-1", D("0.05"), D("50000"), D("0"))  # venue overfill
    assert position(dispatcher) is None and dispatcher.locked_margin == 0


def test_ledger_conservation_across_mixed_sequence():
    dispatcher = make_perp_dispatcher()
    fills = [  # (side, qty, price, fee)
        ("SELL", "0.02", "50000", "1.0"),
        ("SELL", "0.02", "50500", "1.01"),
        ("BUY", "0.01", "49500", "0.495"),
        ("BUY", "0.06", "49000", "2.94"),  # flip to long 0.03
        ("SELL", "0.01", "49800", "0.498"),
    ]
    for i, (side, qty, price, fee) in enumerate(fills):
        open_via_dispatch(dispatcher, side, qty, price, f"X-{i}", fee=fee)

    pos = position(dispatcher)
    assert (pos.signed_qty, pos.entry_price) == (D("0.02"), D("49000"))
    fees = sum(D(f[3]) for f in fills)
    # short entry 50,250; covers: 0.01 @ 49,500 (+7.5) and 0.03 @ 49,000 (+37.5); long 0.01 @ 49,800 (+8)
    realized = D("7.5") + D("37.5") + D("8")
    assert dispatcher.cash_balance == D("10000") + realized - fees
    assert dispatcher.daily_realized_pnl == realized - fees
    assert dispatcher.locked_margin == pos.initial_margin == D("0.02") * D("49000") / 10
    assert dispatcher.reserved_cash == 0 and dispatcher.open_orders == {}


# --- Direction-aware operational controls (PaperTradingEngine mailbox) ---------------

def make_engine():
    engine = PaperTradingEngine(
        account_id="PAPER-ACCOUNT-001", trader_id="TRADER-PERP-001", engine_run_id="RUN-PERP",
        pg_pool=MagicMock(), redis_client=MagicMock(),
    )
    engine.dispatcher = make_perp_dispatcher()
    engine.dispatcher.kill_switch = engine.kill_switch
    return engine


def run(engine, command_type, **fields):
    engine.mailbox.push({"command_type": command_type, **fields})
    engine.process_mailbox_commands()


def test_kill_switch_reset_refused_while_short_open_then_allowed_after_flatten():
    engine = make_engine()
    engine.dispatcher.set_perp_position(PERP, D("-0.04"), D("50000"), leverage=10)
    run(engine, "TRIP_KILL_SWITCH", payload={"reason": "test"})

    run(engine, "RESET_SWITCH")
    assert engine.kill_switch.is_latched  # a short counts as open exposure

    run(engine, "CLOSE_POSITION", instrument_id=PERP, payload={"price": "49000"})
    assert engine.dispatcher.get_perp_position_qty(PERP) == 0
    assert engine.dispatcher.cash_balance == D("10040")  # closed by reduce-only BUY: 0.04 * (50,000 - 49,000)

    run(engine, "RESET_SWITCH")
    assert not engine.kill_switch.is_latched


def test_pause_cancels_perp_entries_on_both_sides_but_keeps_reduce_only_exits():
    engine = make_engine()
    d = engine.dispatcher
    d.set_perp_position(PERP, D("0.02"), D("50000"), leverage=10)
    for intent in (
        perp_intent("BUY", "0.01", "ENTRY-LONG", leverage=10),
        perp_intent("SELL", "0.01", "ENTRY-SHORT", leverage=10),
        perp_intent("SELL", "0.02", "EXIT-RO", reduce_only=True),
    ):
        ok, status = d.dispatch(intent, MagicMock(), MagicMock())
        assert ok, status

    run(engine, "PAUSE", target_instance_id="INST-001")

    assert set(d.open_orders) == {"EXIT-RO"}
    assert d.reserved_cash == 0  # entry reservations released; the exit reserved nothing


def test_pause_for_another_instance_leaves_orders_alone():
    engine = make_engine()
    ok, _ = engine.dispatcher.dispatch(perp_intent("SELL", "0.01", "ENTRY-SHORT", leverage=10), MagicMock(), MagicMock())
    assert ok
    run(engine, "PAUSE", target_instance_id="SOME-OTHER-INSTANCE")
    assert "ENTRY-SHORT" in engine.dispatcher.open_orders
