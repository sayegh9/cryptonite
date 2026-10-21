"""ADR 0010 contract: the daily-session verdict, the entry filters' wording, the five new mandate fields (validators and
what counts as a risk-adding edit) and the mandate check's two new refusals. Pure code: no database."""

import datetime
import uuid
from decimal import Decimal

import pytest
from cryptonite_execution.mandates import MandateExposure, MandateOrder, check_mandate
from cryptonite_schemas.mandate import (
    BREACH_CODES,
    AutonomyLevel,
    Mandate,
    MandateReject,
    MandateStatus,
    risk_adding_change,
)
from cryptonite_schemas.session import (
    SESSION_REFUSALS,
    SessionVerdict,
    entry_filter_refusal,
    next_session_start,
    reward_risk,
    session_verdict,
    utc_session_date,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

D = Decimal
T = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.UTC)
BUDGET = D("10000")


def verdict(pnl, *, loss="3", target="2", lock="50", loss_at=None, target_at=None, lock_at=None):
    return session_verdict(BUDGET, D(loss) if loss is not None else None, D(target) if target is not None else None,
                           D(lock), D(pnl), loss_at, target_at, lock_at)


# --- session_verdict --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("pnl", "expected"), [
    ("0", SessionVerdict.ACTIVE),
    ("-299.99", SessionVerdict.ACTIVE),
    ("-300", SessionVerdict.LOSS_LATCHED),  # pnl <= -budget * 3%: the boundary latches
    ("-300.01", SessionVerdict.LOSS_LATCHED),
    ("199.99", SessionVerdict.ACTIVE),
    ("200", SessionVerdict.TARGET_MET),  # pnl >= budget * 2%: the boundary meets the target
    ("5000", SessionVerdict.TARGET_MET),
])
def test_verdict_boundaries(pnl, expected):
    assert verdict(pnl) is expected


def test_the_lock_fires_only_after_the_target_was_hit():
    # 100 is the lock level (50% of the $200 target), but the target was never hit: still ACTIVE
    assert verdict("100") is SessionVerdict.ACTIVE
    assert verdict("100.01", target_at=T) is SessionVerdict.TARGET_MET
    assert verdict("100", target_at=T) is SessionVerdict.LOCK_FIRED  # pnl <= target * lock%: the boundary fires
    assert verdict("-50", target_at=T) is SessionVerdict.LOCK_FIRED
    # profit lock 0: it fires when the session gives back everything, 100: at the target itself
    assert verdict("0.01", target_at=T, lock="0") is SessionVerdict.TARGET_MET
    assert verdict("0", target_at=T, lock="0") is SessionVerdict.LOCK_FIRED
    assert verdict("200", lock="100") is SessionVerdict.LOCK_FIRED


def test_null_limits_are_off():
    assert verdict("-9999", loss=None) is SessionVerdict.ACTIVE
    assert verdict("9999", target=None) is SessionVerdict.ACTIVE
    assert verdict("-9999", loss=None, target=None) is SessionVerdict.ACTIVE


def test_latches_are_sticky_for_the_day_whatever_the_pnl_or_the_limits():
    assert verdict("500", loss_at=T) is SessionVerdict.LOSS_LATCHED
    assert verdict("500", loss=None, loss_at=T) is SessionVerdict.LOSS_LATCHED  # the limit removed after the latch
    assert verdict("500", lock_at=T) is SessionVerdict.LOCK_FIRED
    assert verdict("-10", target_at=T, lock="0", target=None) is SessionVerdict.TARGET_MET  # no target, no lock level
    assert verdict("0", target=None, target_at=T) is SessionVerdict.TARGET_MET


def test_precedence_loss_then_lock_then_target():
    assert verdict("0", loss_at=T, lock_at=T, target_at=T) is SessionVerdict.LOSS_LATCHED
    assert verdict("-300", target_at=T) is SessionVerdict.LOSS_LATCHED  # a loss beyond the limit after the target
    assert verdict("500", lock_at=T, target_at=T) is SessionVerdict.LOCK_FIRED


pcts = st.one_of(st.none(), st.decimals(min_value=D("0.01"), max_value=D("20"), places=2))
stamps = st.one_of(st.none(), st.just(T))


