"""Interval statistics for the autonomy gates (remediation Phase 4, R4.1).

A gate that compares a point estimate with a threshold passes on luck: 30 fair-coin trades show a
positive mean about half the time. These helpers give the interval around the estimate, so a check can
ask for its lower bound instead.

- ``wilson_interval``: binomial proportion (win rate), well behaved at small n and near 0 or 1;
- ``stationary_bootstrap``: Politis-Romano resampling in blocks of random (geometric) length with mean
  ``block`` (default about sqrt(n)), which keeps the serial dependence of trade or daily returns;
  always seeded, so the same data give the same interval;
- ``mean_interval`` (expectancy per trade after fees) and ``sortino_diff_interval`` (strategy Sortino
  minus BTC-hold Sortino, resampling both series with the same indices so the days stay paired).

Intervals are two-sided at ``confidence`` (90% by default: the lower bound is a one-sided 95% bound).
"""

import math
import random
from collections.abc import Callable, Sequence
from statistics import NormalDist

CONFIDENCE = 0.90
N_BOOT = 2000
SEED = 20261005


def wilson_interval(successes: int, n: int, confidence: float = CONFIDENCE) -> tuple[float, float] | None:
    """Wilson score interval of ``successes / n``; None when n is 0."""
    if n <= 0:
        return None
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _stationary_indices(n: int, rng: random.Random, mean_block: float) -> list[int]:
    """One stationary-bootstrap resample of range(n): blocks start at random points, each next index
    continues the block (wrapping around) with probability 1 - 1/mean_block."""
    p_new = 1.0 / mean_block
    idx: list[int] = []
    i = rng.randrange(n)
    while len(idx) < n:
        idx.append(i)
        i = rng.randrange(n) if rng.random() < p_new else (i + 1) % n
    return idx


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def stationary_bootstrap(
    stat: Callable[[list[int]], float | None],
    n: int,
    *,
    confidence: float = CONFIDENCE,
    n_boot: int = N_BOOT,
    block: float | None = None,
    seed: int = SEED,
) -> tuple[float, float] | None:
    """Percentile interval of ``stat(indices)`` over stationary-bootstrap resamples of ``range(n)``.
    Resamples where the statistic is undefined (None or not finite) are dropped; None if n < 2 or
    fewer than half the resamples give a number."""
    if n < 2:
        return None
    rng = random.Random(seed)
    mean_block = max(1.0, block if block is not None else math.sqrt(n))
    vals = []
    for _ in range(n_boot):
        v = stat(_stationary_indices(n, rng, mean_block))
        if v is not None and math.isfinite(v):
            vals.append(v)
    if len(vals) < n_boot / 2:
        return None
    vals.sort()
    alpha = (1 - confidence) / 2
    return _quantile(vals, alpha), _quantile(vals, 1 - alpha)


def mean_interval(values: Sequence[float], **kwargs) -> tuple[float, float] | None:
    """Interval of the mean (e.g. expectancy: mean trade return after fees)."""
    vals = list(values)
    return stationary_bootstrap(lambda idx: sum(vals[i] for i in idx) / len(idx), len(vals), **kwargs)


def sortino(returns: Sequence[float], periods_per_year: int = 365) -> float | None:
    """Annualised Sortino (target 0); None with < 2 returns or no downside (undefined, not infinite)."""
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    dd = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / len(returns))
    return mean / dd * math.sqrt(periods_per_year) if dd > 0 else None


def sortino_diff_interval(
    returns: Sequence[float], benchmark_returns: Sequence[float], periods_per_year: int = 365, **kwargs
) -> tuple[float, float] | None:
    """Interval of Sortino(strategy) - Sortino(benchmark) over the same periods (paired resampling)."""
    r, b = list(returns), list(benchmark_returns)
    if len(r) != len(b):
        return None

    def diff(idx: list[int]) -> float | None:
        s, sb = sortino([r[i] for i in idx], periods_per_year), sortino([b[i] for i in idx], periods_per_year)
        return None if s is None or sb is None else s - sb

    return stationary_bootstrap(diff, len(r), **kwargs)
