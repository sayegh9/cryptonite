"""H15 A2: the Brain's ``broad`` scan of one coin: every catalog preset on every catalog timeframe, on closed bars only.

Pure over a frame of 1m bars and a clock, so a past moment can be replayed. Too little history or a stale feed is reported
as UNAVAILABLE for that scope, never as a verdict: the EMAs return values on any window, so a short window would
otherwise give a wrong reading rather than an error.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import polars as pl
from cryptonite_schemas.profile import PRESET_CATALOG

from cryptonite_scanner.conditional import conditional_item, expires_at, required_bars
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.models import RiskConfig, compute_spec_hash
from cryptonite_scanner.presets import preset_by_slug
from cryptonite_scanner.resampler import TIMEFRAME_MINUTES, resample_1m_bars

SCAN_LOOKBACK_BARS = 300  # the live scanner's formula, in bars of the largest timeframe evaluated
PARENT_1D_BARS = 70  # a 4h preset reads a daily EMA-50; 300 daily bars would mean loading 300 days of 1m bars per coin
LOOKBACK_MINUTES = max(SCAN_LOOKBACK_BARS * TIMEFRAME_MINUTES["4h"], PARENT_1D_BARS * TIMEFRAME_MINUTES["1d"])
PARENT = {"15m": "1h", "1h": "4h", "4h": "1d"}
FRESH_BARS = 2  # a match counts on the newest two closed bars
MAX_ENTRY_DRIFT = 0.025  # as the swing scan: an entry this far from the last close is stale
STALE_AFTER_BARS = 2
INSUFFICIENT_WARMUP = "INSUFFICIENT_WARMUP"
STALE_DATA = "STALE_DATA"


@dataclass
class CoinScan:
    frames: dict[str, pl.DataFrame] = field(default_factory=dict)
    matches: list[dict[str, Any]] = field(default_factory=list)
    conditional: list[dict[str, Any]] = field(default_factory=list)
    unavailable: list[dict[str, Any]] = field(default_factory=list)
    rejections: list[dict[str, str]] = field(default_factory=list)


def closed_frames(df_1m: pl.DataFrame, now_ns: int) -> dict[str, pl.DataFrame]:
    """Each timeframe's complete bars that have closed by ``now_ns``, enriched. A bar still open is never read."""
    if "bar_open_ns" not in df_1m.columns and "ts_event_ns" in df_1m.columns:
        df_1m = df_1m.with_columns(bar_open_ns=pl.col("ts_event_ns") - 60_000_000_000)
    frames: dict[str, pl.DataFrame] = {}
    for tf in ("15m", "1h", "4h", "1d"):
        bars = resample_1m_bars(df_1m, tf).filter(pl.col("interval_end_exclusive_ns") <= now_ns)
        if not bars.is_empty():
            frames[tf] = ScannerEngine.enrich_indicators(bars)
    return frames


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, UTC).isoformat()


def _unavailable(instrument_id: str, slug: str, timeframe: str, reason: str, detail: str) -> dict[str, Any]:
    return {"instrument_id": instrument_id, "preset_slug": slug, "timeframe": timeframe,
            "status": "UNAVAILABLE", "reason": f"UNAVAILABLE: {reason}", "detail": detail}


def _scope_problem(frame: pl.DataFrame | None, parent: pl.DataFrame | None, own_need: int, parent_need: int, bar_ns: int,
                   now_ns: int) -> tuple[str, str] | None:
    if frame is None or frame.height < own_need:
        return INSUFFICIENT_WARMUP, f"{0 if frame is None else frame.height} of {own_need} closed bars"
    if parent_need and (parent is None or parent.height < parent_need):
        return INSUFFICIENT_WARMUP, f"{0 if parent is None else parent.height} of {parent_need} closed parent bars"
    ends = frame["interval_end_exclusive_ns"].tail(FRESH_BARS).to_list()
    if now_ns - int(ends[-1]) > STALE_AFTER_BARS * bar_ns:
        return STALE_DATA, f"newest closed bar ended {_iso(int(ends[-1]))}"
    if len(ends) == FRESH_BARS and int(ends[1]) - int(ends[0]) != bar_ns:
        return INSUFFICIENT_WARMUP, "a gap in the newest bars"  # a cross compares two neighbouring bars
    return None