@settings(max_examples=2000, deadline=None)
@given(loss=pcts, target=pcts, lock=st.decimals(min_value=D("0"), max_value=D("100"), places=2),
       pnl=st.decimals(min_value=D("-5000"), max_value=D("5000"), places=2), loss_at=stamps, target_at=stamps,
       lock_at=stamps)
def test_verdict_properties(loss, target, lock, pnl, loss_at, target_at, lock_at):
    v = session_verdict(BUDGET, loss, target, lock, pnl, loss_at, target_at, lock_at)
    # a recorded latch always holds, in precedence order
    if loss_at is not None:
        assert v is SessionVerdict.LOSS_LATCHED
    elif lock_at is not None:
        assert v in (SessionVerdict.LOSS_LATCHED, SessionVerdict.LOCK_FIRED)
    elif target_at is not None:
        assert v is not SessionVerdict.ACTIVE
    # the loss limit always wins when breached
    if loss is not None and pnl <= -BUDGET * loss / 100:
        assert v is SessionVerdict.LOSS_LATCHED
    # nothing set and nothing recorded: always ACTIVE
    if loss is None and target is None and not (loss_at or target_at or lock_at):
        assert v is SessionVerdict.ACTIVE
    # the lock never fires before the target was hit (now or before)
    if v is SessionVerdict.LOCK_FIRED and lock_at is None:
        assert target_at is not None or (target is not None and pnl >= BUDGET * target / 100)
    # ACTIVE means: inside both limits and nothing recorded
    if v is SessionVerdict.ACTIVE:
        assert loss_at is target_at is lock_at is None
        assert loss is None or pnl > -BUDGET * loss / 100
        assert target is None or pnl < BUDGET * target / 100


def test_session_dates_are_utc_days():
    cairo = datetime.timezone(datetime.timedelta(hours=3))
    late = datetime.datetime(2026, 10, 22, 1, 30, tzinfo=cairo)  # 22:30 UTC on the 21st
    assert utc_session_date(late) == datetime.date(2026, 10, 21)
    assert utc_session_date(datetime.datetime(2026, 10, 21, 23, 59, 59)) == datetime.date(2026, 10, 21)  # naive: UTC
    assert next_session_start(late) == datetime.datetime(2026, 10, 22, tzinfo=datetime.UTC)


# --- entry filters ----------------------------------------------------------------------------------------------------

def mandate(**kw) -> Mandate:
    base = {"id": uuid.uuid4(), "name": "M", "capital_budget_usd": BUDGET, "instruments": ["BTCUSDT"],
            "max_position_pct": D("10")}
    return Mandate(**{**base, **kw})


def test_reward_risk():
    assert reward_risk(D("100"), D("95"), D("110")) == D("2")
    assert reward_risk(D("100"), D("105"), D("90")) == D("2")  # a short
    assert reward_risk(D("100"), D("100"), D("110")) is None


def test_entry_filter_wording():
    m = mandate(min_conviction=D("0.70"), min_rr=D("2.0"))
    active = SessionVerdict.ACTIVE
    assert entry_filter_refusal(m, active, conviction=0.62) == (
        "MANDATE_MIN_CONVICTION", "conviction 0.62 below the mandate minimum 0.70")
    assert entry_filter_refusal(m, active, conviction=0.70) is None
    assert entry_filter_refusal(m, active, entry=D("100"), stop_loss=D("95"), take_profit=D("108")) == (
        "MANDATE_MIN_RR", "R:R 1.6 below the mandate minimum 2.0")
    # cut, never rounded up past the minimum: 1.996 reads 1.99
    assert entry_filter_refusal(m, active, entry=D("100"), stop_loss=D("95"), take_profit=D("109.98"))[1] == (
        "R:R 1.99 below the mandate minimum 2.0")
    assert entry_filter_refusal(m, active, entry=D("100"), stop_loss=D("95"), take_profit=D("110")) is None
    # no confidence, no entry price, no stop or no target: that filter is skipped
    assert entry_filter_refusal(m, active) is None
    assert entry_filter_refusal(m, active, stop_loss=D("95"), take_profit=D("108")) is None
    # the session comes first
    assert entry_filter_refusal(m, SessionVerdict.LOSS_LATCHED, conviction=0.62) == (
        "MANDATE_DAILY_LOSS_LATCHED", "daily loss limit reached — new entries resume 00:00 UTC")
    assert entry_filter_refusal(m, SessionVerdict.TARGET_MET) == (
        "MANDATE_DAILY_TARGET_MET", "daily profit target reached — new entries resume 00:00 UTC")
    assert entry_filter_refusal(m, SessionVerdict.LOCK_FIRED)[1] == SESSION_REFUSALS[SessionVerdict.TARGET_MET]
    # filters off: nothing refused
    assert entry_filter_refusal(mandate(), active, conviction=0.1, entry=D("100"), stop_loss=D("99"),
                                take_profit=D("100.5")) is None


