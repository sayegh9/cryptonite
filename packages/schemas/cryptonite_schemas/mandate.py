"""AI mandates, autonomy levels and approval policy: the shared contract (AI-first blueprint §4.4, §4.5, D8, O1, O2).

Pure data and pure policy only (no I/O, no sibling packages), so every service can import it: the
orchestrator enforces it, the API edits it, the debate worker routes decisions by it and the notifier
renders it. The executable mandate check lives in ``cryptonite_execution.mandates`` and the autonomy
state machine in ``cryptonite_execution.autonomy``.
"""

import datetime
import math
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

# Hard ceilings that no mandate row can exceed (the DB has the same CHECK constraints)
MAX_POSITION_PCT_CAP = Decimal("25")  # same ceiling as the dispatcher's 25% concentration clamp
MAX_DRAWDOWN_PCT_CAP = Decimal("20")  # D10/O2: the hard stop is at most 20% of the mandate budget
ACCOUNT_BACKSTOP_DRAWDOWN_PCT = Decimal("20")  # O2: account-wide backstop that latches everything
MAX_PERP_LEVERAGE_CAP = 5  # O1: perps only after L3, capped at 5x
DEFAULT_VETO_WINDOW_S = 600  # §4.5: L2 veto window, 10 minutes
DEFAULT_APPROVAL_TTL_S = 900  # L1 approvals expire after 15 minutes (timeout = no for new risk)
DEFAULT_REDUCE_VETO_S = 300  # a model-proposed exit at L1 waits 5 minutes for a veto, then proceeds
# Smallest entry worth placing, as % of the mandate budget. An entry is sized down to the room left under
# the position limit and the budget; below this the order is not placed (a refusal, not a breach).
MIN_MANDATE_ENTRY_PCT = Decimal("1")
# A holding below this is dust (a fill rounding leftover), not something to sell. A SELL card needs at least this much.
MIN_HOLDING_QTY = Decimal("0.000000001")

MANDATE_INSTANCE_PREFIX = "AI_MANDATE_"
LEGACY_AI_INSTANCE = "AI_DEBATE_WORKER"  # pre-mandate AI instance; its positions still get AI exits


class AutonomyLevel(IntEnum):
    L1 = 1  # Copilot: proposes; every risk-adding action needs approval
    L2 = 2  # Supervised: acts inside the mandate; ADD_RISK waits out a veto window
    L3 = 3  # Autonomous: acts inside the mandate; digest only


LEVEL_NAMES = {AutonomyLevel.L1: "L1 Copilot", AutonomyLevel.L2: "L2 Supervised", AutonomyLevel.L3: "L3 Autonomous"}


class MandateStatus(StrEnum):
    ACTIVE = "ACTIVE"
    HALTED = "HALTED"  # hard stop or backstop: reduce-only until the operator resumes it (TOTP)
    PAUSED = "PAUSED"  # operator pause: reduce-only, no TOTP needed to pause
    ARCHIVED = "ARCHIVED"


class ActionClass(StrEnum):
    ADD_RISK = "ADD_RISK"
    REDUCE_RISK = "REDUCE_RISK"


class TimeoutPolicy(StrEnum):
    DENY = "DENY"  # timeout means no (new risk, D8)
    PROCEED = "PROCEED"  # timeout means go (risk reducing, D8; an L2 veto window inside active hours)


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    VETOED = "VETOED"
    EXPIRED = "EXPIRED"  # timed out with policy DENY: nothing happens
    EXPIRED_PROCEEDED = "EXPIRED_PROCEEDED"  # timed out with policy PROCEED: the action ran
    CANCELLED = "CANCELLED"


