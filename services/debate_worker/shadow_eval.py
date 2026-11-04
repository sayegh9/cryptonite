"""H13d: the pass mark of the shadow prompt, fixed before any data exists (docs/adr/0012). Standard library only.

``evaluate`` only returns a verdict: nothing reads it to change a prompt, a route or a gate. Going live is a later operator
decision and a separate change. Do not change these constants based on results.
"""

import bisect
import datetime
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

PROMPT_VERSION = "shadow-v1"

MIN_ROWS = 100
MIN_DAYS = 21
MIN_REGIME_ROWS = 30
MIN_REGIME_TAKES = 10
TAKE_RATE_MIN = 0.10
TAKE_RATE_MAX = 0.70
MIN_EDGE_R = 0.20
BOOTSTRAP_SEED = 20261104
BOOTSTRAP_RESAMPLES = 5000
BOOTSTRAP_LOWER_QUANTILE = 0.10  # the one-sided 90% lower bound

NOT_ENOUGH_DATA, FAIL, PASS = "NOT_ENOUGH_DATA", "FAIL", "PASS"
UP, DOWN = "UP", "DOWN"
REGIMES = (UP, DOWN)
PRIMING = "HISTORICAL_PRIMING"
_TAKE_SIDES = {("BUY", "LONG"), ("SELL", "SHORT")}
_REGIME_WINDOW_NS = 24 * 3600 * 10**9
_REGIME_TOLERANCE_NS = 2 * 3600 * 10**9


@dataclass(frozen=True)
class Row:
    day: datetime.date  # UTC day of the signal
    state: str
    prompt_version: str
    recommendation: str | None
    direction: str | None  # the setup's own: LONG / SHORT
    net_r: float | None  # candidate_outcomes.net_r_live
    regime: str | None  # UP / DOWN; None when the 24h change is flat or cannot be measured
    trigger_reason: str | None = None
    live_decision: str | None = None  # policy B, for reference only


@dataclass(frozen=True)
class Verdict:
    result: str
    reasons: list[str]
    stats: dict[str, Any] = field(default_factory=dict)


def is_take(row: Row) -> bool:
    """A BUY or SELL in the setup's direction. A HOLD, or a verdict against the setup, is a SKIP."""
    return (row.recommendation, row.direction) in _TAKE_SIDES


def usable(row: Row, prompt_version: str = PROMPT_VERSION) -> bool:
    return (row.state == "OK" and row.prompt_version == prompt_version and row.net_r is not None
            and row.trigger_reason != PRIMING)


