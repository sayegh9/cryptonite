"""Research validation (remediation Phase 5): history guard, anchored walk-forward folds, the Deflated
Sharpe Ratio and parameter robustness. Pure functions; the loop (``loop.py``) does the backtests.

- ``check_history`` (R5.0): a spec whose warm-up + in-sample + out-of-sample + holdout reaches before
  the contiguous catalog history (or well past its end) is refused with a clear reason, instead of a
  ``DataGapError`` or a silently shortened window mid-run.
- ``anchored_folds`` (R5.1): the in-sample window in 5 equal segments; fold k (1..4) is scored on
  segment k, with all earlier data as its anchored history, so the objective sees 4 periods in time.
- ``deflated_sharpe_ratio`` (R5.2, Bailey & Lopez de Prado 2014): the probability that the selected
  Sharpe beats the best Sharpe expected from ``n_trials`` unskilled trials with the observed variance.
- ``perturbations`` (R5.5): each best parameter moved by +-10%, one at a time.
"""

import math
from statistics import NormalDist
from typing import Any

DAY_NS = 86_400 * 10**9
PERIODS_PER_YEAR = 365  # backtest Sharpe ratios are annualized from daily returns
EULER_GAMMA = 0.5772156649015329
N_FOLDS = 4
END_SLACK_NS = DAY_NS  # the catalog may trail "now" by the ingest cadence


class InsufficientHistory(ValueError):
    pass


def check_history(windows: list[tuple[int, int, int]], coverage: dict[str, Any]) -> None:
    """Raise ``InsufficientHistory`` when the windows need data the catalog does not have.

    ``coverage``: ``{"start_ns", "end_ns"}`` of the contiguous history ending at the latest bar
    (``GET /api/v1/backtests/coverage``); ``start_ns`` None means no data.
    """
    need_start = min(w[0] for w in windows)
    need_end = max(w[2] for w in windows)
    have_start, have_end = coverage.get("start_ns"), coverage.get("end_ns")
    need_days = (need_end - need_start) / DAY_NS
    if have_start is None or have_end is None:
        raise InsufficientHistory(f"insufficient history: no bars in the catalog for {coverage.get('bar_type')}; "
                                  f"the spec needs {need_days:.0f} days (warm-up + in-sample + out-of-sample + holdout)")
    have_days = (have_end - have_start) / DAY_NS
    if need_start < have_start or need_end > have_end + END_SLACK_NS:
        raise InsufficientHistory(
            f"insufficient history: the spec needs {need_days:.0f} days (warm-up + in-sample + out-of-sample + "
            f"holdout) but {coverage.get('bar_type')} has {have_days:.0f} contiguous days; backfill more history "
            f"(scripts/backfill_history.py) or shorten the windows")


def anchored_folds(window: tuple[int, int, int], n_folds: int = N_FOLDS) -> list[tuple[int, int, int]]:
    """(warmup_start, start, end) of each fold's scoring segment inside the in-sample ``window``. The
    window's evaluation span is cut into ``n_folds + 1`` segments; segment 0 is history only. Each fold
    keeps the window's warm-up length before its own start."""
    warmup, start, end = window
    seg = (end - start) // (n_folds + 1)
    warm = start - warmup
    folds = []
    for k in range(1, n_folds + 1):
        s = start + k * seg
        e = end if k == n_folds else start + (k + 1) * seg - 1
        folds.append((s - warm, s, e))
    return folds


def fold_objective(sharpes: list[float]) -> float:
    """Mean fold Sharpe minus half its standard deviation: rewards stable over lucky."""
    mean = sum(sharpes) / len(sharpes)
    std = math.sqrt(sum((s - mean) ** 2 for s in sharpes) / (len(sharpes) - 1)) if len(sharpes) > 1 else 0.0
    return mean - 0.5 * std


def deflated_sharpe_ratio(sharpe: float, trial_sharpes: list[float], n_trials: int, n_obs: int,
                          skew: float = 0.0, kurtosis: float = 3.0,
                          periods_per_year: int = PERIODS_PER_YEAR) -> float | None:
    """DSR of an annualized ``sharpe`` selected from ``n_trials`` trials whose (annualized) Sharpes are
    ``trial_sharpes`` (their variance estimates the variance across all trials), over ``n_obs`` periods
    (days). Returns a probability in [0, 1]; None when it cannot be computed (n_obs < 2, no trials).
    Normal returns are assumed unless ``skew``/``kurtosis`` are given."""
    if n_obs < 2 or n_trials < 1 or not trial_sharpes:
        return None
    scale = math.sqrt(periods_per_year)
    sr = sharpe / scale
    srs = [s / scale for s in trial_sharpes]
    mean = sum(srs) / len(srs)
    var = sum((s - mean) ** 2 for s in srs) / (len(srs) - 1) if len(srs) > 1 else 0.0
    z = NormalDist()
    if n_trials > 1 and var > 0:
        sr0 = math.sqrt(var) * ((1 - EULER_GAMMA) * z.inv_cdf(1 - 1 / n_trials)
                                + EULER_GAMMA * z.inv_cdf(1 - 1 / (n_trials * math.e)))
    else:
        sr0 = 0.0
    denom = 1 - skew * sr + (kurtosis - 1) / 4 * sr * sr
    if denom <= 0:
        return None
    return z.cdf((sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom))


def perturbations(params: dict[str, Any], space: list[Any], pct: float = 0.10) -> list[tuple[str, float, dict[str, Any]]]:
    """(name, factor, parameters) for each numeric parameter moved by -pct and +pct (ints by at least 1),
    clamped to the parameter space; moves that change nothing are skipped."""
    bounds = {p.name: p for p in space}
    out = []
    for name, value in params.items():
        p = bounds.get(name)
        if p is None or not isinstance(value, int | float):
            continue
        for factor in (1 - pct, 1 + pct):
            if p.kind == "int":
                moved = round(value * factor)
                if moved == value:
                    moved = value + (1 if factor > 1 else -1)
                moved = int(min(max(moved, int(p.low)), int(p.high)))
            else:
                moved = min(max(value * factor, p.low), p.high)
            if moved != value:
                out.append((name, factor, {**params, name: moved}))
    return out
