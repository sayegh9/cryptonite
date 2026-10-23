"""A5.1 mandates: the mandate check in front of GuardedOrderDispatcher.

- unit tests for every reject code;
- property tests (hypothesis): no AI order the check accepts escapes its mandate, and the check never
  loosens or changes the dispatcher (the 1–25% clamp is untouched);
- the orchestrator's AI order path: resolved mandate, reject before the dispatcher, mandate instance;
- migration 025: the DB repeats the hard ceilings.
"""

import asyncio
import copy
import json
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import asyncpg
import pytest
from cryptonite_execution.mandates import (
    CONVICTION_MULTIPLIER,
    MandateExposure,
    MandateOrder,
    action_class,
    check_mandate,
    entry_room_usd,
    max_entry_quantity,
    order_notional,
)
from cryptonite_execution.risk import GuardedOrderDispatcher, OrderIntent
from cryptonite_orchestrator.server import SimpleQuoteCache, command_poller
from cryptonite_schemas.mandate import (
    BREACH_CODES,
    ActionClass,
    AutonomyLevel,
    Mandate,
    MandateReject,
    MandateStatus,
    mandate_instance_id,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from migrated_db import migrated_database
from pydantic import ValidationError
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_fresh_dispatcher

D = Decimal
SPOT = "BTCUSDT.BINANCE"
PERP = "BTCUSDT-PERP.BINANCE"


def mandate(**kw) -> Mandate:
    base = dict(id=uuid.UUID(int=1), name="Alt-momentum", capital_budget_usd=D("10000"), instruments=["BTCUSDT", "ETHUSDT"],
                max_position_pct=D("25"), max_drawdown_pct=D("20"))
    base.update(kw)
    return Mandate(**base)


def buy(qty="0.01", price="50000", inst=SPOT, **kw) -> MandateOrder:
    return MandateOrder(instrument_id=inst, side="BUY", quantity=D(qty), ref_price=D(price) if price else None,
                        order_type=kw.pop("order_type", "LIMIT"), **kw)


# ---------------------------------------------------------------------------------------------- unit


def test_contract_repeats_the_hard_ceilings():
    with pytest.raises(ValidationError):
        mandate(max_position_pct=D("26"))
    with pytest.raises(ValidationError):
        mandate(max_drawdown_pct=D("21"))
    with pytest.raises(ValidationError):  # ADR 0009 D2: 10x ceiling (was 5x)
        mandate(max_leverage=11)
    assert mandate(max_leverage=10, allow_perps=True).max_leverage == 10  # leverage above 1x needs perps (ADR 0011)
    with pytest.raises(ValidationError, match="LEVERAGE_NEEDS_MARGIN_OR_PERP"):
        mandate(max_leverage=2)
    for level in (AutonomyLevel.L2, AutonomyLevel.L3):  # ADR 0011 D4: perps at any execution mode (was L1 only)
        assert mandate(allow_perps=True, autonomy_level=level).allow_perps
    assert mandate(allow_perps=True).allow_perps
    assert mandate(allow_perps=True).max_leverage == 3  # D2: enabling perps without a maximum gives 3x
    assert mandate(allow_perps=True, max_leverage=7).max_leverage == 7
    assert mandate().max_leverage == 1
    assert mandate().max_risk_per_trade_pct == D("1")  # D6: 1% default, 2% cap
    with pytest.raises(ValidationError):
        mandate(max_risk_per_trade_pct=D("2.01"))
    assert mandate().instruments == ["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"]
    assert mandate().instance_id == "AI_MANDATE_" + uuid.UUID(int=1).hex


def test_an_ai_order_without_a_mandate_is_rejected():
    assert check_mandate(None, buy(), MandateExposure(), approval_ok=True, autotrade_on=True) is MandateReject.REQUIRED


def test_inside_the_mandate_with_approval_passes():
    assert check_mandate(mandate(), buy(), MandateExposure(), approval_ok=True, autotrade_on=True) is None


def test_spot_market_and_direction_defenses_are_refusals_not_breaches():
    no_spot = mandate(markets=["PERP"])
    reject_market = check_mandate(no_spot, buy(), MandateExposure(), approval_ok=True, autotrade_on=True)
    assert reject_market is MandateReject.MARKET_NOT_ALLOWED
    assert reject_market not in BREACH_CODES

    short_only = mandate(instruments=["BTCUSDT-PERP"], markets=["PERP"], direction="LONG",
                         allow_perps=True, max_leverage=3)
    reject_direction = check_mandate(
        short_only, MandateOrder(PERP, "SELL", D("0.01"), D("50000"), order_type="LIMIT", leverage=2),
        MandateExposure(),
        approval_ok=True, autotrade_on=True,
    )
    assert reject_direction is MandateReject.DIRECTION_NOT_ALLOWED
    assert reject_direction not in BREACH_CODES

    margin = mandate(instruments=["BTCUSDT-MARGIN"], markets=["SPOT_MARGIN"])
    reject_margin = check_mandate(margin, buy(inst="BTCUSDT-MARGIN.BINANCE"), MandateExposure(),
                                  approval_ok=True, autotrade_on=True)
    assert reject_margin is MandateReject.MARKET_NOT_ALLOWED
    assert reject_margin not in BREACH_CODES


@pytest.mark.parametrize("status", [MandateStatus.HALTED, MandateStatus.PAUSED, MandateStatus.ARCHIVED])
def test_a_mandate_that_is_not_active_takes_no_new_risk(status):
    assert check_mandate(mandate(status=status), buy(), MandateExposure(), approval_ok=True, autotrade_on=True) is MandateReject.HALTED


def test_instrument_outside_the_mandate():
    assert check_mandate(mandate(), buy(inst="SOLUSDT.BINANCE", price="150"), MandateExposure(),
                         approval_ok=True, autotrade_on=True) is MandateReject.INSTRUMENT_NOT_ALLOWED


def test_perps_need_the_opt_in_at_l1_and_an_approval():
    m = mandate(instruments=["BTCUSDT-PERP"])
    assert check_mandate(m, buy(inst=PERP), MandateExposure(), approval_ok=True, autotrade_on=True) is MandateReject.PERPS_NOT_ALLOWED
    l3 = mandate(instruments=["BTCUSDT-PERP"], autonomy_level=AutonomyLevel.L3)
    assert check_mandate(l3, buy(inst=PERP), MandateExposure(), approval_ok=True, autotrade_on=True) is MandateReject.PERPS_NOT_ALLOWED
    ok = mandate(instruments=["BTCUSDT-PERP"], allow_perps=True, max_leverage=5)
    assert check_mandate(ok, buy(inst=PERP, leverage=5), MandateExposure(), approval_ok=True, autotrade_on=True) is None
    # L1: every risk-adding perp order needs the operator's approval (D3)
    assert check_mandate(ok, buy(inst=PERP, leverage=5), MandateExposure(), approval_ok=False, autotrade_on=True) \
        is MandateReject.APPROVAL_REQUIRED
    assert check_mandate(ok, buy(inst=PERP, leverage=6), MandateExposure(), approval_ok=True, autotrade_on=True) is MandateReject.LEVERAGE_EXCEEDED
    ten = mandate(instruments=["BTCUSDT-PERP"], allow_perps=True, max_leverage=10)
    assert check_mandate(ten, buy(inst=PERP, leverage=10), MandateExposure(), approval_ok=True, autotrade_on=True) is None
    assert check_mandate(ten, buy(inst=PERP, leverage=11), MandateExposure(), approval_ok=True, autotrade_on=True) \
        is MandateReject.LEVERAGE_EXCEEDED


def test_budget_and_position_limits_count_existing_exposure():
    m = mandate(capital_budget_usd=D("10000"), max_position_pct=D("10"))
    # 0.019 BTC @ 50,000 = 950 <= 1,000 (10% of budget)
    assert check_mandate(m, buy("0.019"), MandateExposure(), approval_ok=True, autotrade_on=True) is None
    assert check_mandate(m, buy("0.021"), MandateExposure(), approval_ok=True, autotrade_on=True) is MandateReject.POSITION_PCT_EXCEEDED
    assert check_mandate(m, buy("0.019"), MandateExposure(instrument_usd=D("100"), gross_usd=D("100")),
                         approval_ok=True, autotrade_on=True) is MandateReject.POSITION_PCT_EXCEEDED
    assert check_mandate(m, buy("0.019"), MandateExposure(gross_usd=D("9100")),
                         approval_ok=True, autotrade_on=True) is MandateReject.BUDGET_EXCEEDED


def test_market_orders_and_conviction_are_priced_worst_case():
    assert order_notional(buy("0.01", order_type="MARKET")) == D("512.5")  # +2.5% collar + slippage
    assert order_notional(buy("0.01", conviction_level="OVERWEIGHT")) == D("750")
    assert order_notional(buy(price=None)) is None
    assert check_mandate(mandate(), buy(price=None), MandateExposure(), approval_ok=True, autotrade_on=True) is MandateReject.NO_REFERENCE_PRICE


def test_entry_room_counts_what_the_mandate_already_holds():
    m = mandate(capital_budget_usd=D("10000"), max_position_pct=D("10"))
    assert entry_room_usd(m, MandateExposure()) == D("1000")
    assert entry_room_usd(m, MandateExposure(instrument_usd=D("800"), gross_usd=D("800"))) == D("200")
    assert entry_room_usd(m, MandateExposure(instrument_usd=D("0"), gross_usd=D("9900"))) == D("100")  # budget binds
    assert entry_room_usd(m, MandateExposure(instrument_usd=D("1200"), gross_usd=D("1200"))) == D("0")


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    held=st.decimals(min_value=0, max_value=3000, places=2),
    other=st.decimals(min_value=0, max_value=9000, places=2),
    price=st.decimals(min_value=1, max_value=200000, places=2),
    pct=st.sampled_from([D("5"), D("10"), D("25")]),
    order_type=st.sampled_from(["MARKET", "LIMIT"]),
    conviction=st.sampled_from([None, "OVERWEIGHT", "UNDERWEIGHT"]),
)
def test_an_entry_sized_to_the_room_is_never_a_breach(held, other, price, pct, order_type, conviction):
    """The incident of 2026-10-04: approved entries on top of an existing position were refused as
    breaches. An entry sized with max_entry_quantity passes the limit checks; a hair more does not."""
    m = mandate(capital_budget_usd=D("10000"), max_position_pct=pct)
    exposure = MandateExposure(gross_usd=held + other, instrument_usd=held)
    qty = max_entry_quantity(m, exposure, price, order_type=order_type, conviction_level=conviction)
    if qty <= 0:
        return
    order = buy(str(qty), str(price), order_type=order_type, conviction_level=conviction)
    assert check_mandate(m, order, exposure, approval_ok=True, autotrade_on=True) is None
    # "a hair more": 0.1%, or two quantity steps when rounding the quantity down to the step already
    # left more than 0.1% of slack (tiny rooms, e.g. $0.02 left at 2,111.71 -> 0.00000923)
    from cryptonite_execution.mandates import QUANTITY_STEP
    over_qty = max(qty * D("1.001"), qty + 2 * QUANTITY_STEP)
    over = buy(str(over_qty), str(price), order_type=order_type, conviction_level=conviction)
    assert check_mandate(m, over, exposure, approval_ok=True, autotrade_on=True) in (MandateReject.POSITION_PCT_EXCEEDED,
                                                                  MandateReject.BUDGET_EXCEEDED)