class MandateReject(StrEnum):
    """Reject codes of the mandate check. It runs before GuardedOrderDispatcher and never replaces it."""

    REQUIRED = "MANDATE_REQUIRED"  # an AI order without a mandate
    NOT_FOUND = "MANDATE_NOT_FOUND"
    HALTED = "MANDATE_HALTED"  # not ACTIVE: HALTED, PAUSED or ARCHIVED
    INSTRUMENT_NOT_ALLOWED = "MANDATE_INSTRUMENT_NOT_ALLOWED"
    PERPS_NOT_ALLOWED = "MANDATE_PERPS_NOT_ALLOWED"
    LEVERAGE_EXCEEDED = "MANDATE_LEVERAGE_EXCEEDED"
    BUDGET_EXCEEDED = "MANDATE_BUDGET_EXCEEDED"
    POSITION_PCT_EXCEEDED = "MANDATE_POSITION_PCT_EXCEEDED"
    POSITION_LIMIT_REACHED = "MANDATE_POSITION_LIMIT_REACHED"  # no room left for a new entry: not placed
    INSTRUMENT_CONFLICT = "MANDATE_INSTRUMENT_CONFLICT"  # someone else holds the instrument
    POSITION_NOT_OWNED = "MANDATE_POSITION_NOT_OWNED"  # a reduce larger than the mandate's own position
    APPROVAL_REQUIRED = "MANDATE_APPROVAL_REQUIRED"  # ADD_RISK below L3 without a valid approval
    NO_REFERENCE_PRICE = "MANDATE_NO_REFERENCE_PRICE"
    INSTANCE_UNAVAILABLE = "MANDATE_INSTANCE_UNAVAILABLE"
    MARGIN_MODE_NOT_ALLOWED = "MANDATE_MARGIN_MODE_NOT_ALLOWED"  # ADR 0009 D5: mandate perps are ISOLATED only
    # The orchestrator sized the entry itself and the check still found it over a limit: a system fault (sizing
    # and the check disagreeing), not the AI overstepping. Refused, logged as an error, never a breach.
    SIZING_CONFLICT = "MANDATE_SIZING_CONFLICT"


# Rejections that mean the AI tried to act outside its limits (§4.5: any breach demotes one level).
# Missing approval, a halted mandate, a missing price, an instrument someone else holds and a reduce
# larger than the mandate's (possibly just-changed) holding are refusals, not breaches: they can
# follow from the operator's own trades or timing, not from the AI overstepping. So is an entry with no
# room left under the position limit: the system sizes entries to the room, so reaching the limit is
# not the AI overstepping.
BREACH_CODES = frozenset({
    MandateReject.INSTRUMENT_NOT_ALLOWED, MandateReject.PERPS_NOT_ALLOWED, MandateReject.LEVERAGE_EXCEEDED,
    MandateReject.BUDGET_EXCEEDED, MandateReject.POSITION_PCT_EXCEEDED,
})


def canonical_instrument(raw: str) -> str:
    """BTCUSDT / btcusdt.binance / BTCUSDT-PERP -> BTCUSDT.BINANCE / BTCUSDT-PERP.BINANCE."""
    s = raw.strip().upper()
    return s if "." in s else f"{s}.BINANCE"


def is_perp_instrument(instrument_id: str) -> bool:
    return canonical_instrument(instrument_id).split(".", 1)[0].endswith("-PERP")


def mandate_instance_id(mandate_id: uuid.UUID | str) -> str:
    """The paper strategy instance a mandate's orders and positions are booked under."""
    return f"{MANDATE_INSTANCE_PREFIX}{uuid.UUID(str(mandate_id)).hex}"


def mandate_id_from_instance(instance_id: str | None) -> uuid.UUID | None:
    if not instance_id or not instance_id.startswith(MANDATE_INSTANCE_PREFIX):
        return None
    try:
        return uuid.UUID(instance_id[len(MANDATE_INSTANCE_PREFIX):])
    except ValueError:
        return None


def is_ai_instance(instance_id: str | None) -> bool:
    return instance_id == LEGACY_AI_INSTANCE or mandate_id_from_instance(instance_id) is not None


_HOURS = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


