"""Per-mandate daily sessions: the daily loss limit, the daily profit target and the profit lock (ADR 0010).

Pure code only (no I/O, no sibling packages), so the orchestrator, the gate, the debate worker, the API and the Brain
all read one rule. The session state is never stored: it is derived from today's ``ai_mandate_daily_sessions`` row
(P&L and the latch times the orchestrator recorded) and the mandate's current limits.

A session runs from 00:00 UTC to the next 00:00 UTC. P&L is the mandate's equity change since the start of the day,
and every limit is a percentage of the mandate's budget. A recorded latch is sticky for the day: once the loss latch,
the target or the lock has been recorded, the current P&L (and an edit of the limits) no longer clears it. The next
UTC day starts a new row, so everything clears at 00:00 UTC.
"""

import datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

HUNDRED = Decimal("100")


class SessionVerdict(StrEnum):
    ACTIVE = "ACTIVE"  # new entries allowed
    TARGET_MET = "TARGET_MET"  # profit target reached: no new entries, positions kept, stops may tighten
    LOCK_FIRED = "LOCK_FIRED"  # P&L fell back to the profit lock after the target: flattened, no new entries
    LOSS_LATCHED = "LOSS_LATCHED"  # daily loss limit reached: flattened, no new entries


def utc_session_date(now: datetime.datetime) -> datetime.date:
    """The session (UTC calendar day) ``now`` falls in. A naive datetime is read as UTC."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.UTC)
    return now.astimezone(datetime.UTC).date()


def session_start(day: datetime.date) -> datetime.datetime:
    """00:00 UTC of the session ``day``."""
    return datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=datetime.UTC)


def next_session_start(now: datetime.datetime) -> datetime.datetime:
    """The next 00:00 UTC after ``now``: when today's latches clear."""
    return session_start(utc_session_date(now) + datetime.timedelta(days=1))


def loss_limit_usd(budget: Decimal, daily_loss_limit_pct: Decimal | None) -> Decimal | None:
    return None if daily_loss_limit_pct is None else budget * daily_loss_limit_pct / HUNDRED


def target_usd(budget: Decimal, daily_profit_target_pct: Decimal | None) -> Decimal | None:
    return None if daily_profit_target_pct is None else budget * daily_profit_target_pct / HUNDRED


def lock_usd(budget: Decimal, daily_profit_target_pct: Decimal | None, profit_lock_pct: Decimal) -> Decimal | None:
    """The session P&L at which the profit lock flattens, once the target has been hit."""
    t = target_usd(budget, daily_profit_target_pct)
    return None if t is None else t * profit_lock_pct / HUNDRED


def session_verdict(
    budget: Decimal,
    daily_loss_limit_pct: Decimal | None,
    daily_profit_target_pct: Decimal | None,
    profit_lock_pct: Decimal,
    pnl: Decimal,
    loss_latched_at: datetime.datetime | None,
    target_hit_at: datetime.datetime | None,
    lock_fired_at: datetime.datetime | None,
) -> SessionVerdict:
    """Today's verdict for one mandate. Precedence: LOSS_LATCHED > LOCK_FIRED > TARGET_MET > ACTIVE.

    - loss: ``pnl <= -budget * daily_loss_limit_pct / 100``, or a recorded ``loss_latched_at``;
    - target: ``pnl >= budget * daily_profit_target_pct / 100``, or a recorded ``target_hit_at``;
    - lock: after the target was hit, ``pnl <= target_usd * profit_lock_pct / 100``, or a recorded ``lock_fired_at``.

    A NULL limit is off. A recorded latch wins over the current P&L and over the current limits (sticky for the day).
    """
    if loss_latched_at is not None:
        return SessionVerdict.LOSS_LATCHED
    loss = loss_limit_usd(budget, daily_loss_limit_pct)
    if loss is not None and pnl <= -loss:
        return SessionVerdict.LOSS_LATCHED
    if lock_fired_at is not None:
        return SessionVerdict.LOCK_FIRED
    target = target_usd(budget, daily_profit_target_pct)
    target_hit = target_hit_at is not None or (target is not None and pnl >= target)
    if not target_hit:
        return SessionVerdict.ACTIVE
    lock = lock_usd(budget, daily_profit_target_pct, profit_lock_pct)
    if lock is not None and pnl <= lock:
        return SessionVerdict.LOCK_FIRED
    return SessionVerdict.TARGET_MET


# Today's ai_mandate_daily_sessions row, as every reader selects it
SESSION_COLUMNS = ("session_date, start_equity, start_budget, peak_pnl, last_pnl, loss_latched_at, target_hit_at, "
                   "lock_fired_at, updated_at")


def verdict_from_row(mandate, row) -> SessionVerdict:
    """The verdict of ``mandate`` (a ``Mandate``: its budget and current limits) from today's session ``row``
    (``SESSION_COLUMNS``); no row yet reads as P&L 0 with nothing latched."""
    if row is None:
        pnl, loss_at, target_at, lock_at = Decimal("0"), None, None, None
    else:
        pnl = Decimal(str(row["last_pnl"]))
        loss_at, target_at, lock_at = row["loss_latched_at"], row["target_hit_at"], row["lock_fired_at"]
    return session_verdict(mandate.capital_budget_usd, mandate.daily_loss_limit_pct, mandate.daily_profit_target_pct,
                           mandate.profit_lock_pct, pnl, loss_at, target_at, lock_at)