def test_someone_else_on_the_instrument_is_a_conflict():
    assert check_mandate(mandate(), buy(), MandateExposure(other_owner=True),
                         approval_ok=True, autotrade_on=True) is MandateReject.INSTRUMENT_CONFLICT


@pytest.mark.parametrize("level,approval,expected", [
    (AutonomyLevel.L1, False, MandateReject.APPROVAL_REQUIRED),
    (AutonomyLevel.L2, False, MandateReject.APPROVAL_REQUIRED),
    (AutonomyLevel.L3, False, None),
    (AutonomyLevel.L1, True, None),
])
def test_add_risk_below_l3_needs_an_approval(level, approval, expected):
    assert check_mandate(mandate(autonomy_level=level), buy(), MandateExposure(), approval_ok=approval, autotrade_on=True) is expected


def test_reducing_is_always_allowed_for_what_the_mandate_holds_even_halted():
    sell = MandateOrder(instrument_id=SPOT, side="SELL", quantity=D("0.02"), ref_price=D("50000"))
    assert action_class(sell) is ActionClass.REDUCE_RISK
    halted = mandate(status=MandateStatus.HALTED)
    assert check_mandate(halted, sell, MandateExposure(instrument_qty=D("0.02")), approval_ok=False, autotrade_on=True) is None
    # ...and even outside its instruments (a legacy position) or with no price
    no_px = MandateOrder(instrument_id="SOLUSDT.BINANCE", side="SELL", quantity=D("1"), ref_price=None)
    assert check_mandate(halted, no_px, MandateExposure(instrument_qty=D("1")), approval_ok=False, autotrade_on=True) is None
    # but never more than it holds: that would sell someone else's coins
    assert check_mandate(halted, sell, MandateExposure(instrument_qty=D("0.01"), other_owner=True),
                         approval_ok=False, autotrade_on=True) is MandateReject.POSITION_NOT_OWNED


