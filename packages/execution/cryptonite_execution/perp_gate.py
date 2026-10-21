"""Stage 5 gate of the leverage plan (ADR 0009): may leveraged perps move on to the Binance futures testnet?

Pure: the trades come in, a checklist goes out (``scripts/perp_gate_status.py`` reads the DB). A trade's result is
measured in R: its realized PnL over the loss the card planned at the stop (``leverage_plan.loss_at_stop_usd``, which
already includes the slippage allowance and fees), so a stop that held costs about -1 R.

Required checks (all must pass): at least 30 closed AI perp trades over at least 30 days, no liquidation, no stop
failure, and a positive mean R. Informational (shown, never deciding): the mean R's 90% interval, the spot record over
the same window, and whether the setup grades order their outcomes (A > B > C).

A stop failure is a stop that did not hold: a trade that lost more than ``STOP_OVERRUN_R`` (1.25 R, the planned loss
plus a quarter), or a mandate close that sold less than the mandate owned (``CLOSE_SHORT``).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from cryptonite_execution.autonomy import GateCheck
from cryptonite_execution.stats import mean_interval

MIN_CLOSED_TRADES = 30
MIN_DAYS = 30
STOP_OVERRUN_R = Decimal("1.25")
GRADES = ("A", "B", "C")


@dataclass(frozen=True)
class PerpTrade:
    position_id: str
    instrument_id: str
    opened_at: datetime
    closed_at: datetime
    realized_pnl: Decimal
    planned_loss_usd: Decimal | None  # the card's loss at the stop; None when the entry carried no plan
    grade: str | None = None
    close_reason: str | None = None

    @property
    def r(self) -> Decimal | None:
        if self.planned_loss_usd is None or self.planned_loss_usd <= 0:
            return None
        return self.realized_pnl / self.planned_loss_usd

    @property
    def stop_overrun(self) -> bool:
        r = self.r
        return r is not None and r < -STOP_OVERRUN_R


@dataclass(frozen=True)
class PerpGateReport:
    checks: tuple[GateCheck, ...]
    stop_failures: tuple[str, ...]  # position ids (overruns) and CLOSE_SHORT command ids

    @property
    def passed(self) -> bool:
        required = [c for c in self.checks if not c.informational]
        return bool(required) and all(c.passed for c in required)


def _fmt(x: float | Decimal | None, spec: str = ".2f") -> str:
    return "n/a" if x is None else format(float(x), spec)


def perp_gate(trades: Sequence[PerpTrade], *, liquidations: int, short_closes: Sequence[str], now: datetime,
              spot_mean_return_pct: float | None = None) -> PerpGateReport:
    closed = sorted(trades, key=lambda t: t.closed_at)
    first = min((t.opened_at for t in closed), default=None)
    days = (now - first) / timedelta(days=1) if first else 0.0
    rs = [float(t.r) for t in closed if t.r is not None]
    mean_r = sum(rs) / len(rs) if rs else None
    unplanned = len(closed) - len(rs)
    overruns = tuple(t.position_id for t in closed if t.stop_overrun)
    failures = overruns + tuple(short_closes)
    ci = mean_interval(rs) if len(rs) >= 2 else None

    checks = [
        GateCheck("closed_perp_trades", f">= {MIN_CLOSED_TRADES}", str(len(closed)), len(closed) >= MIN_CLOSED_TRADES),
        GateCheck("days_since_first_perp", f">= {MIN_DAYS} d", f"{days:.1f} d", days >= MIN_DAYS),
        GateCheck("liquidations", "0", str(liquidations), liquidations == 0),
        GateCheck("stop_failures", f"0 (loss beyond {STOP_OVERRUN_R} R, or CLOSE_SHORT)", str(len(failures)),
                  not failures),
        # every trade must be measurable in R: an entry without a plan cannot show the stop held
        GateCheck("trades_without_plan", "0", str(unplanned), unplanned == 0),
        GateCheck("mean_r", "> 0 (after fees and funding)", _fmt(mean_r, ".3f"), mean_r is not None and mean_r > 0),
        GateCheck("mean_r_interval", "lower bound > 0 (90%)", _fmt(ci[0], ".3f") if ci else "n/a",
                  ci is not None and ci[0] > 0, informational=True,
                  interval=(f"{ci[0]:.3f}", f"{ci[1]:.3f}") if ci else None),
        GateCheck("spot_mean_return_pct_same_window", "for comparison", _fmt(spot_mean_return_pct),
                  True, informational=True),
    ]
    by_grade = {g: [float(t.r) for t in closed if t.grade == g and t.r is not None] for g in GRADES}
    means = {g: (sum(v) / len(v) if v else None) for g, v in by_grade.items()}
    known = [means[g] for g in GRADES if means[g] is not None]
    ordered = len(known) >= 2 and all(a > b for a, b in zip(known, known[1:]))
    checks.append(GateCheck(
        "grades_ordered", "mean R: A > B > C (graded trades)",
        ", ".join(f"{g} {_fmt(means[g], '.2f')} (n={len(by_grade[g])})" for g in GRADES), ordered, informational=True))
    return PerpGateReport(tuple(checks), failures)
