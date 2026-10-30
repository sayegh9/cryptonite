"""Correlation-aware exposure across mandates (remediation Phase 8, F4).

Each mandate is limited on its own (``check_mandate``), but BTC and ETH move together (correlation ~0.8),
so several mandates can each stay inside their limits while the account as a whole carries far more BTC
risk than any single position may. This check caps the **combined beta-weighted BTC exposure** of every
mandate:

    exposure = Σ |notional_i| × |β_i|   over every mandate's open positions and working entry orders
               + worst-case notional of the new entry × |β_order|
    refused  when exposure > cap_pct × account equity

- ``β_i`` is the beta of the instrument's 1h returns to BTC's over the last 30 days of the analytics lake
  (``beta_from_closes``; BTC itself is 1). Absolute values: a short or a negative beta is not allowed to
  offset another mandate's risk (conservative; spot is long-only anyway).
- When beta cannot be computed (no lake, too few aligned bars, flat BTC), β = ``FALLBACK_BETA`` = 1.0.
  For the instruments the mandates trade (BTC, ETH) that counts the position at its full notional.
  Caveat: ETH's measured beta to BTC is often a little above 1 (≈1.0–1.3), so 1.0 is close to, not
  strictly above, its real beta; the cap itself (25% of equity, the single-order clamp) carries the margin.
- The cap defaults to ``DEFAULT_CROSS_MANDATE_EXPOSURE_PCT`` (25%) and can only be lowered (F7).

The check runs in the orchestrator's AI order path right after ``check_mandate`` and before
``GuardedOrderDispatcher``. It only refuses; it never resizes, never replaces either check, and never
refuses a risk-reducing order (spot SELL, reduce-only perp). Its refusal, ``CROSS_MANDATE_EXPOSURE``, is
not a mandate breach: no mandate overstepped its own limits.
"""

import math
from collections.abc import Iterable, Mapping
from decimal import Decimal

from cryptonite_schemas.profile import symbol_of

from cryptonite_execution.risk_limits import DEFAULT_CROSS_MANDATE_EXPOSURE_PCT

CROSS_MANDATE_EXPOSURE = "CROSS_MANDATE_EXPOSURE"
FALLBACK_BETA = Decimal("1")
BETA_WINDOW_DAYS = 30
BETA_TIMEFRAME = "1h"
BAR_SECONDS = 3600
# A week of aligned hourly returns at least; fewer and the estimate is noise -> fallback
MIN_ALIGNED_RETURNS = 7 * 24
BENCHMARK_SYMBOL = "BTCUSDT"

__all__ = [
    "BENCHMARK_SYMBOL", "BETA_WINDOW_DAYS", "CROSS_MANDATE_EXPOSURE", "DEFAULT_CROSS_MANDATE_EXPOSURE_PCT",
    "FALLBACK_BETA", "MIN_ALIGNED_RETURNS", "beta_from_closes", "beta_symbol", "beta_weighted_exposure",
    "check_cross_mandate_exposure",
]


def beta_symbol(instrument_id: str) -> str:
    """The lake symbol whose returns stand for an instrument: ``ETHUSDT.BINANCE`` and
    ``ETHUSDT-PERP.BINANCE`` -> ``ETHUSDT``."""
    return symbol_of(instrument_id)


def _hourly_returns(closes: Iterable[tuple[int, float]], bar_ns: int) -> dict[int, float]:
    """ts -> simple return from the bar one interval earlier; a return across a gap is not formed."""
    series = sorted((int(ts), float(px)) for ts, px in closes if px is not None and float(px) > 0)
    out: dict[int, float] = {}
    for (t0, p0), (t1, p1) in zip(series, series[1:]):
        if t1 - t0 == bar_ns:
            out[t1] = p1 / p0 - 1.0
    return out


def beta_from_closes(asset: Iterable[tuple[int, float]], benchmark: Iterable[tuple[int, float]], *,
                     bar_seconds: int = BAR_SECONDS, min_returns: int = MIN_ALIGNED_RETURNS) -> Decimal | None:
    """OLS beta of the asset's bar returns on the benchmark's: cov(r_a, r_b) / var(r_b), over the
    timestamps where both have a return. ``(ts_ns, close)`` pairs, any order. ``None`` when fewer than
    ``min_returns`` aligned returns exist or the benchmark did not move (the caller then uses
    ``FALLBACK_BETA``)."""
    bar_ns = bar_seconds * 1_000_000_000
    ra, rb = _hourly_returns(asset, bar_ns), _hourly_returns(benchmark, bar_ns)
    common = sorted(ra.keys() & rb.keys())
    if len(common) < max(min_returns, 2):
        return None
    xa = [ra[t] for t in common]
    xb = [rb[t] for t in common]
    n = len(common)
    ma, mb = sum(xa) / n, sum(xb) / n
    var_b = sum((b - mb) ** 2 for b in xb) / (n - 1)
    if var_b <= 0 or not math.isfinite(var_b):
        return None
    cov = sum((a - ma) * (b - mb) for a, b in zip(xa, xb)) / (n - 1)
    beta = cov / var_b
    if not math.isfinite(beta):
        return None
    return Decimal(repr(round(beta, 6)))


def instrument_beta(betas: Mapping[str, Decimal], instrument_id: str) -> Decimal:
    sym = beta_symbol(instrument_id)
    if sym == BENCHMARK_SYMBOL:
        return Decimal("1")
    beta = betas.get(sym)
    return abs(beta) if beta is not None else FALLBACK_BETA


def beta_weighted_exposure(holdings: Mapping[str, Decimal], betas: Mapping[str, Decimal]) -> Decimal:
    """Σ |usd| × |β| over instrument -> USD exposure (positions and working entries)."""
    return sum((abs(Decimal(str(usd))) * instrument_beta(betas, inst) for inst, usd in holdings.items()), Decimal("0"))


def check_cross_mandate_exposure(holdings: Mapping[str, Decimal], order_instrument: str, order_notional: Decimal,
                                 betas: Mapping[str, Decimal], equity: Decimal,
                                 cap_pct: Decimal = DEFAULT_CROSS_MANDATE_EXPOSURE_PCT) -> str | None:
    """None when the entry fits under the cap, else the refusal text (starting with
    ``CROSS_MANDATE_EXPOSURE``). ``holdings``: every mandate's exposure by instrument, in USD; the caller
    passes only risk-adding orders. ``cap_pct`` is never above the default (F7 can only lower it)."""
    cap_pct = min(Decimal(str(cap_pct)), DEFAULT_CROSS_MANDATE_EXPOSURE_PCT)
    if equity is None or equity <= 0:
        return f"{CROSS_MANDATE_EXPOSURE}: account equity is not positive"
    before = beta_weighted_exposure(holdings, betas)
    added = abs(Decimal(str(order_notional))) * instrument_beta(betas, order_instrument)
    cap = equity * cap_pct / Decimal("100")
    if before + added > cap:
        return (f"{CROSS_MANDATE_EXPOSURE}: beta-weighted BTC exposure across mandates would be "
                f"${before + added:,.0f} (${before:,.0f} held + ${added:,.0f} new), over the cap of "
                f"${cap:,.0f} ({_plain(cap_pct)}% of equity ${equity:,.0f})")
    return None


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")