def test_perp_reduce_must_close_the_mandates_own_side():
    m = mandate(instruments=["BTCUSDT-PERP"], allow_perps=True, max_leverage=5)
    close_long = MandateOrder(instrument_id=PERP, side="SELL", quantity=D("0.01"), ref_price=D("50000"), reduce_only=True)
    exp = MandateExposure(instrument_qty=D("0.01"), instrument_side="LONG")
    assert check_mandate(m, close_long, exp, approval_ok=False, autotrade_on=True) is None
    wrong = MandateOrder(instrument_id=PERP, side="BUY", quantity=D("0.01"), ref_price=D("50000"), reduce_only=True)
    assert check_mandate(m, wrong, exp, approval_ok=False, autotrade_on=True) is MandateReject.POSITION_NOT_OWNED


# --------------------------------------------------------------------------------------- properties

money = st.decimals(min_value=D("0"), max_value=D("200000"), places=2, allow_nan=False, allow_infinity=False)
qty = st.decimals(min_value=D("0.0001"), max_value=D("5"), places=4, allow_nan=False, allow_infinity=False)
price = st.decimals(min_value=D("1"), max_value=D("120000"), places=2, allow_nan=False, allow_infinity=False)
instruments = st.sampled_from([SPOT, "ETHUSDT.BINANCE", "SOLUSDT.BINANCE", PERP, "ETHUSDT-PERP.BINANCE"])