def entries_allowed(verdict: SessionVerdict) -> bool:
    return verdict is SessionVerdict.ACTIVE


# The refusal shown on an order card's dry run, per verdict (LOCK_FIRED reads as the target being reached)
SESSION_REFUSALS = {
    SessionVerdict.LOSS_LATCHED: "daily loss limit reached — new entries resume 00:00 UTC",
    SessionVerdict.TARGET_MET: "daily profit target reached — new entries resume 00:00 UTC",
    SessionVerdict.LOCK_FIRED: "daily profit target reached — new entries resume 00:00 UTC",
}


def reward_risk(entry: Decimal, stop_loss: Decimal, take_profit: Decimal) -> Decimal | None:
    """R:R = |take_profit - entry| / |entry - stop_loss|; None when the stop sits at the entry."""
    risk = abs(entry - stop_loss)
    if risk == 0:
        return None
    return abs(take_profit - entry) / risk


def _ratio(v: Decimal) -> str:
    """2.0, 2.25, 1.6: two decimals at most (cut, never rounded up past a minimum), one at least."""
    text = format(v.quantize(Decimal("0.01"), rounding=ROUND_DOWN).normalize(), "f")
    return text if "." in text else f"{text}.0"


# Refusal codes of the entry filters outside the orchestrator's gate (the API dry runs, the debate worker's log);
# the gate itself refuses the session cases with MandateReject.DAILY_LOSS_LATCHED / DAILY_TARGET_MET
MIN_CONVICTION_REFUSAL = "MANDATE_MIN_CONVICTION"
MIN_RR_REFUSAL = "MANDATE_MIN_RR"
SESSION_REFUSAL_CODES = {
    SessionVerdict.LOSS_LATCHED: "MANDATE_DAILY_LOSS_LATCHED",
    SessionVerdict.TARGET_MET: "MANDATE_DAILY_TARGET_MET",
    SessionVerdict.LOCK_FIRED: "MANDATE_DAILY_TARGET_MET",
}


def entry_filter_refusal(mandate, verdict: SessionVerdict, *, conviction: Decimal | float | None = None,
                         entry: Decimal | None = None, stop_loss: Decimal | None = None,
                         take_profit: Decimal | None = None, side: str = "BUY",
                         require_inputs: bool = False) -> tuple[str, str] | None:
    """(code, text) when ``mandate`` refuses a new entry (ADR 0010), else None. Refuse only: nothing is resized.

    The daily session first, then the conviction, then R:R. ``side`` is the order's side (BUY is a long, SELL a short):
    the stop must sit on the loss side of the entry and the target on the profit side, else the entry is refused.
    ``require_inputs`` (a Brain card, which can carry all of them): a filter the mandate sets is not skipped when its
    input is missing, the entry is refused. Without it a missing confidence, or a missing stop, target or entry price,
    skips that filter (a debate carries a conviction but no stop or target)."""
    if verdict is not SessionVerdict.ACTIVE:
        return SESSION_REFUSAL_CODES[verdict], SESSION_REFUSALS[verdict]
    if mandate.min_conviction is not None:
        if conviction is None:
            if require_inputs:
                return MIN_CONVICTION_REFUSAL, (f"the mandate requires conviction \u2265 {mandate.min_conviction:.2f}: "
                                                "the card has no confidence")
        else:
            c = Decimal(str(conviction))
            if c < mandate.min_conviction:
                return MIN_CONVICTION_REFUSAL, f"conviction {c:.2f} below the mandate minimum {mandate.min_conviction:.2f}"
    if mandate.min_rr is not None:
        if require_inputs and not (stop_loss and take_profit):
            return MIN_RR_REFUSAL, (f"the mandate requires R:R \u2265 {_ratio(mandate.min_rr)}: add a stop and a target")
        if entry and stop_loss and take_profit:
            e, sl, tp = Decimal(str(entry)), Decimal(str(stop_loss)), Decimal(str(take_profit))
            long = side.upper() != "SELL"
            if not (sl < e < tp if long else tp < e < sl):
                return MIN_RR_REFUSAL, "stop/target on the wrong side of the entry"
            rr = reward_risk(e, sl, tp)
            if rr is not None and rr < mandate.min_rr:
                return MIN_RR_REFUSAL, f"R:R {_ratio(rr)} below the mandate minimum {_ratio(mandate.min_rr)}"
    return None


__all__ = [
    "MIN_CONVICTION_REFUSAL", "MIN_RR_REFUSAL", "SESSION_COLUMNS", "SESSION_REFUSALS", "SESSION_REFUSAL_CODES",
    "entry_filter_refusal", "SessionVerdict", "entries_allowed", "lock_usd", "loss_limit_usd", "next_session_start",
    "reward_risk", "session_start", "session_verdict", "target_usd", "utc_session_date", "verdict_from_row",
]