def in_active_hours(spec: str | None, now: datetime.datetime) -> bool:
    """Operator-set active hours, ``HH:MM-HH:MM`` in UTC (may wrap midnight). None: never active."""
    if not spec:
        return False
    m = _HOURS.match(spec)
    if not m:
        return False
    start = int(m.group(1)) * 60 + int(m.group(2))
    end = int(m.group(3)) * 60 + int(m.group(4))
    t = now.astimezone(datetime.UTC)
    minute = t.hour * 60 + t.minute
    if start == end:
        return True  # 00:00-00:00: all day
    return start <= minute < end if start < end else (minute >= start or minute < end)


def timeout_policy_for(action: ActionClass, level: AutonomyLevel, active_hours: bool) -> TimeoutPolicy | None:
    """What an unanswered approval means (D8, §4.5). None: this action needs no approval at all.

    - REDUCE_RISK: timeout proceeds (risk reducing runs).
    - ADD_RISK at L1: timeout is no.
    - ADD_RISK at L2: a veto window that proceeds inside the operator's active hours and is no outside them.
    - ADD_RISK at L3: no approval.
    """
    if action is ActionClass.REDUCE_RISK:
        return TimeoutPolicy.PROCEED
    if level >= AutonomyLevel.L3:
        return None
    if level == AutonomyLevel.L2 and active_hours:
        return TimeoutPolicy.PROCEED
    return TimeoutPolicy.DENY


def timeout_status(policy: TimeoutPolicy) -> ApprovalStatus:
    """The terminal status of a PENDING approval whose deadline passed."""
    return ApprovalStatus.EXPIRED_PROCEEDED if policy is TimeoutPolicy.PROCEED else ApprovalStatus.EXPIRED


class Mandate(BaseModel):
    """One ``ai_mandates`` row: the scope an AI may trade in, enforced in code."""

    id: uuid.UUID
    name: str = Field(min_length=1, max_length=80)
    capital_budget_usd: Decimal = Field(gt=0)
    instruments: list[str] = Field(min_length=1)
    max_position_pct: Decimal = Field(ge=1, le=MAX_POSITION_PCT_CAP)
    max_drawdown_pct: Decimal = Field(default=MAX_DRAWDOWN_PCT_CAP, gt=0, le=MAX_DRAWDOWN_PCT_CAP)
    allow_perps: bool = False
    max_leverage: int = Field(default=1, ge=1, le=MAX_PERP_LEVERAGE_CAP)
    strategies: list[str] = Field(default_factory=list)
    autonomy_level: AutonomyLevel = AutonomyLevel.L1
    status: MandateStatus = MandateStatus.ACTIVE
    active_hours_utc: str | None = None
    veto_window_s: int = Field(default=DEFAULT_VETO_WINDOW_S, ge=60, le=3600)

    @field_validator("instruments")
    @classmethod
    def _canonical(cls, v: list[str]) -> list[str]:
        return sorted({canonical_instrument(i) for i in v})

    @field_validator("active_hours_utc")
    @classmethod
    def _hours(cls, v: str | None) -> str | None:
        if v is not None and not _HOURS.match(v):
            raise ValueError("active_hours_utc must be HH:MM-HH:MM (UTC)")
        return v

    @model_validator(mode="after")
    def _perps_only_at_l3(self) -> "Mandate":
        # O1: spot-only until the mandate has earned L3
        if self.allow_perps and self.autonomy_level < AutonomyLevel.L3:
            raise ValueError("allow_perps requires autonomy level L3 (O1)")
        return self

    @property
    def instance_id(self) -> str:
        return mandate_instance_id(self.id)

    def allows_instrument(self, instrument_id: str) -> bool:
        return canonical_instrument(instrument_id) in self.instruments

    @classmethod
    def from_row(cls, row) -> "Mandate":
        r = dict(row)
        return cls(
            id=r["id"], name=r["name"], capital_budget_usd=r["capital_budget_usd"],
            instruments=list(r["instruments"] or []), max_position_pct=r["max_position_pct"],
            max_drawdown_pct=r["max_drawdown_pct"], allow_perps=r["allow_perps"], max_leverage=r["max_leverage"],
            strategies=list(r.get("strategies") or []), autonomy_level=AutonomyLevel(int(r["autonomy_level"])),
            status=MandateStatus(r["status"]), active_hours_utc=r.get("active_hours_utc"),
            veto_window_s=r.get("veto_window_s") or DEFAULT_VETO_WINDOW_S,
        )