@st.composite
def mandates(draw):
    level = draw(st.sampled_from(list(AutonomyLevel)))
    perps = draw(st.booleans())  # ADR 0011 D4: perps at any level
    return Mandate(
        id=uuid.uuid4(), name="m", capital_budget_usd=draw(st.decimals(min_value=D("100"), max_value=D("100000"), places=2)),
        instruments=draw(st.lists(instruments, min_size=1, max_size=4)),
        max_position_pct=draw(st.decimals(min_value=D("1"), max_value=D("25"), places=1)),
        allow_perps=perps, max_leverage=draw(st.integers(1, 10)) if perps else 1, autonomy_level=level,
        status=draw(st.sampled_from(list(MandateStatus))),
    )


@st.composite
def orders(draw):
    return MandateOrder(
        instrument_id=draw(instruments), side=draw(st.sampled_from(["BUY", "SELL"])), quantity=draw(qty),
        ref_price=draw(st.one_of(st.none(), price)), order_type=draw(st.sampled_from(["MARKET", "LIMIT"])),
        reduce_only=draw(st.booleans()), leverage=draw(st.one_of(st.none(), st.integers(1, 20))),
        conviction_level=draw(st.sampled_from([None, "OVERWEIGHT", "UNDERWEIGHT", "BUY"])),
    )


@st.composite
def exposures(draw):
    inst_usd = draw(money)
    return MandateExposure(gross_usd=inst_usd + draw(money), instrument_usd=inst_usd, instrument_qty=draw(qty),
                           instrument_side=draw(st.sampled_from([None, "LONG", "SHORT"])), other_owner=draw(st.booleans()))


@settings(max_examples=2000, deadline=None)
@given(m=mandates(), order=orders(), exposure=exposures(), approval=st.booleans())
def test_no_accepted_ai_order_escapes_its_mandate(m, order, exposure, approval):
    if check_mandate(m, order, exposure, approval_ok=approval, autotrade_on=True) is not None:
        return
    if action_class(order) is ActionClass.REDUCE_RISK:
        # a reduce never exceeds what the mandate itself holds
        mult = D("1") if order.reduce_only else CONVICTION_MULTIPLIER.get(order.conviction_level or "", D("1"))
        assert order.quantity * mult <= exposure.instrument_qty
        return
    notional = order_notional(order)
    assert m.status is MandateStatus.ACTIVE
    assert m.allows_instrument(order.instrument_id)
    assert not exposure.other_owner
    mult = CONVICTION_MULTIPLIER.get(order.conviction_level or "", D("1"))
    assert notional is not None and notional >= order.quantity * mult * order.ref_price  # what the dispatcher will size
    assert exposure.gross_usd + notional <= m.capital_budget_usd
    assert exposure.instrument_usd + notional <= m.capital_budget_usd * m.max_position_pct / 100
    assert m.max_position_pct <= 25
    if order.is_perp:
        assert m.allow_perps and (order.leverage or 1) <= min(m.max_leverage, 10)
    if m.autonomy_level < AutonomyLevel.L3:
        assert approval


def test_dispatcher_clamp_is_unchanged():
    d, _ = make_fresh_dispatcher()
    assert (d.max_position_concentration_pct, d.min_order_equity_pct) == (D("0.25"), D("0.01"))
    defaults = GuardedOrderDispatcher.__init__.__defaults__
    assert D("0.25") in defaults and D("0.01") in defaults


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(m=mandates(), side=st.sampled_from(["BUY", "SELL"]), q=st.decimals(min_value=D("0.0001"), max_value=D("0.2"), places=4),
       limit=st.one_of(st.none(), st.decimals(min_value=D("45000"), max_value=D("55000"), places=0)),
       held=st.decimals(min_value=D("0"), max_value=D("0.1"), places=3), approval=st.booleans())
def test_the_mandate_check_only_adds_rejections_to_the_dispatcher(m, side, q, limit, held, approval):
    """Whatever the mandate says, the dispatcher decides exactly as it would alone: the mandate check
    runs first, never mutates the intent, and an order it accepts still meets every dispatcher limit."""
    alone, _ = make_fresh_dispatcher()
    piped, _ = make_fresh_dispatcher()
    for d in (alone, piped):
        if held:
            d.positions[SPOT] = held
    intent = OrderIntent(client_order_id="P-1", instance_id=m.instance_id, instrument_id=SPOT, side=side,
                         order_type="LIMIT" if limit else "MARKET", quantity=q, price=limit)
    before = intent.model_copy(deep=True)
    order = MandateOrder(instrument_id=SPOT, side=side, quantity=q, ref_price=limit or (D("50010") if side == "BUY" else D("50000")),
                         order_type=intent.order_type)
    verdict = check_mandate(m, order, MandateExposure(instrument_qty=held), approval_ok=approval, autotrade_on=True)
    assert intent == before  # the mandate check never resizes or edits the order

    ok_alone, reason_alone, _ = alone.evaluate_intent(copy.deepcopy(intent))
    if verdict is None:
        ok_piped, reason_piped, _ = piped.evaluate_intent(copy.deepcopy(intent))
        assert (ok_piped, reason_piped) == (ok_alone, reason_alone)
    # accepted end to end => the dispatcher's own 25% concentration clamp held
    if verdict is None and ok_alone and side == "BUY":
        eff = (limit or D("50010") * D("1.025"))
        assert (held * D("50000") + q * eff) / alone._portfolio_equity() <= D("0.25")