def _preset_version(preset: Any) -> str:
    return compute_spec_hash(preset.universe.model_dump(mode="json"),
                             [g.model_dump(mode="json") for g in preset.rules])[:12]


def _precision(price: float) -> int:
    return 6 if price < 0.1 else (4 if price < 10 else 2)


def scan_coin(instrument_id: str, df_1m: pl.DataFrame, now_ns: int) -> CoinScan:
    """Evaluate one coin at ``now_ns``. A failure on one scope reports that scope, never the others."""
    out = CoinScan(frames=closed_frames(df_1m, now_ns))
    for slug, meta in PRESET_CATALOG.items():
        preset = preset_by_slug(slug)
        side = str(getattr(meta.side, "value", meta.side)).upper()
        own_need, parent_need = required_bars(preset.rules)
        for tf in meta.timeframes:
            frame, parent = out.frames.get(tf), out.frames.get(PARENT[tf])
            bar_ns = TIMEFRAME_MINUTES[tf] * 60_000_000_000
            problem = _scope_problem(frame, parent, own_need, parent_need, bar_ns, now_ns)
            if problem is not None:
                out.unavailable.append(_unavailable(instrument_id, slug, tf, *problem))
                continue
            assert frame is not None
            try:
                _evaluate_scope(out, instrument_id, slug, side, tf, preset, frame, parent, bar_ns, now_ns)
            except Exception as e:
                out.unavailable.append(_unavailable(instrument_id, slug, tf, "EVALUATION_FAILED", type(e).__name__))
    return out


def _evaluate_scope(out: CoinScan, instrument_id: str, slug: str, side: str, tf: str, preset: Any, frame: pl.DataFrame,
                    parent: pl.DataFrame | None, bar_ns: int, now_ns: int) -> None:
    tail = frame.tail(FRESH_BARS + 1)
    latest = float(tail["close"][-1])
    prec = _precision(latest)
    newest_close = int(tail["bar_close_inclusive_ns"][-1])
    oldest_fresh = int(tail["bar_close_inclusive_ns"].tail(FRESH_BARS)[0])
    matched = ScannerEngine.evaluate(
        df=tail, rule_groups=preset.rules, risk_config=preset.risk or RiskConfig(min_risk_reward=1.8),
        instrument_id=instrument_id, timeframe=tf, spec_version_id=uuid.uuid4(), scan_run_id=uuid.uuid4(),
        price_precision=prec, htf_df=parent, filter_min_rr=False)
    fresh = [c for c in matched if c.bar_close_inclusive_ns >= oldest_fresh
             and abs(float(c.suggested_entry) - latest) / latest <= MAX_ENTRY_DRIFT]
    if fresh:
        cand = max(fresh, key=lambda c: c.bar_close_inclusive_ns)
        entry, stop, target = (round(float(v), prec) for v in
                               (cand.suggested_entry, cand.suggested_stop, cand.suggested_target))
        risk = abs(entry - stop)
        out.matches.append({"instrument_id": instrument_id, "preset_slug": slug, "timeframe": tf, "direction": side,
                            "entry": entry, "stop": stop, "target": target,
                            "rr_ratio": round(abs(target - entry) / risk, 2) if risk > 0 else 0.0,
                            "matched_rules": list(cand.matched_rules[:4]), "source": "live_scan",
                            "bar_close": _iso(cand.bar_close_inclusive_ns)})
        return
    out.rejections.append({"coin": instrument_id.split(".")[0], "preset_slug": slug, "timeframe": tf,
                           "outcome": "STALE" if matched else "NO_TRIGGER"})
    joined = ScannerEngine.join_htf(frame.tail(2), parent)
    item = conditional_item(joined, preset.rules)
    if item is not None:
        out.conditional.append({
            "instrument_id": instrument_id, "direction": side, "timeframe": tf, "preset_slug": slug,
            "preset_version": _preset_version(preset), "status": "CONDITIONAL",
            "as_of": _iso(now_ns), "bar_close": _iso(newest_close), "expires_at": expires_at(newest_close, bar_ns),
            **item})