# --- the Mandate model ------------------------------------------------------------------------------------------------

def test_new_fields_default_off_and_the_lock_to_50():
    m = mandate()
    assert (m.min_conviction, m.min_rr, m.daily_loss_limit_pct, m.daily_profit_target_pct) == (None, None, None, None)
    assert m.profit_lock_pct == D("50")


def test_a_perps_mandate_gets_3x_its_risk_per_trade_as_daily_loss_limit():
    assert mandate(instruments=["BTCUSDT-PERP"], allow_perps=True).daily_loss_limit_pct == D("3")
    assert mandate(instruments=["BTCUSDT-PERP"], allow_perps=True,
                   max_risk_per_trade_pct=D("1.5")).daily_loss_limit_pct == D("4.5")
    # capped at the hard stop
    assert mandate(instruments=["BTCUSDT-PERP"], allow_perps=True, max_risk_per_trade_pct=D("2"),
                   max_drawdown_pct=D("5")).daily_loss_limit_pct == D("5")
    # named (or explicitly off) wins; spot stays off
    assert mandate(instruments=["BTCUSDT-PERP"], allow_perps=True,
                   daily_loss_limit_pct=D("2")).daily_loss_limit_pct == D("2")
    assert mandate(instruments=["BTCUSDT-PERP"], allow_perps=True,
                   daily_loss_limit_pct=None).daily_loss_limit_pct is None
    assert mandate().daily_loss_limit_pct is None


def test_from_row_keeps_null_and_reads_rows_from_before_049():
    row = {"id": uuid.uuid4(), "name": "P", "capital_budget_usd": BUDGET, "instruments": ["BTCUSDT-PERP.BINANCE"],
           "max_position_pct": D("10"), "max_drawdown_pct": D("20"), "allow_perps": True, "max_leverage": 3,
           "max_risk_per_trade_pct": D("1"), "strategies": [], "autonomy_level": 1, "status": "ACTIVE",
           "active_hours_utc": None, "veto_window_s": 600}
    old = Mandate.from_row(row)  # no 049 columns: NULL is passed, so the stored (absent) value is kept as off
    assert (old.daily_loss_limit_pct, old.profit_lock_pct) == (None, D("50"))
    m = Mandate.from_row({**row, "min_conviction": D("0.70"), "min_rr": D("2.00"), "daily_loss_limit_pct": D("3.00"),
                          "daily_profit_target_pct": D("2.00"), "profit_lock_pct": D("40.00")})
    assert (m.min_conviction, m.min_rr, m.daily_loss_limit_pct, m.daily_profit_target_pct, m.profit_lock_pct) == (
        D("0.70"), D("2.00"), D("3.00"), D("2.00"), D("40.00"))


@pytest.mark.parametrize("kw", [
    {"daily_loss_limit_pct": D("0.5")},  # below one trade's risk (1%)
    {"daily_loss_limit_pct": D("12"), "max_drawdown_pct": D("10")},  # above the hard stop
    {"min_conviction": D("0.49")}, {"min_conviction": D("0.96")},
    {"min_rr": D("0.99")},
    {"daily_profit_target_pct": D("0")}, {"daily_profit_target_pct": D("100.01")},
    {"profit_lock_pct": D("-1")}, {"profit_lock_pct": D("101")},
])
def test_validators_refuse(kw):
    with pytest.raises(ValidationError):
        mandate(**kw)