# ---------------------------------------------------------------------------- orchestrator AI path


class FakeConn:
    """Answers the orchestrator's SQL for one claimed command."""

    def __init__(self, command, mandate_row=None, positions=(), approval=None):
        self.queue = [command]
        self.mandate_row = mandate_row
        self.positions = list(positions)
        self.approval = approval
        self.executed: list[tuple] = []
        self.instances: set[str] = set()
        self.events: list[str] = []  # ai_autonomy_events kinds written

    async def fetchrow(self, sql, *args):
        if "WITH claimed" in sql:
            if self.queue:
                return self.queue.pop(0)
            raise asyncio.CancelledError()
        if "FROM ai_mandates" in sql:
            return self.mandate_row
        if "FROM ai_approvals" in sql:
            return self.approval
        return None

    async def fetch(self, sql, *args):
        if "FROM paper_orders" in sql:  # net filled holdings of one instance
            return [{"instrument_id": p["instrument_id"], "net": p["quantity"], "avg_px": p["current_price"]}
                    for p in self.positions if p["instance_id"] == args[0]]
        if "paper_positions" in sql:
            return [p for p in self.positions if p["instance_id"] == args[0]]
        return []

    async def fetchval(self, sql, *args):
        if "FROM paper_positions" in sql:  # someone else holds the instrument?
            return 1 if any(p["instrument_id"] == args[1] and p["instance_id"] != args[2] for p in self.positions) else None
        if "paper_strategy_instances" in sql:
            return 1 if args[0] in self.instances else None
        if "INSERT INTO ai_autonomy_events" in sql:
            self.events.append(args[1])
            return len(self.events)
        return None

    def transaction(self):
        class Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return Tx()

    async def execute(self, sql, *args):
        if "INSERT INTO paper_strategy_instances" in sql:
            self.instances.add(args[0])
        self.executed.append((sql, args))
        return "OK"

    def outcome(self):
        for sql, args in self.executed:
            if "UPDATE execution_commands" in sql:
                return ("REJECTED", args[1]) if "REJECTED" in sql else ("COMPLETED", None)
        return None


def mandate_row(m: Mandate) -> dict:
    return {**m.model_dump(), "autonomy_level": int(m.autonomy_level), "status": m.status.value}


async def run_ai_command(payload, *, mandate_row=None, target="AI_DEBATE_WORKER", requester="operator:u1",
                         cmd_type="SUBMIT_ORDER", mandate_id=None, positions=(), approval=None, dispatcher_position=None):
    d, _ = make_fresh_dispatcher()
    if dispatcher_position is not None:
        d.positions[SPOT] = dispatcher_position
    engine = make_engine(d)
    engine.bridge = MagicMock()
    quotes = SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)
    cid = uuid.uuid4()
    row = {"command_id": cid, "command_type": cmd_type, "target_instance_id": target, "requester_id": requester,
           "mandate_id": mandate_id, "payload": json.dumps(payload)}
    if approval is not None:
        approval = {**approval, "command_id": approval.get("command_id", cid)}
    conn = FakeConn(row, mandate_row, positions, approval)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    with pytest.raises(asyncio.CancelledError):
        await command_poller(pool, engine, quotes)
    return conn, engine


async def test_ai_order_without_a_mandate_never_reaches_the_dispatcher():
    conn, engine = await run_ai_command({"instrument_id": "BTCUSDT", "side": "BUY", "allocation_pct": 5})
    assert conn.outcome() == ("REJECTED", "MANDATE_REQUIRED")
    assert engine.dispatcher.open_orders == {} and engine.dispatcher.reserved_cash == 0


async def test_manual_order_targeting_the_ai_instance_is_an_ai_order():
    conn, _ = await run_ai_command({"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.01"}, cmd_type="MANUAL_ORDER")
    assert conn.outcome() == ("REJECTED", "MANDATE_REQUIRED")


async def test_unknown_mandate_is_rejected():
    conn, _ = await run_ai_command({"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.01"}, mandate_id=uuid.uuid4())
    assert conn.outcome() == ("REJECTED", "MANDATE_NOT_FOUND")


async def test_halted_mandate_rejects_new_risk_before_the_dispatcher():
    m = mandate(status=MandateStatus.HALTED)
    conn, engine = await run_ai_command({"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.01", "mandate_id": str(m.id)},
                                        mandate_row=mandate_row(m))
    assert conn.outcome() == ("REJECTED", "MANDATE_HALTED")
    assert engine.dispatcher.open_orders == {}


