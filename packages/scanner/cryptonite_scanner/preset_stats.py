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


@dataclass
class _OpenTrade:
    """Small per-position state carried while replaying bounded bar batches."""

    entry_ns: int
    entry: float
    stop: float
    side: str
    risk: float
    remaining: float = 1.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    best: float = 0.0
    active_stop: float = 0.0
    tp1_done: bool = False

    @classmethod
    def open(cls, entry_ns: int, entry: float, stop: float, side: str) -> _OpenTrade | None:
        """Create an open state only for a correctly sided structural stop."""
        risk = abs(entry - stop)
        long = side == "LONG"
        if risk <= 0 or (long and stop >= entry) or (not long and stop <= entry):
            return None
        return cls(
            entry_ns=entry_ns,
            entry=entry,
            stop=stop,
            side=side,
            risk=risk,
            fees=entry * ENTRY_FEE_RATE,
            best=entry,
            active_stop=stop,
        )

    def process_bar(self, row: dict[str, Any], event_ns: int) -> ReplayTrade | None:
        """Apply one completed bar using adverse-first ordering for ambiguous OHLC paths.

        Existing stops are checked before targets. A trail created from this bar's favorable extreme
        becomes active on the next bar because OHLC does not establish whether its high/low came first.
        """
        long = self.side == "LONG"
        high, low = float(row["high"]), float(row["low"])
        hard_stop = self.entry * (
            1 - GLOBAL_STOP_LOSS_PCT / 100 if long else 1 + GLOBAL_STOP_LOSS_PCT / 100
        )
        effective_stop = (
            max(self.stop, hard_stop, self.active_stop)
            if long
            else min(self.stop, hard_stop, self.active_stop)
        )
        if (low <= effective_stop) if long else (high >= effective_stop):
            return self._close(event_ns, effective_stop)

        favorable = high if long else low
        self.best = max(self.best, favorable) if long else min(self.best, favorable)
        gain_pct = (
            (self.best / self.entry - 1) * 100 if long else (self.entry / self.best - 1) * 100
        )
        if gain_pct >= GLOBAL_TRAIL_ACTIVATE_PCT:
            trail = self.best * (1 - GLOBAL_TRAIL_PCT / 100 if long else 1 + GLOBAL_TRAIL_PCT / 100)
            self.active_stop = (
                max(self.active_stop, trail) if long else min(self.active_stop, trail)
            )

        target = self.entry + PROFILE_TP_R * self.risk * (1 if long else -1)
        target_hit = high >= target if long else low <= target
        if not self.tp1_done and target_hit:
            scaled = PROFILE_TP1_SCALE_PCT / 100
            self.gross_pnl += scaled * (target - self.entry) * (1 if long else -1)
            self.fees += scaled * target * EXIT_FEE_RATE
            self.remaining -= scaled
            self.tp1_done = True
        return None

    def finish(self, event_ns: int, price: float) -> ReplayTrade:
        """Close any remaining quantity at the last observed close."""
        return self._close(event_ns, price)

    def _close(self, event_ns: int, price: float) -> ReplayTrade:
        """Settle the remaining quantity and return its net result in initial-risk units."""
        direction = 1 if self.side == "LONG" else -1
        self.gross_pnl += self.remaining * (price - self.entry) * direction
        self.fees += self.remaining * price * EXIT_FEE_RATE
        self.remaining = 0.0
        return ReplayTrade(self.entry_ns, event_ns, (self.gross_pnl - self.fees) / self.risk)


def _trade_exit(
    bars: list[dict[str, Any]],
    start_index: int,
    entry: float,
    stop: float,
    side: str,
) -> tuple[int, float]:
    """Simulate default profile exits from bars after the signal close, charging each filled exit fee."""
    state = _OpenTrade.open(
        int(bars[start_index].get("ts_event_ns", start_index)), entry, stop, side
    )
    if state is None:
        return start_index, entry
    for index in range(start_index + 1, len(bars)):
        event_ns = int(bars[index].get("ts_event_ns", index))
        trade = state.process_bar(bars[index], event_ns)
        if trade is not None:
            return index, trade.net_r
    final_index = len(bars) - 1
    final_trade = state.finish(
        int(bars[final_index].get("ts_event_ns", final_index)), float(bars[final_index]["close"])
    )
    return final_index, final_trade.net_r