@pytest.mark.parametrize(("field", "old", "new", "adds_risk"), [
    ("min_conviction", "0.70", "0.60", True),  # lowered
    ("min_conviction", "0.70", None, True),  # removed
    ("min_conviction", "0.70", "0.80", False),  # raised: tighter
    ("min_conviction", None, "0.70", False),  # set
    ("min_rr", "2.0", "1.5", True),
    ("min_rr", "2.0", None, True),
    ("min_rr", "2.0", "2.5", False),
    ("min_rr", None, "2.0", False),
    ("daily_loss_limit_pct", "3", "4", True),  # raised
    ("daily_loss_limit_pct", "3", None, True),  # removed
    ("daily_loss_limit_pct", "3", "2", False),
    ("daily_loss_limit_pct", None, "3", False),
    ("daily_profit_target_pct", "2", "3", True),
    ("daily_profit_target_pct", "2", None, True),
    ("daily_profit_target_pct", "2", "1", False),
    ("daily_profit_target_pct", None, "2", False),
    ("profit_lock_pct", "50", "40", True),  # lowered: gives back more before flattening
    ("profit_lock_pct", "50", "60", False),
])
def test_risk_adding_change_for_every_new_field(field, old, new, adds_risk):
    before = mandate(**{field: D(old) if old is not None else None})
    after = before.model_copy(update={field: D(new) if new is not None else None})
    assert risk_adding_change(before, after) is adds_risk
    assert risk_adding_change(before, before) is False


# --- the mandate check ------------------------------------------------------------------------------------------------

def test_new_reject_codes_are_refusals_not_breaches():
    assert MandateReject.DAILY_LOSS_LATCHED.value == "MANDATE_DAILY_LOSS_LATCHED"
    assert MandateReject.DAILY_TARGET_MET.value == "MANDATE_DAILY_TARGET_MET"
    assert MandateReject.DAILY_LOSS_LATCHED not in BREACH_CODES
    assert MandateReject.DAILY_TARGET_MET not in BREACH_CODES


BUY = MandateOrder("BTCUSDT.BINANCE", "BUY", D("0.01"), D("50000"))
SELL = MandateOrder("BTCUSDT.BINANCE", "SELL", D("0.01"), D("50000"))
HELD = MandateExposure(gross_usd=D("500"), instrument_usd=D("500"), instrument_qty=D("0.01"), instrument_side="LONG")


@pytest.mark.parametrize(("session", "code"), [
    (SessionVerdict.ACTIVE, None),
    (SessionVerdict.LOSS_LATCHED, MandateReject.DAILY_LOSS_LATCHED),
    (SessionVerdict.TARGET_MET, MandateReject.DAILY_TARGET_MET),
    (SessionVerdict.LOCK_FIRED, MandateReject.DAILY_TARGET_MET),
])
def test_the_session_refuses_add_risk_even_approved_and_never_reduce_risk(session, code):
    m = mandate(autonomy_level=AutonomyLevel.L1, max_position_pct=D("25"))
    assert check_mandate(m, BUY, HELD, approval_ok=True, session=session) is code
    assert check_mandate(m, SELL, HELD, approval_ok=True, session=session) is None
    assert check_mandate(m, SELL, HELD, approval_ok=False, session=session) is None
    # a perp reduce-only close passes too
    perp = mandate(instruments=["BTCUSDT-PERP"], allow_perps=True)
    close = MandateOrder("BTCUSDT-PERP.BINANCE", "SELL", D("0.01"), D("50000"), reduce_only=True)
    held_perp = MandateExposure(D("500"), D("500"), D("0.01"), "LONG")
    assert check_mandate(perp, close, held_perp, approval_ok=False, session=session) is None


def test_a_halted_mandate_keeps_its_halted_code():
    m = mandate(status=MandateStatus.HALTED)
    assert check_mandate(m, BUY, HELD, approval_ok=True, session=SessionVerdict.LOSS_LATCHED) is MandateReject.HALTED


def test_the_default_session_changes_nothing():
    m = mandate()
    assert check_mandate(m, BUY, HELD, approval_ok=True) == check_mandate(m, BUY, HELD, approval_ok=True,
                                                                          session=SessionVerdict.ACTIVE)