async def test_l1_mandate_needs_an_approval_bound_to_this_command():
    m = mandate()
    payload = {"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.01", "mandate_id": str(m.id)}
    conn, _ = await run_ai_command(payload, mandate_row=mandate_row(m))
    assert conn.outcome() == ("REJECTED", "MANDATE_APPROVAL_REQUIRED")

    aid = uuid.uuid4()
    other_cmd = {"mandate_id": m.id, "action_class": "ADD_RISK", "status": "APPROVED", "command_id": uuid.uuid4()}
    conn, _ = await run_ai_command({**payload, "approval_id": str(aid)}, mandate_row=mandate_row(m), approval=other_cmd)
    assert conn.outcome() == ("REJECTED", "MANDATE_APPROVAL_REQUIRED")  # an approval for another command

    vetoed = {"mandate_id": m.id, "action_class": "ADD_RISK", "status": "VETOED"}
    conn, _ = await run_ai_command({**payload, "approval_id": str(aid)}, mandate_row=mandate_row(m), approval=vetoed)
    assert conn.outcome() == ("REJECTED", "MANDATE_APPROVAL_REQUIRED")


async def test_approved_order_is_dispatched_under_the_mandate_instance():
    m = mandate()
    approval = {"mandate_id": m.id, "action_class": "ADD_RISK", "status": "APPROVED"}
    conn, engine = await run_ai_command(
        {"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.01", "mandate_id": str(m.id), "approval_id": str(uuid.uuid4())},
        mandate_row=mandate_row(m), approval=approval,
    )
    assert conn.outcome() == ("COMPLETED", None)
    assert m.instance_id in conn.instances
    evt = engine.bridge.enqueue_event.call_args.args[0]
    assert evt["instance_id"] == m.instance_id and engine.dispatcher.get_position_qty(SPOT) == D("0.01")


async def test_l3_mandate_trades_without_approval_but_inside_its_budget():
    m = mandate(autonomy_level=AutonomyLevel.L3, capital_budget_usd=D("1000"))
    payload = {"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.004", "mandate_id": str(m.id)}  # ~205 USD
    conn, _ = await run_ai_command(payload, mandate_row=mandate_row(m))
    assert conn.outcome() == ("COMPLETED", None)
    conn, engine = await run_ai_command({**payload, "quantity": "0.03"}, mandate_row=mandate_row(m))  # 1,538 > 1,000
    assert conn.outcome() == ("REJECTED", "MANDATE_BUDGET_EXCEEDED")
    assert engine.dispatcher.open_orders == {}
    assert conn.events == ["BREACH"]  # ADR 0011 D3: a breach is recorded and alerted, the mode stays


async def test_approved_entry_on_top_of_a_position_is_sized_to_the_room_not_breached():
    """2026-10-04: the mandate held 8% of its budget in BTC (limit 10%); an approved 5% entry was sized at
    the full 5% and refused as MANDATE_POSITION_PCT_EXCEEDED, recording a BREACH. It is now sized down
    to the 2% that is left and placed."""
    m = mandate(capital_budget_usd=D("10000"), max_position_pct=D("10"))
    held = [{"instance_id": mandate_instance_id(m.id), "instrument_id": SPOT, "side": "LONG",
             "quantity": D("0.016"), "current_price": D("50000")}]  # ~$800
    approval = {"mandate_id": m.id, "action_class": "ADD_RISK", "status": "APPROVED"}
    payload = {"instrument_id": "BTCUSDT", "side": "BUY", "allocation_pct": 5, "mandate_id": str(m.id),
               "approval_id": str(uuid.uuid4())}
    conn, engine = await run_ai_command(payload, mandate_row=mandate_row(m), positions=held, approval=approval)
    assert conn.outcome() == ("COMPLETED", None)
    assert conn.events == []  # no BREACH, no demotion
    qty = engine.dispatcher.get_position_qty(SPOT)
    room = D("1000") - D("0.016") * D("50010")  # valued at the ask, like the mandate check
    assert D("0") < qty * D("50010") * D("1.025") <= room
    assert qty * D("50010") > room * D("0.95")  # it uses the room, minus the 1% sizing margin and the buffer


async def test_a_limit_entry_above_the_ask_is_sized_at_its_limit_price_not_breached():
    """2026-10-13: a LIMIT buy 0.8% above the ask on a mandate already half-way to its position limit. Sizing valued
    the room at the ask, the check valued the order and the holding at the limit price, so the sized-down entry
    was refused as MANDATE_POSITION_PCT_EXCEEDED and recorded a BREACH. Both now use the same price."""
    m = mandate(capital_budget_usd=D("10000"), max_position_pct=D("10"))
    held = [{"instance_id": mandate_instance_id(m.id), "instrument_id": SPOT, "side": "LONG",
             "quantity": D("0.01"), "current_price": D("50000")}]  # ~$500 of a $1,000 limit
    approval = {"mandate_id": m.id, "action_class": "ADD_RISK", "status": "APPROVED"}
    limit = D("50400")  # the ask is 50010
    payload = {"instrument_id": "BTCUSDT", "side": "BUY", "order_type": "LIMIT", "price": str(limit),
               "allocation_pct": 5, "mandate_id": str(m.id), "approval_id": str(uuid.uuid4())}
    conn, engine = await run_ai_command(payload, mandate_row=mandate_row(m), positions=held, approval=approval)
    assert conn.outcome() == ("COMPLETED", None)
    assert conn.events == []  # no BREACH
    qty = D(str(engine.dispatcher.get_position_qty(SPOT)))  # a limit above the ask fills at once
    assert D("0") < qty and qty * limit + D("0.01") * limit <= D("1000")  # inside the limit at the check's price


