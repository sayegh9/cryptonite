"""Autonomy ladder: the state machine and the promotion gate evaluator (AI-first blueprint §4.5, A5.5).

Pure functions only; callers persist the result as an ``ai_autonomy_events`` row and update
``ai_mandates``. The rules:

- promotion goes one level at a time, is proposed by the system from a passing gate report and is
  applied only when the operator confirms it with TOTP (never silently, §10 invariant 6);
- demotion is automatic: any breach, or a drawdown >= half the hard stop, drops one level;
- the hard stop (and the account-wide backstop) halts the mandate and drops it to L1;
- leaving L3 always withdraws the perps opt-in (O1);
- a halted or paused mandate resumes only with operator TOTP, at the level it holds (L1 after a stop).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from cryptonite_schemas.mandate import AutonomyLevel, MandateStatus

# §4.5 promotion gates
L2_MIN_DAYS = 30
L2_MIN_CLOSED_TRADES = 30
L2_MAX_DRAWDOWN_PCT = Decimal("10")
L3_MIN_DAYS = 60
L3_MAX_CALIBRATION_ERROR_PP = Decimal("10")
L3_MAX_VETO_RATE_PCT = Decimal("10")


class AutonomyTrigger(StrEnum):
    PROMOTION_CONFIRMED = "PROMOTION_CONFIRMED"  # operator confirmed a proposal with TOTP
    BREACH = "BREACH"  # the AI tried to act outside its mandate
    HALF_DRAWDOWN = "HALF_DRAWDOWN"  # drawdown >= half the hard stop
    HARD_STOP = "HARD_STOP"  # drawdown >= the hard stop
    BACKSTOP = "BACKSTOP"  # account-wide 20% backstop
    OPERATOR_DEMOTE = "OPERATOR_DEMOTE"
    PAUSE = "PAUSE"
    RESUME = "RESUME"


class TransitionRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AutonomyState:
    level: AutonomyLevel
    status: MandateStatus = MandateStatus.ACTIVE
    allow_perps: bool = False


@dataclass(frozen=True)
class Transition:
    before: AutonomyState
    after: AutonomyState
    kind: str  # ai_autonomy_events.kind; NOOP = nothing changes (e.g. a demotion at L1)
    reason: str

    @property
    def level_changed(self) -> bool:
        return self.before.level != self.after.level


@dataclass(frozen=True)
class GateCheck:
    name: str
    required: str
    actual: str
    passed: bool


@dataclass(frozen=True)
class GateReport:
    level: AutonomyLevel
    target_level: AutonomyLevel | None
    checks: tuple[GateCheck, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.target_level is not None and bool(self.checks) and all(c.passed for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "level": int(self.level), "target_level": int(self.target_level) if self.target_level else None,
            "passed": self.passed,
            "checks": [{"name": c.name, "required": c.required, "actual": c.actual, "passed": c.passed} for c in self.checks],
        }


@dataclass(frozen=True)
class GateMetrics:
    days_at_level: float = 0.0
    closed_trades: int = 0
    max_drawdown_pct: Decimal | None = None
    breaches: int = 0
    sortino: float | None = None
    benchmark_sortino: float | None = None  # BTC buy-and-hold over the same window
    honest_paper: bool = True  # A0.4 paper fidelity is in place
    calibration_error_pp: Decimal | None = None  # measured from A7.2 on; None fails the L3 gate
    veto_rate_pct: Decimal | None = None


def _step_down(state: AutonomyState, reason: str) -> Transition:
    """One level down (L1 stays L1: kind NOOP). Leaving L3 withdraws the perps opt-in."""
    level = AutonomyLevel(max(int(state.level) - 1, int(AutonomyLevel.L1)))
    after = AutonomyState(level, state.status, state.allow_perps and level == AutonomyLevel.L3)
    return Transition(state, after, "DEMOTED" if level != state.level else "NOOP", reason)


def transition(
    state: AutonomyState,
    trigger: AutonomyTrigger,
    *,
    totp_ok: bool = False,
    gate: GateReport | None = None,
    reason: str = "",
) -> Transition:
    """The next state for ``trigger``, or ``TransitionRefused``."""
    if trigger is AutonomyTrigger.PROMOTION_CONFIRMED:
        if not totp_ok:
            raise TransitionRefused("TOTP_REQUIRED")
        if state.status is not MandateStatus.ACTIVE:
            raise TransitionRefused("MANDATE_NOT_ACTIVE")
        if state.level >= AutonomyLevel.L3:
            raise TransitionRefused("ALREADY_AT_TOP")
        target = AutonomyLevel(int(state.level) + 1)
        if gate is None or gate.level != state.level or gate.target_level != target or not gate.passed:
            raise TransitionRefused("GATE_NOT_PASSED")
        return Transition(state, AutonomyState(target, state.status, state.allow_perps), "PROMOTED",
                          reason or f"operator confirmed promotion to L{int(target)}")

    if trigger is AutonomyTrigger.BREACH:
        return _step_down(state, reason or "mandate breach")
    if trigger is AutonomyTrigger.HALF_DRAWDOWN:
        return _step_down(state, reason or "drawdown >= half the hard stop")
    if trigger is AutonomyTrigger.OPERATOR_DEMOTE:
        if state.level == AutonomyLevel.L1:
            raise TransitionRefused("ALREADY_AT_BOTTOM")
        return _step_down(state, reason or "operator demotion")

    if trigger in (AutonomyTrigger.HARD_STOP, AutonomyTrigger.BACKSTOP):
        after = AutonomyState(AutonomyLevel.L1, MandateStatus.HALTED, False)
        kind = "HALTED" if trigger is AutonomyTrigger.HARD_STOP else "BACKSTOP"
        return Transition(state, after, kind, reason or trigger.value)

    if trigger is AutonomyTrigger.PAUSE:
        if state.status is not MandateStatus.ACTIVE:
            raise TransitionRefused("MANDATE_NOT_ACTIVE")
        return Transition(state, AutonomyState(state.level, MandateStatus.PAUSED, state.allow_perps), "PAUSED",
                          reason or "operator pause")

    if trigger is AutonomyTrigger.RESUME:
        if state.status not in (MandateStatus.HALTED, MandateStatus.PAUSED):
            raise TransitionRefused("MANDATE_NOT_STOPPED")
        if not totp_ok:
            raise TransitionRefused("TOTP_REQUIRED")
        return Transition(state, AutonomyState(state.level, MandateStatus.ACTIVE, state.allow_perps), "RESUMED",
                          reason or "operator resumed")

    raise TransitionRefused(f"UNKNOWN_TRIGGER:{trigger}")


def _fmt(v) -> str:
    if v is None:
        return "not measured"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def evaluate_gate(level: AutonomyLevel, m: GateMetrics) -> GateReport:
    """The §4.5 checklist for the next level. Unmeasured numbers fail (the gate fails closed)."""
    if level == AutonomyLevel.L1:
        sortino_ok = m.sortino is not None and m.benchmark_sortino is not None and m.sortino > m.benchmark_sortino
        checks = (
            GateCheck("days_at_level", f">= {L2_MIN_DAYS} d", f"{m.days_at_level:.1f} d", m.days_at_level >= L2_MIN_DAYS),
            GateCheck("closed_trades", f">= {L2_MIN_CLOSED_TRADES}", str(m.closed_trades), m.closed_trades >= L2_MIN_CLOSED_TRADES),
            GateCheck("honest_paper", "A0.4 paper fidelity", "yes" if m.honest_paper else "no", m.honest_paper),
            GateCheck("max_drawdown_pct", f"<= {L2_MAX_DRAWDOWN_PCT}%", _fmt(m.max_drawdown_pct),
                      m.max_drawdown_pct is not None and m.max_drawdown_pct <= L2_MAX_DRAWDOWN_PCT),
            GateCheck("mandate_breaches", "0", str(m.breaches), m.breaches == 0),
            GateCheck("sortino_vs_btc_hold", "> BTC hold (after fees)",
                      f"{_fmt(m.sortino)} vs {_fmt(m.benchmark_sortino)}", sortino_ok),
        )
        return GateReport(level, AutonomyLevel.L2, checks)
    if level == AutonomyLevel.L2:
        checks = (
            GateCheck("days_at_level", f">= {L3_MIN_DAYS} d", f"{m.days_at_level:.1f} d", m.days_at_level >= L3_MIN_DAYS),
            GateCheck("calibration_error_pp", f"within ±{L3_MAX_CALIBRATION_ERROR_PP} pp", _fmt(m.calibration_error_pp),
                      m.calibration_error_pp is not None and abs(m.calibration_error_pp) <= L3_MAX_CALIBRATION_ERROR_PP),
            GateCheck("veto_rate_pct", f"< {L3_MAX_VETO_RATE_PCT}%", _fmt(m.veto_rate_pct),
                      m.veto_rate_pct is not None and m.veto_rate_pct < L3_MAX_VETO_RATE_PCT),
            GateCheck("mandate_breaches", "0", str(m.breaches), m.breaches == 0),
        )
        return GateReport(level, AutonomyLevel.L3, checks)
    return GateReport(level, None, ())


def sortino(returns: Sequence[float], periods_per_year: int = 365) -> float | None:
    """Annualised Sortino ratio of periodic returns (target 0). None with fewer than 2 returns."""
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    downside = [min(r, 0.0) ** 2 for r in returns]
    dd = math.sqrt(sum(downside) / len(returns))
    if dd == 0:
        return math.inf if mean > 0 else 0.0
    return mean / dd * math.sqrt(periods_per_year)


def period_returns(values: Sequence[float]) -> list[float]:
    return [(b - a) / a for a, b in zip(values, values[1:]) if a]
