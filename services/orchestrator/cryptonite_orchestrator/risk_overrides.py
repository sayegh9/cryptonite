"""Operator risk-limit overrides and BTC betas, kept current in the orchestrator (remediation F7, F4).

- ``refresh_risk_limits``: reads ``risk_limit_overrides`` for this process's (environment, account) and
  sets the dispatcher's ``max_daily_loss_pct`` / ``max_position_concentration_pct`` to
  ``min(baseline, override)`` (``cryptonite_execution.risk_limits.apply_to_dispatcher``). The baseline
  is what the dispatcher had before the first override, capped at the code default, so an override can
  only tighten; removing one restores the baseline, never more. A failed read keeps what is applied.
  The F4 cap is kept here too (``STATE.cross_cap_pct``).
- ``refresh_betas``: hourly, the 30-day beta of each mandate instrument's 1h returns to BTC's, read from
  the analytics lake (``ANALYTICS_LAKE_PATH``). Missing data leaves the instrument out, and the F4 check
  then uses beta 1.0.

Both run from ``ops_monitor`` (start-up and every 60 s), which then publishes the applied values in
``risk:effective_limits``.
"""

import asyncio
import datetime
import logging
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from cryptonite_execution.cross_exposure import (
    BENCHMARK_SYMBOL,
    BETA_TIMEFRAME,
    BETA_WINDOW_DAYS,
    beta_from_closes,
    beta_symbol,
)
from cryptonite_execution.risk_limits import (
    DEFAULT_CROSS_MANDATE_EXPOSURE_PCT,
    DispatcherBaseline,
    RiskLimitValues,
    apply_to_dispatcher,
    cross_mandate_cap_pct,
)

logger = logging.getLogger("cryptonite.orchestrator.risk_overrides")

BETA_REFRESH_S = 3600
DEFAULT_LAKE_PATH = "/app/data/analytics_lake"


@dataclass
class RiskState:
    baseline: DispatcherBaseline | None = None
    overrides: RiskLimitValues = field(default_factory=RiskLimitValues)
    cross_cap_pct: Decimal = DEFAULT_CROSS_MANDATE_EXPOSURE_PCT
    overrides_loaded_at: float | None = None
    betas: dict[str, Decimal] = field(default_factory=dict)  # lake symbol -> beta to BTC
    betas_at: float | None = None
    last_applied_version: int = 0
    enforced_at: datetime.datetime | None = None


STATE = RiskState()


def environment() -> str:
    return os.getenv("ENVIRONMENT", "PAPER").upper()


async def load_overrides(conn, env: str, account_id: str) -> tuple[RiskLimitValues, int]:
    row = await conn.fetchrow(
        """SELECT max_daily_loss_pct, max_order_equity_pct, cross_mandate_exposure_pct, override_version
           FROM risk_limit_overrides WHERE environment = $1 AND account_id = $2""", env, account_id)
    if row is None:
        return RiskLimitValues(), 0
    return RiskLimitValues.from_row(row), int(row["override_version"]) if "override_version" in row else 1


def apply_overrides(engine, overrides: RiskLimitValues, state: RiskState = STATE, version: int = 0) -> bool:
    """Apply ``overrides`` to the engine's dispatcher (tighter only) and the F4 cap."""
    d = engine.dispatcher
    if state.baseline is None:
        state.baseline = DispatcherBaseline.capture(d)
    changed = apply_to_dispatcher(d, state.baseline, overrides)
    state.overrides = overrides
    state.cross_cap_pct = cross_mandate_cap_pct(overrides)
    if changed or version != state.last_applied_version:
        state.last_applied_version = version
        state.enforced_at = datetime.datetime.now(datetime.UTC)
        changed = True
    return changed


async def refresh_risk_limits(engine, pool, state: RiskState = STATE, env: str | None = None) -> RiskLimitValues | None:
    account_id = getattr(engine, "account_id", "PAPER-ACCOUNT-001")
    try:
        async with pool.acquire() as conn:
            overrides, version = await load_overrides(conn, env or environment(), account_id)
    except Exception as e:  # keep what is applied: a failed read must not loosen anything
        logger.error("Could not read risk_limit_overrides: %s", e)
        return None
    if apply_overrides(engine, overrides, state, version):
        d = engine.dispatcher
        logger.warning("Risk limits applied: max_daily_loss_pct=%s max_order_equity_pct=%s cross_mandate_cap=%s%% (v%s)",
                       d.max_daily_loss_pct, d.max_position_concentration_pct, state.cross_cap_pct, state.last_applied_version)
    state.overrides_loaded_at = time.time()
    return overrides


def _closes(query_engine, symbol: str, start_ns: int, end_ns: int) -> list[tuple[int, float]]:
    bars = query_engine.query_bars_df(symbol=symbol, timeframe=BETA_TIMEFRAME, start_ns=start_ns, end_ns=end_ns)
    if bars is None or bars.is_empty() or "ts_event_ns" not in bars.columns or "close" not in bars.columns:
        return []
    return list(zip(bars["ts_event_ns"].to_list(), [float(c) for c in bars["close"].to_list()]))


def compute_betas(query_engine, symbols: set[str], now: datetime.datetime | None = None) -> dict[str, Decimal]:
    """Beta to BTC per lake symbol over the last ``BETA_WINDOW_DAYS`` of 1h bars; symbols without
    enough data are left out (the F4 check then uses 1.0)."""
    now = now or datetime.datetime.now(datetime.UTC)
    end_ns = int(now.timestamp() * 1_000_000_000)
    start_ns = end_ns - BETA_WINDOW_DAYS * 86_400 * 1_000_000_000
    btc = _closes(query_engine, BENCHMARK_SYMBOL, start_ns, end_ns)
    betas: dict[str, Decimal] = {}
    for sym in sorted(symbols - {BENCHMARK_SYMBOL}):
        try:
            beta = beta_from_closes(_closes(query_engine, sym, start_ns, end_ns), btc)
        except Exception as e:
            logger.warning("Beta for %s unavailable: %s", sym, e)
            beta = None
        if beta is not None:
            betas[sym] = beta
    return betas


async def refresh_betas(pool, state: RiskState = STATE, *, query_engine=None, force: bool = False,
                        now_s: float | None = None) -> dict[str, Decimal]:
    """Recompute the betas when due (hourly). On failure the old betas are dropped after two periods,
    so a stale number never lingers (the check falls back to 1.0)."""
    now_s = now_s if now_s is not None else time.time()
    if not force and state.betas_at is not None and now_s - state.betas_at < BETA_REFRESH_S:
        return state.betas
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT unnest(instruments) AS inst FROM ai_mandates WHERE status <> 'ARCHIVED'")
        symbols = {beta_symbol(r["inst"]) for r in rows}
        if query_engine is None:
            from cryptonite_storage.queries import AnalyticsQueryEngine
            query_engine = AnalyticsQueryEngine(Path(os.getenv("ANALYTICS_LAKE_PATH", DEFAULT_LAKE_PATH)))
        state.betas = await asyncio.to_thread(compute_betas, query_engine, symbols)
        state.betas_at = now_s
        logger.info("Betas to BTC (%dd of %s returns): %s", BETA_WINDOW_DAYS, BETA_TIMEFRAME,
                    {k: str(v) for k, v in state.betas.items()} or "none (fallback 1.0)")
    except Exception as e:
        logger.error("Could not compute betas: %s", e)
        if state.betas_at is not None and now_s - state.betas_at > 2 * BETA_REFRESH_S:
            state.betas = {}
    return state.betas