@pytest.mark.parametrize("held_btc", [D("0"), D("0.005"), D("0.01"), D("0.015")])
@pytest.mark.parametrize("limit_vs_ask", [D("-0.02"), D("0"), D("0.004"), D("0.008"), D("0.03"), D("0.10")])
async def test_a_system_sized_entry_never_breaches_its_mandate(held_btc, limit_vs_ask):
    """Whatever the limit price and the holding, an entry the orchestrator sizes itself is placed or refused as
    POSITION_LIMIT_REACHED; it is never a BREACH (a breach means the AI asked for more than its mandate allows)."""
    m = mandate(capital_budget_usd=D("10000"), max_position_pct=D("10"))
    held = [{"instance_id": mandate_instance_id(m.id), "instrument_id": SPOT, "side": "LONG",
             "quantity": held_btc, "current_price": D("50000")}] if held_btc else []
    approval = {"mandate_id": m.id, "action_class": "ADD_RISK", "status": "APPROVED"}
    payload = {"instrument_id": "BTCUSDT", "side": "BUY", "order_type": "LIMIT",
               "price": str((D("50010") * (1 + limit_vs_ask)).quantize(D("0.01"))),
               "allocation_pct": 5, "mandate_id": str(m.id), "approval_id": str(uuid.uuid4())}
    conn, _ = await run_ai_command(payload, mandate_row=mandate_row(m), positions=held, approval=approval)
    status, reason = conn.outcome()
    assert conn.events == [], reason
    # Placed, or refused for a reason that is not the AI's: no room left, or the dispatcher's price collar
    assert status == "COMPLETED" or reason.startswith(("MANDATE_POSITION_LIMIT_REACHED", "PRICE_COLLAR_REJECT")), reason


async def test_sizing_that_disagrees_with_the_check_is_a_sizing_conflict_never_a_breach(monkeypatch):
    """The backstop: if sizing ever disagrees with the check again, the order is refused as SIZING_CONFLICT (a system
    fault, logged as an error) and the mandate records no BREACH."""
    from cryptonite_orchestrator import mandate_gate

    real = mandate_gate.max_entry_quantity
    monkeypatch.setattr(mandate_gate, "max_entry_quantity", lambda *a, **k: real(*a, **k) * D("3"))
    m = mandate(capital_budget_usd=D("10000"), max_position_pct=D("10"))
    held = [{"instance_id": mandate_instance_id(m.id), "instrument_id": SPOT, "side": "LONG",
             "quantity": D("0.01"), "current_price": D("50000")}]
    approval = {"mandate_id": m.id, "action_class": "ADD_RISK", "status": "APPROVED"}
    payload = {"instrument_id": "BTCUSDT", "side": "BUY", "allocation_pct": 9, "mandate_id": str(m.id),
               "approval_id": str(uuid.uuid4())}
    conn, engine = await run_ai_command(payload, mandate_row=mandate_row(m), positions=held, approval=approval)
    assert conn.outcome() == ("REJECTED", MandateReject.SIZING_CONFLICT.value)
    assert conn.events == [] and engine.dispatcher.open_orders == {}
    assert MandateReject.SIZING_CONFLICT not in BREACH_CODES


async def test_an_ai_quantity_over_the_limit_is_still_a_breach():
    """Only the system's own sizing is exempt: an order that names a quantity over the position limit is the AI
    overstepping, and stays a BREACH."""
    m = mandate(capital_budget_usd=D("10000"), max_position_pct=D("10"))
    approval = {"mandate_id": m.id, "action_class": "ADD_RISK", "status": "APPROVED"}
    payload = {"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.05", "mandate_id": str(m.id),
               "approval_id": str(uuid.uuid4())}  # ~$2,500 against a $1,000 limit
    conn, _ = await run_ai_command(payload, mandate_row=mandate_row(m), approval=approval)
    assert conn.outcome() == ("REJECTED", MandateReject.POSITION_PCT_EXCEEDED.value)
    assert conn.events == ["BREACH"] or "BREACH" in conn.events


async def test_entry_with_no_room_left_is_refused_without_a_breach():
    m = mandate(capital_budget_usd=D("10000"), max_position_pct=D("10"))
    held = [{"instance_id": mandate_instance_id(m.id), "instrument_id": SPOT, "side": "LONG",
             "quantity": D("0.0196"), "current_price": D("50000")}]  # ~$980: $20 left, under the 1% minimum
    approval = {"mandate_id": m.id, "action_class": "ADD_RISK", "status": "APPROVED"}
    payload = {"instrument_id": "BTCUSDT", "side": "BUY", "allocation_pct": 5, "mandate_id": str(m.id),
               "approval_id": str(uuid.uuid4())}
    conn, engine = await run_ai_command(payload, mandate_row=mandate_row(m), positions=held, approval=approval)
    status, reason = conn.outcome()
    assert status == "REJECTED" and reason.startswith("MANDATE_POSITION_LIMIT_REACHED")
    assert conn.events == [] and engine.dispatcher.open_orders == {}
    assert MandateReject.POSITION_LIMIT_REACHED not in BREACH_CODES