def _mean(values: Sequence[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _figures(rows: Sequence[Row], take) -> dict[str, Any]:
    takes = [r.net_r for r in rows if take(r)]
    skips = [r.net_r for r in rows if not take(r)]
    mean_take, mean_skip = _mean(takes), _mean(skips)
    return {"rows": len(rows), "takes": len(takes), "skips": len(skips), "mean_take": mean_take, "mean_skip": mean_skip,
            "edge": mean_take - mean_skip if takes and skips else None}


def bootstrap_lower_bound(rows: Iterable[Row], *, seed: int = BOOTSTRAP_SEED, resamples: int = BOOTSTRAP_RESAMPLES,
                          quantile: float = BOOTSTRAP_LOWER_QUANTILE) -> float | None:
    """Lower bound of mean TAKE minus mean SKIP, resampling whole UTC days: setups overlap, so rows are not independent.
    A resample with no TAKE or no SKIP has no difference and is dropped; None when none has one."""
    by_day: dict[datetime.date, tuple[list[float], list[float]]] = {}
    for r in rows:
        by_day.setdefault(r.day, ([], []))[0 if is_take(r) else 1].append(r.net_r)
    # fsum: the database returns rows in no fixed order, and the same rows must always give the same bound
    days = [(math.fsum(t), len(t), math.fsum(s), len(s)) for t, s in (by_day[d] for d in sorted(by_day))]
    if not days:
        return None
    rng = random.Random(seed)
    edges: list[float] = []
    for _ in range(resamples):
        take_sum, takes, skip_sum, skips = 0.0, 0, 0.0, 0
        for ts, tn, ss, sn in rng.choices(days, k=len(days)):
            take_sum, takes, skip_sum, skips = take_sum + ts, takes + tn, skip_sum + ss, skips + sn
        if takes and skips:
            edges.append(take_sum / takes - skip_sum / skips)
    if not edges:
        return None
    edges.sort()
    return edges[int(quantile * len(edges))]


def _reference(rows: Sequence[Row]) -> dict[str, Any]:
    """The live policy's figure on the same setups: shown next to the verdict, never part of it."""
    decided = [r for r in rows if r.live_decision in ("TAKE", "SKIP")]
    return _figures(decided, lambda r: r.live_decision == "TAKE")


def _r(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.3f}R"


def _failures(stats: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    rate = stats["take_rate"]
    if not TAKE_RATE_MIN <= rate <= TAKE_RATE_MAX:
        failures.append(f"TAKE rate {rate:.1%} is outside {TAKE_RATE_MIN:.0%}-{TAKE_RATE_MAX:.0%}")
    if stats["overall"]["mean_take"] is None or stats["overall"]["mean_take"] <= 0:
        failures.append(f"mean net R of TAKE {_r(stats['overall']['mean_take'])} is not above 0")
    groups = [("overall", stats["overall"])] + [(f"regime {g}", stats["regimes"][g]) for g in REGIMES]
    for name, figures in groups:
        if figures["edge"] is None or figures["edge"] < MIN_EDGE_R:
            failures.append(f"TAKE minus SKIP {_r(figures['edge'])} {name} is below {MIN_EDGE_R:.2f}R")
    if stats["lower_bound"] is None or stats["lower_bound"] <= 0:
        failures.append(f"90% lower bound of TAKE minus SKIP {_r(stats['lower_bound'])} is not above 0")
    return failures


def evaluate(rows: Iterable[Row], prompt_version: str = PROMPT_VERSION) -> Verdict:
    """NOT_ENOUGH_DATA, FAIL or PASS with its reasons. Every condition must hold for PASS.

    A TAKE rate outside the band fails as soon as the row counts are met, without waiting for the TAKE counts: a filter
    that never says yes would otherwise never reach them and stay NOT_ENOUGH_DATA for ever.
    """
    used = [r for r in rows if usable(r, prompt_version)]
    regime_rows = {g: [r for r in used if r.regime == g] for g in REGIMES}
    days = len({r.day for r in used})
    overall = _figures(used, is_take)
    stats: dict[str, Any] = {
        "prompt_version": prompt_version, "days": days, "overall": overall,
        "take_rate": overall["takes"] / len(used) if used else 0.0,
        "regimes": {g: _figures(regime_rows[g], is_take) for g in REGIMES},
        "live_reference": _reference(used), "lower_bound": None,
    }
    short_rows = []
    if len(used) < MIN_ROWS:
        short_rows.append(f"{len(used)} labelled rows, need {MIN_ROWS}")
    if days < MIN_DAYS:
        short_rows.append(f"{days} calendar days, need {MIN_DAYS}")
    short_rows += [f"{len(regime_rows[g])} labelled rows in regime {g}, need {MIN_REGIME_ROWS}"
                   for g in REGIMES if len(regime_rows[g]) < MIN_REGIME_ROWS]
    short_takes = [f"{stats['regimes'][g]['takes']} TAKEs in regime {g}, need {MIN_REGIME_TAKES}"
                   for g in REGIMES if stats["regimes"][g]["takes"] < MIN_REGIME_TAKES]
    if short_rows:
        return Verdict(NOT_ENOUGH_DATA, short_rows + short_takes, stats)
    stats["lower_bound"] = bootstrap_lower_bound(used)
    if short_takes and TAKE_RATE_MIN <= stats["take_rate"] <= TAKE_RATE_MAX:
        return Verdict(NOT_ENOUGH_DATA, short_takes, stats)
    failures = _failures(stats)
    if failures:
        return Verdict(FAIL, failures, stats)
    return Verdict(PASS, [f"{len(used)} rows over {days} days: every condition holds"], stats)


def regime_at(bars: Sequence[tuple[int, float]], signal_ns: int) -> str | None:
    """The sign of the coin's 24h close change at the signal, from 1h (close time ns, close) bars sorted by time. None when
    the change is flat or a bar is missing within two hours of either end."""
    stamps = [ts for ts, _ in bars]
    at = bisect.bisect_right(stamps, signal_ns) - 1
    if at < 0 or signal_ns - stamps[at] > _REGIME_TOLERANCE_NS:
        return None
    cutoff = stamps[at] - _REGIME_WINDOW_NS
    before = bisect.bisect_right(stamps, cutoff) - 1
    if before < 0 or cutoff - stamps[before] > _REGIME_TOLERANCE_NS:
        return None
    now, then = bars[at][1], bars[before][1]
    return UP if now > then else DOWN if now < then else None
