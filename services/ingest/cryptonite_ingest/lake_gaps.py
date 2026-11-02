"""Find and fill missing 1m bars in the Analytics Lake from Binance REST klines (H7b).

Debate structure (``services/debate_worker/structure.py``) resamples 1m lake bars and needs the trailing 200
decision bars contiguous, so a single lost minute refuses every debate for days. Live gap recovery covers the
minutes after its checkpoint; this module covers the rest: the in-ingest sweep fills recent holes, the
``repair-gaps`` command fills N days once, and ``check-continuity`` reports what ``structure.py`` would see.

Safe alongside live ingestion:
- only minutes that are missing AND older than the newest lake bar of the window are written. Live ingest only
  ever appends after its checkpoint, which is at or behind that bar, so the two never write the same minute;
- the lake is read again under the lake maintenance lock right before each write and minutes that appeared
  meanwhile are dropped, so a rerun, the sweep and the repair never duplicate a bar or change an existing one;
- only closed klines on the minute grid are written.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
from cryptonite_scanner.resampler import TIMEFRAME_MINUTES, resample_1m_bars
from cryptonite_schemas.bar import EnrichedBar, compute_payload_checksum
from cryptonite_schemas.identifiers import to_nautilus_bar_type, to_spot_instrument_id
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.lake_lock import LakeLockBusy, lake_maintenance_lock
from cryptonite_storage.queries import AnalyticsQueryEngine

logger = logging.getLogger("cryptonite.lake_gaps")

MINUTE_NS = 60 * 1_000_000_000
REST_PAGE = 1000  # klines per REST call (weight 2 on Binance spot)
REST_PAUSE_S = 0.25  # between calls: 10 days x 5 symbols stays far below the 6000/min weight cap
# (decision, higher) timeframes per profile style, as in structure.levels_from_lake
STRUCTURE_STYLES = {"SWING": ("1h", "4h"), "INTRADAY": ("15m", "1h")}
STRUCTURE_BARS = 200
STRUCTURE_LOOKBACK_HIGHER_BARS = 230


def _fmt(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime("%Y-%m-%d %H:%M")


def missing_minutes(present: Iterable[int], start_ns: int, end_ns: int) -> list[int]:
    """Minute close stamps in [start_ns, end_ns] (grid-aligned) that ``present`` lacks."""
    have = set(present)
    t = start_ns + (-start_ns) % MINUTE_NS
    out = []
    while t <= end_ns:
        if t not in have:
            out.append(t)
        t += MINUTE_NS
    return out


def runs(stamps: list[int], step_ns: int = MINUTE_NS) -> list[tuple[int, int]]:
    """Sorted stamps as contiguous (first, last) runs."""
    out: list[tuple[int, int]] = []
    for t in sorted(stamps):
        if out and t - out[-1][1] == step_ns:
            out[-1] = (out[-1][0], t)
        else:
            out.append((t, t))
    return out


def fetch_windows(missing: list[int]) -> list[tuple[int, int]]:
    """Group missing close stamps into REST windows of at most one page each, as (first, last) close stamps."""
    windows: list[tuple[int, int]] = []
    for t in sorted(missing):
        if windows and t <= windows[-1][0] + (REST_PAGE - 1) * MINUTE_NS:
            windows[-1] = (windows[-1][0], t)
        else:
            windows.append((t, t))
    return windows


@dataclass
class FillResult:
    symbol: str
    missing: int = 0
    filled: int = 0
    rest_calls: int = 0
    unfilled: list[tuple[int, int]] = field(default_factory=list)  # runs REST had no closed kline for
    skipped: str = ""  # why nothing was written (lock busy), empty otherwise

    def describe(self) -> str:
        if self.skipped:
            return f"{self.symbol}: skipped ({self.skipped})"
        text = f"{self.symbol}: {self.missing} missing, {self.filled} filled, {self.rest_calls} REST call(s)"
        if self.unfilled:
            text += "; still missing " + ", ".join(f"{_fmt(a)}..{_fmt(b)}" for a, b in self.unfilled[:10])
        return text


class LakeGapFiller:
    """Fills interior 1m holes of the Analytics Lake from REST klines."""

    def __init__(self, rest_client, lake_path: str | Path, *, pause_s: float = REST_PAUSE_S) -> None:
        self.rest = rest_client
        self.lake_path = Path(lake_path)
        self.writer = AnalyticsLakeWriter(self.lake_path)
        self.pause_s = pause_s

    def _present(self, symbol: str, start_ns: int, end_ns: int) -> set[int]:
        engine = AnalyticsQueryEngine(self.lake_path)
        try:
            return engine.bar_close_stamps(symbol, "1m", start_ns, end_ns)
        finally:
            engine._con.close()

    async def fill(
        self,
        symbol: str,
        start_ns: int,
        end_ns: int,
        price_precision: int,
        size_precision: int,
        now_ns: int | None = None,
    ) -> FillResult:
        """Fill every missing minute in [start_ns, end_ns] that is older than the window's newest lake bar."""
        symbol = symbol.upper()
        result = FillResult(symbol)
        now_ns = now_ns if now_ns is not None else time.time_ns()
        end_ns = min(end_ns, now_ns // MINUTE_NS * MINUTE_NS)  # closed minutes only
        present = await asyncio.to_thread(self._present, symbol, start_ns, end_ns)
        if not present:
            return result  # nothing to anchor on: the live stream / start-up recovery owns an empty lake
        newest = max(present)
        missing = missing_minutes(present, start_ns, newest - MINUTE_NS)
        result.missing = len(missing)
        if not missing:
            return result

        wanted = set(missing)
        bars: dict[int, EnrichedBar] = {}
        inst_id = to_spot_instrument_id(symbol)
        bar_type = str(to_nautilus_bar_type(inst_id, "1m"))
        for first, last in fetch_windows(missing):
            if result.rest_calls:
                await asyncio.sleep(self.pause_s)
            result.rest_calls += 1
            # Binance selects klines by OPEN time; a lake bar is stamped at its close, one minute later
            raw = await self.rest.fetch_klines(
                symbol=symbol,
                interval="1m",
                start_time_ms=(first - MINUTE_NS) // 1_000_000,
                end_time_ms=(last - MINUTE_NS) // 1_000_000,
                limit=REST_PAGE,
            )
            arrival_ns = time.time_ns()
            for k in raw:
                if not k.is_closed:
                    continue
                bar = EnrichedBar.from_binance_raw(
                    raw=k,
                    instrument_id=inst_id,
                    bar_type=bar_type,
                    price_precision=price_precision,
                    size_precision=size_precision,
                    received_at_ns=arrival_ns,
                )
                if bar.ts_event_ns in wanted and bar.ts_event_ns <= end_ns:
                    bars[bar.ts_event_ns] = bar

        if bars:
            try:
                result.filled = await asyncio.to_thread(self._write_new, symbol, start_ns, newest, bars)
            except LakeLockBusy:
                result.skipped = "lake maintenance lock busy"
                return result
        result.unfilled = runs([t for t in missing if t not in bars])
        return result

    def _write_new(self, symbol: str, start_ns: int, newest_ns: int, bars: dict[int, EnrichedBar]) -> int:
        """Under the maintenance lock: re-read the lake and write only bars still missing, one file per UTC day."""
        with lake_maintenance_lock(self.lake_path):
            present = self._present(symbol, start_ns, newest_ns)
            new = [bars[t] for t in sorted(bars) if t not in present]
            by_day: dict[str, list[EnrichedBar]] = {}
            for b in new:
                by_day.setdefault(_fmt(b.ts_event_ns)[:10], []).append(b)
            for day_bars in by_day.values():  # the writer partitions a file by its first bar's date
                batch_id = str(uuid.uuid4())
                checksum = compute_payload_checksum(day_bars)
                for b in day_bars:
                    b.ingest_batch_id = batch_id
                    b.payload_checksum = checksum
                self.writer.write_batch(
                    bars=day_bars, venue="BINANCE", market_type="SPOT", timeframe="1m", symbol=symbol,
                    batch_id=batch_id,
                )
            return len(new)


@dataclass
class ContinuityResult:
    symbol: str
    style: str
    decision_tf: str
    higher_tf: str
    ok: bool
    problems: list[str] = field(default_factory=list)


def check_continuity(lake_path: str | Path, symbol: str, style: str, now_ns: int | None = None) -> ContinuityResult:
    """What ``structure.levels_from_lake`` checks: 200 contiguous closed decision bars, both latest bars fresh."""
    decision_tf, higher_tf = STRUCTURE_STYLES[style]
    res = ContinuityResult(symbol.upper(), style, decision_tf, higher_tf, ok=False)
    now_ns = now_ns if now_ns is not None else time.time_ns()
    higher_ns = TIMEFRAME_MINUTES[higher_tf] * MINUTE_NS
    step_ns = TIMEFRAME_MINUTES[decision_tf] * MINUTE_NS
    engine = AnalyticsQueryEngine(lake_path)
    try:
        bars = engine.query_bars_df(symbol, "1m", now_ns - STRUCTURE_LOOKBACK_HIGHER_BARS * higher_ns, now_ns)
    finally:
        engine._con.close()
    if bars.is_empty():
        res.problems.append("no 1m lake bars")
        return res
    decision = resample_1m_bars(bars, decision_tf).filter(pl.col("interval_end_exclusive_ns") <= now_ns)
    higher = resample_1m_bars(bars, higher_tf).filter(pl.col("interval_end_exclusive_ns") <= now_ns)
    if decision.is_empty() or higher.is_empty():
        res.problems.append("no complete closed decision or higher-timeframe bars")
        return res
    ends = decision.tail(STRUCTURE_BARS)["interval_end_exclusive_ns"].to_list()
    if len(ends) < STRUCTURE_BARS:
        res.problems.append(f"only {len(ends)} complete {decision_tf} bars")
    holes = [(a, b) for a, b in zip(ends, ends[1:]) if b - a != step_ns]
    for a, b in holes:
        res.problems.append(
            f"{(b - a) // step_ns - 1} {decision_tf} bar(s) missing in {_fmt(a)}..{_fmt(b - step_ns)} UTC"
        )
    if holes:  # name the 1m minutes behind the incomplete decision bars
        present = set(bars["ts_event_ns"].cast(pl.Int64).to_list())
        lost = missing_minutes(present, holes[0][0] + MINUTE_NS, holes[-1][1] - step_ns)
        shown = ", ".join(f"{_fmt(a)}..{_fmt(b)}" for a, b in runs(lost)[:20])
        res.problems.append(f"1m close stamps missing: {shown}" + (" ..." if len(runs(lost)) > 20 else ""))
    if now_ns - ends[-1] > 2 * step_ns:
        res.problems.append(f"latest {decision_tf} bar ends {_fmt(ends[-1])} UTC: older than two bars")
    if now_ns - int(higher["interval_end_exclusive_ns"][-1]) > 2 * higher_ns:
        res.problems.append(f"latest {higher_tf} bar is older than two bars")
    res.ok = not res.problems
    return res