async def test_mandate_cannot_take_an_instrument_someone_else_holds():
    m = mandate(autonomy_level=AutonomyLevel.L3)
    manual = [{"instance_id": "MANUAL_TRADE", "instrument_id": SPOT, "side": "LONG", "quantity": D("0.1"), "current_price": D("50000")}]
    conn, _ = await run_ai_command({"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.001", "mandate_id": str(m.id)},
                                   mandate_row=mandate_row(m), positions=manual)
    assert conn.outcome() == ("REJECTED", "MANDATE_INSTRUMENT_CONFLICT")


async def test_mandate_sell_is_capped_at_its_own_position():
    m = mandate(status=MandateStatus.HALTED)
    held = [{"instance_id": mandate_instance_id(m.id), "instrument_id": SPOT, "side": "LONG", "quantity": D("0.01"),
             "current_price": D("50000")},
            {"instance_id": "MANUAL_TRADE", "instrument_id": SPOT, "side": "LONG", "quantity": D("0.5"), "current_price": D("50000")}]
    payload = {"instrument_id": "BTCUSDT", "side": "SELL", "quantity": "0.02", "mandate_id": str(m.id)}
    conn, _ = await run_ai_command(payload, mandate_row=mandate_row(m), positions=held)
    assert conn.outcome() == ("REJECTED", "MANDATE_POSITION_NOT_OWNED")


async def test_manual_orders_are_not_touched_by_mandates():
    conn, engine = await run_ai_command({"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.01"},
                                        cmd_type="MANUAL_ORDER", target="MANUAL_TRADE", requester="web-ui")
    assert conn.outcome() == ("COMPLETED", None)
    assert not any("ai_mandates" in sql for sql, _ in conn.executed)


# ------------------------------------------------------------------------------------- migration 025


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_mandates") as url:
        yield url


async def test_migration_025_enforces_the_ceilings(db_url):
    conn = await asyncpg.connect(db_url)
    try:
        insert = ("INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, max_drawdown_pct, "
                  "allow_perps, max_leverage, autonomy_level) VALUES ($1, 1000, $2, $3, $4, $5, $6, $7)")
        await conn.execute(insert, "ok", ["BTCUSDT.BINANCE"], 25, 20, False, 1, 1)
        for args in (("pos", ["BTCUSDT.BINANCE"], 26, 20, False, 1, 1), ("dd", ["BTCUSDT.BINANCE"], 10, 21, False, 1, 1),
                     ("lev", ["BTCUSDT.BINANCE"], 10, 20, True, 11, 3),
                     ("none", [], 10, 20, False, 1, 1)):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(insert, *args)
        # ADR 0011 D4: 050 dropped the perps-level CHECK, so perps are accepted at L2 as at L1
        await conn.execute(insert, "perps-l2", ["BTCUSDT.BINANCE"], 10, 20, True, 2, 2)
        mid = await conn.fetchval("SELECT id FROM ai_mandates WHERE name = 'ok'")
        await conn.execute("INSERT INTO ai_autonomy_events (mandate_id, kind, proposal_status) VALUES ($1, 'PROPOSED', 'PENDING')", mid)
        with pytest.raises(asyncpg.UniqueViolationError):  # one open proposal per mandate
            await conn.execute("INSERT INTO ai_autonomy_events (mandate_id, kind, proposal_status) VALUES ($1, 'PROPOSED', 'PENDING')", mid)
        assert await conn.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_name = 'execution_commands' AND column_name = 'mandate_id'") == 1
    finally:
        await conn.close()


async def test_agent_ro_cannot_read_mandates(db_url):
    conn = await asyncpg.connect(db_url)
    try:
        has = await conn.fetchval("SELECT has_table_privilege('cryptonite_agent_ro', 'ai_mandates', 'SELECT')")
        api = await conn.fetchval("SELECT has_table_privilege('cryptonite_api', 'ai_mandates', 'UPDATE')")
        api_eq = await conn.fetchval("SELECT has_table_privilege('cryptonite_api', 'ai_mandate_equity', 'INSERT')")
    finally:
        await conn.close()
    assert (has, api, api_eq) == (False, True, False)


@pytest.fixture(autouse=True)
def _autotrade_switch_on(monkeypatch):
    """ADR 0011 D10 applies at runtime: with the master switch off a Veto window or Fully automatic mandate behaves as
    Ask me. These tests exercise the switch-on behaviour of those levels."""
    from cryptonite_execution import profile_lifecycle

    async def on(conn):
        return True

    monkeypatch.setattr(profile_lifecycle, "autotrade_switch_on", on)
