"""Pure preset replay and summary functions (ADR 0011, P1.9 research-only replay)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Any
from uuid import uuid4

import polars as pl

from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.models import ScanSpecCreate

ENTRY_FEE_RATE = 0.001
EXIT_FEE_RATE = 0.001
PROFILE_TP_R = 2.0
PROFILE_TP1_SCALE_PCT = 50.0
GLOBAL_STOP_LOSS_PCT = 5.0
GLOBAL_TRAIL_ACTIVATE_PCT = 3.0
GLOBAL_TRAIL_PCT = 2.0


@dataclass(frozen=True)
class ReplayTrade:
    """A single fully closed simulated trade, expressed in initial-risk units."""

    entry_ns: int
    exit_ns: int
    net_r: float


def _trade_exit(
    bars: list[dict[str, Any]],
    start_index: int,
    entry: float,
    stop: float,
    side: str,
) -> tuple[int, float]:
    """Simulate default profile exits from bars after the signal close, charging each filled exit fee."""
    risk = abs(entry - stop)
    long = side == "LONG"
    if risk <= 0 or (long and stop >= entry) or (not long and stop <= entry):
        return start_index, entry

    tp1 = entry + PROFILE_TP_R * risk * (1 if long else -1)
    hard_stop = entry * (1 - GLOBAL_STOP_LOSS_PCT / 100 if long else 1 + GLOBAL_STOP_LOSS_PCT / 100)
    remaining = 1.0
    gross_pnl = 0.0
    fees = entry * ENTRY_FEE_RATE
    best = entry
    active_stop = stop

    for index in range(start_index + 1, len(bars)):
        bar = bars[index]
        high, low = float(bar["high"]), float(bar["low"])

        # Stops are checked before targets when one OHLC bar touches both levels.
        effective_stop = (
            max(stop, hard_stop, active_stop) if long else min(stop, hard_stop, active_stop)
        )
        stop_hit = low <= effective_stop if long else high >= effective_stop
        if stop_hit:
            exit_price = effective_stop
            gross_pnl += remaining * (exit_price - entry) * (1 if long else -1)
            fees += remaining * exit_price * EXIT_FEE_RATE
            return index, (gross_pnl - fees) / risk

        # The trail only moves after observing the bar's favorable extreme. It can trigger on later bars.
        favorable = high if long else low
        best = max(best, favorable) if long else min(best, favorable)
        gain_pct = (best / entry - 1) * 100 if long else (entry / best - 1) * 100
        if gain_pct >= GLOBAL_TRAIL_ACTIVATE_PCT:
            trail = best * (1 - GLOBAL_TRAIL_PCT / 100 if long else 1 + GLOBAL_TRAIL_PCT / 100)
            active_stop = max(active_stop, trail) if long else min(active_stop, trail)

        tp_hit = high >= tp1 if long else low <= tp1
        if remaining == 1.0 and tp_hit:
            scaled = PROFILE_TP1_SCALE_PCT / 100
            gross_pnl += scaled * (tp1 - entry) * (1 if long else -1)
            fees += scaled * tp1 * EXIT_FEE_RATE
            remaining -= scaled

        # A trail reached within the bar closes the remainder, after TP1 is accounted for.
        trail_hit = low <= active_stop if long else high >= active_stop
        if remaining > 0 and gain_pct >= GLOBAL_TRAIL_ACTIVATE_PCT and trail_hit:
            gross_pnl += remaining * (active_stop - entry) * (1 if long else -1)
            fees += remaining * active_stop * EXIT_FEE_RATE
            return index, (gross_pnl - fees) / risk

    final_index = len(bars) - 1
    final_price = float(bars[final_index]["close"])
    gross_pnl += remaining * (final_price - entry) * (1 if long else -1)
    fees += remaining * final_price * EXIT_FEE_RATE
    return final_index, (gross_pnl - fees) / risk


def replay_entries(
    bars: pl.DataFrame,
    preset: ScanSpecCreate,
    coin: str,
    timeframe: str,
    *,
    price_precision: int = 8,
) -> list[ReplayTrade]:
    """Run a scanner preset on closed bars and replay each engine entry with default profile exits.

    Bars must include OHLCV and ``ts_event_ns``. They may include the scanner's derived timestamp
    columns; those are supplied when absent. Funding is omitted because this replay is spot-only.
    """
    if bars.is_empty() or bars.height < 2:
        return []
    if "ts_event_ns" not in bars.columns:
        raise ValueError("bars must contain ts_event_ns")

    step_ns = _timeframe_ns(timeframe)
    prepared = bars.sort("ts_event_ns")
    if "bar_open_ns" not in prepared.columns:
        prepared = prepared.with_columns((pl.col("ts_event_ns") - step_ns).alias("bar_open_ns"))
    if "interval_end_exclusive_ns" not in prepared.columns:
        prepared = prepared.with_columns(
            (pl.col("ts_event_ns") + 1).alias("interval_end_exclusive_ns")
        )
    if "bar_close_inclusive_ns" not in prepared.columns:
        prepared = prepared.with_columns(pl.col("ts_event_ns").alias("bar_close_inclusive_ns"))

    candidates = ScannerEngine.evaluate(
        prepared,
        preset.rules,
        preset.risk,
        instrument_id=f"{coin.upper()}.BINANCE",
        timeframe=timeframe,
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        price_precision=price_precision,
    )
    rows = prepared.to_dicts()
    by_end = {int(row["interval_end_exclusive_ns"]): index for index, row in enumerate(rows)}
    side = (preset.risk.direction or "LONG").upper()
    trades: list[ReplayTrade] = []

    for candidate in candidates:
        start_index = by_end.get(int(candidate.interval_end_exclusive_ns))
        if start_index is None or start_index >= len(rows) - 1:
            continue
        entry, stop = float(candidate.suggested_entry), float(candidate.suggested_stop)
        exit_index, net_r = _trade_exit(rows, start_index, entry, stop, side)
        trades.append(
            ReplayTrade(
                int(rows[start_index]["ts_event_ns"]), int(rows[exit_index]["ts_event_ns"]), net_r
            )
        )
    return trades


def summarize_replay(
    preset_slug: str,
    coin: str,
    trades: list[ReplayTrade],
    first_bar_ns: int | None,
    last_bar_ns: int | None,
) -> dict[str, Any]:
    """Return the stable JSON row for one preset and coin, including net R statistics."""
    returns = [trade.net_r for trade in sorted(trades, key=lambda item: item.exit_ns)]
    equity = peak = drawdown = 0.0
    for result in returns:
        equity += result
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "preset": preset_slug,
        "coin": coin.upper(),
        "trades": len(returns),
        "win_rate": sum(result > 0 for result in returns) / len(returns) if returns else 0.0,
        "avg_r": sum(returns) / len(returns) if returns else 0.0,
        "median_r": median(returns) if returns else 0.0,
        "max_drawdown_r": drawdown,
        "first_bar": _iso_ns(first_bar_ns),
        "last_bar": _iso_ns(last_bar_ns),
    }


def report_document(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap results in the versioned report schema, recording the replay assumptions."""
    return {
        "schema_version": 1,
        "assumptions": {
            "market": "spot",
            "funding": "ignored",
            "fee_per_side": ENTRY_FEE_RATE,
            "profile_stop_loss_pct": GLOBAL_STOP_LOSS_PCT,
            "profile_tp_r": PROFILE_TP_R,
            "profile_tp1_scale_pct": PROFILE_TP1_SCALE_PCT,
            "global_trail_activate_pct": GLOBAL_TRAIL_ACTIVATE_PCT,
            "global_trail_pct": GLOBAL_TRAIL_PCT,
        },
        "results": results,
    }


def _timeframe_ns(timeframe: str) -> int:
    """Convert scanner timeframe syntax (for example ``15m`` or ``4h``) to nanoseconds."""
    unit = timeframe[-1:]
    amount = int(timeframe[:-1])
    multipliers = {"m": 60, "h": 3600, "d": 86400}
    if amount <= 0 or unit not in multipliers:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    return amount * multipliers[unit] * 1_000_000_000


def _iso_ns(value: int | None) -> str | None:
    """Convert nanoseconds since epoch to an ISO-8601 UTC timestamp."""
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC).isoformat()