def _requires_htf(preset: ScanSpecCreate) -> bool:
    """Return whether any rule reads an aligned higher-timeframe field."""
    return any(
        condition.field.startswith("htf_")
        or (isinstance(condition.value, str) and condition.value.startswith("htf_"))
        for group in preset.rules
        for condition in group.conditions
    )


def _prepare_bars(bars: pl.DataFrame, timeframe: str) -> pl.DataFrame:
    """Add the scanner timestamp contract to lake bars, whose event timestamps are close inclusive."""
    if bars.is_empty():
        return bars
    if "ts_event_ns" not in bars.columns:
        raise ValueError("bars must contain ts_event_ns")
    step_ns = _timeframe_ns(timeframe)
    prepared = bars.sort("ts_event_ns")
    if "bar_open_ns" not in prepared.columns:
        prepared = prepared.with_columns(
            (pl.col("ts_event_ns") - step_ns + 1_000_000).alias("bar_open_ns")
        )
    if "interval_end_exclusive_ns" not in prepared.columns:
        prepared = prepared.with_columns(
            (pl.col("ts_event_ns") + 1_000_000).alias("interval_end_exclusive_ns")
        )
    if "bar_close_inclusive_ns" not in prepared.columns:
        prepared = prepared.with_columns(pl.col("ts_event_ns").alias("bar_close_inclusive_ns"))
    return prepared


def _evaluate_candidates(
    bars: pl.DataFrame,
    preset: ScanSpecCreate,
    coin: str,
    timeframe: str,
    htf_bars: pl.DataFrame | None,
    price_precision: int,
):
    """Evaluate one bounded base-bar frame and its closed HTF context with the production scanner engine."""
    if bars.is_empty() or (_requires_htf(preset) and (htf_bars is None or htf_bars.is_empty())):
        return [], bars
    prepared = _prepare_bars(bars, timeframe)
    prepared_htf = (
        _prepare_bars(htf_bars, _higher_timeframe(timeframe)) if htf_bars is not None else None
    )
    candidates = ScannerEngine.evaluate(
        prepared,
        preset.rules,
        preset.risk,
        instrument_id=f"{coin.upper()}.BINANCE",
        timeframe=timeframe,
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        price_precision=price_precision,
        htf_df=prepared_htf,
    )
    return candidates, prepared


def replay_entries(
    bars: pl.DataFrame,
    preset: ScanSpecCreate,
    coin: str,
    timeframe: str,
    *,
    price_precision: int = 8,
    htf_bars: pl.DataFrame | None = None,
) -> list[ReplayTrade]:
    """Run a scanner preset on closed bars in bounded chunks and apply the default profile exits.

    Bars must include OHLCV and ``ts_event_ns``. They may include the scanner's derived timestamp
    columns; those are supplied when absent. Funding is omitted because this replay is spot-only.
    """
    if bars.is_empty() or bars.height < 2:
        return []
    prepared_htf = (
        _prepare_bars(htf_bars, _higher_timeframe(timeframe)) if htf_bars is not None else None
    )

    def load_htf(start_ns: int, end_ns: int) -> pl.DataFrame:
        if prepared_htf is None:
            return pl.DataFrame()
        return prepared_htf.filter(
            (pl.col("interval_end_exclusive_ns") >= start_ns)
            & (pl.col("interval_end_exclusive_ns") <= end_ns)
        )

    batches = (bars.slice(offset, 2048) for offset in range(0, bars.height, 2048))
    trades, _, _ = replay_batches(
        batches,
        preset,
        coin,
        timeframe,
        htf_loader=load_htf if prepared_htf is not None else None,
        price_precision=price_precision,
    )
    return trades