def room_pct(budget: Decimal, max_position_pct: Decimal, inst_value: Decimal, gross_value: Decimal) -> Decimal:
    """Room for a new entry, as % of the budget: under the instrument's position limit (``inst_value`` is what the
    mandate holds of it) and under the budget (``gross_value`` is everything it holds)."""
    room = min(budget * max_position_pct / 100 - inst_value, budget - gross_value)
    return max(Decimal("0"), room / budget * 100)


@dataclass(frozen=True)
class RoomRemedy:
    """The one mandate edit that gives a refused entry room again: ``field`` goes from ``old`` to ``new``."""

    field: str  # max_position_pct | capital_budget_usd
    old: Decimal
    new: Decimal
    room_after_pct: Decimal  # the room the entry has after the edit (the entry is sized to it, at most the request)


# Holdings come from float fills (0.05 BTC is 0.0500000000000000027...): the remedy rounds to cents before it rounds
# up, so a hair over $2,500 asks for a 15% limit or a $12,500 budget, not 16% or $12,600
_CENT = Decimal("0.01")


def room_remedy(budget: Decimal, max_position_pct: Decimal, inst_value: Decimal, gross_value: Decimal,
                requested_pct: Decimal) -> RoomRemedy | None:
    """The smallest edit that makes room for an entry of ``requested_pct`` of the budget, or None.

    Raise the instrument's position limit first, to what the holding plus the request needs, at most the 25% cap.
    Raise the budget only when the limit is already at the cap or a higher limit gives no room (the budget is full
    of other holdings); the new budget fits the request under the current limit, rounded up to $100. An edit that
    still leaves less than the smallest entry is not offered. Pure: the caller values the holdings."""
    if budget <= 0 or requested_pct <= 0:
        return None
    if max_position_pct < MAX_POSITION_PCT_CAP:
        need = Decimal(math.ceil((inst_value / budget * 100 + requested_pct).quantize(_CENT)))
        limit = min(MAX_POSITION_PCT_CAP, need)
        if limit > max_position_pct:
            room = room_pct(budget, limit, inst_value, gross_value)
            if room >= MIN_MANDATE_ENTRY_PCT:
                return RoomRemedy("max_position_pct", max_position_pct, limit, room)
    # The request takes a share of the new budget, so a request at or above the limit can never fit whole: aim for
    # half the limit then, not for a budget that grows without bound
    target = requested_pct if requested_pct < max_position_pct else max_position_pct / 2
    need_budget = max(inst_value * 100 / (max_position_pct - target), gross_value * 100 / (100 - target))
    new_budget = Decimal(math.ceil(need_budget.quantize(_CENT) / 100) * 100)
    if new_budget <= budget:
        return None
    room = room_pct(new_budget, max_position_pct, inst_value, gross_value)
    if room < MIN_MANDATE_ENTRY_PCT:
        return None
    return RoomRemedy("capital_budget_usd", budget, new_budget, room)


def risk_adding_change(old: Mandate | None, new: Mandate) -> bool:
    """Does an edit give the AI more room? Those edits need operator TOTP (§7.1 Mandate editor)."""
    if old is None:
        return True
    return (
        new.capital_budget_usd > old.capital_budget_usd
        or new.max_position_pct > old.max_position_pct
        or new.max_drawdown_pct > old.max_drawdown_pct
        or (new.allow_perps and not old.allow_perps)
        or new.max_leverage > old.max_leverage
        or bool(set(new.instruments) - set(old.instruments))
        or bool(set(new.strategies) - set(old.strategies))
        or (new.active_hours_utc or "") != (old.active_hours_utc or "")
        or new.veto_window_s < old.veto_window_s
        or (new.status is MandateStatus.ACTIVE and old.status is not MandateStatus.ACTIVE)
        or new.autonomy_level > old.autonomy_level
    )