def replay_batches(
    batches,
    preset: ScanSpecCreate,
    coin: str,
    timeframe: str,
    *,
    htf_loader=None,
    price_precision: int = 8,
    context_bars: int = 300,
    initial_context: pl.DataFrame | None = None,
) -> tuple[list[ReplayTrade], int | None, int | None]:
    """Replay a bounded stream of bar batches, carrying indicator context and open trades forward.

    ``htf_loader(start_ns, end_ns)`` returns only closed higher-timeframe bars covering the batch and
    the rolling indicator warmup. At most one bar batch plus ``context_bars`` and compact open/trade
    states are retained in Python.
    """
    side = (preset.risk.direction or "LONG").upper()
    context = (
        _prepare_bars(initial_context, timeframe) if initial_context is not None else pl.DataFrame()
    )
    context_end_ns: int | None = int(context["ts_event_ns"][-1]) if not context.is_empty() else None
    first_ns = last_ns = None
    open_trades: list[_OpenTrade] = []
    closed_trades: list[ReplayTrade] = []
    final_row: dict[str, Any] | None = None

    for batch in batches:
        prepared_batch = _prepare_bars(batch, timeframe)
        if prepared_batch.is_empty():
            continue
        first_batch_ns = int(prepared_batch["ts_event_ns"][0])
        last_batch_ns = int(prepared_batch["ts_event_ns"][-1])
        first_ns = first_ns if first_ns is not None else first_batch_ns
        last_ns = last_batch_ns
        combined = (
            pl.concat([context, prepared_batch]) if not context.is_empty() else prepared_batch
        )

        htf = None
        if _requires_htf(preset) and htf_loader is not None:
            htf_tf = _higher_timeframe(timeframe)
            htf_step = _timeframe_ns(htf_tf)
            start_ns = int(combined["bar_open_ns"].min()) - context_bars * htf_step
            end_ns = int(combined["interval_end_exclusive_ns"].max())
            htf = htf_loader(start_ns, end_ns)

        candidates, prepared_combined = _evaluate_candidates(
            combined, preset, coin, timeframe, htf, price_precision
        )
        if context_end_ns is not None:
            candidates = [
                candidate
                for candidate in candidates
                if int(candidate.interval_end_exclusive_ns) > context_end_ns + 1_000_000
            ]
        candidate_rows: dict[int, list[Any]] = {}
        indexes = {
            int(value): index
            for index, value in enumerate(
                prepared_combined.select("interval_end_exclusive_ns").to_series()
            )
        }
        for candidate in candidates:
            index = indexes.get(int(candidate.interval_end_exclusive_ns))
            if index is not None:
                event_ns = int(prepared_combined[index, "ts_event_ns"])
                candidate_rows.setdefault(event_ns, []).append(candidate)

        new_rows = prepared_combined.filter(
            pl.col("ts_event_ns") > (context_end_ns or -1)
        ).to_dicts()
        for row in new_rows:
            event_ns = int(row["ts_event_ns"])
            remaining: list[_OpenTrade] = []
            for trade in open_trades:
                result = trade.process_bar(row, event_ns)
                if result is None:
                    remaining.append(trade)
                else:
                    closed_trades.append(result)
            open_trades = remaining
            for candidate in candidate_rows.get(event_ns, []):
                state = _OpenTrade.open(
                    event_ns,
                    float(candidate.suggested_entry),
                    float(candidate.suggested_stop),
                    side,
                )
                if state is not None:
                    open_trades.append(state)
            context_end_ns = event_ns
            final_row = row

        context = prepared_combined.tail(context_bars)

    if final_row is not None:
        final_ns = int(final_row["ts_event_ns"])
        final_close = float(final_row["close"])
        closed_trades.extend(trade.finish(final_ns, final_close) for trade in open_trades)
    return closed_trades, first_ns, last_ns


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


def _higher_timeframe(timeframe: str) -> str:
    """Match the production scanner's next-closed-timeframe context mapping (ADR 0011)."""
    higher = {"1m": "5m", "5m": "15m", "15m": "1h", "1h": "4h", "4h": "1d"}
    try:
        return higher[timeframe]
    except KeyError:
        raise ValueError(f"no higher timeframe mapping for {timeframe}") from None


def _iso_ns(value: int | None) -> str | None:
    """Convert nanoseconds since epoch to an ISO-8601 UTC timestamp."""
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC).isoformat()
