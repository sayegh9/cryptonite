"""Stop/target labels for scanner candidates: walk 1m bars from the candidate's interval end and
record which barrier (stop or target) is touched first, in R multiples of the stop distance."""

import datetime
import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any

import polars as pl
from outcomes import MIN_COVERAGE

logger = logging.getLogger(__name__)

HOLD_HOURS = {"5m": 12, "15m": 24, "1h": 72, "4h": 168}
NO_DATA_AFTER = datetime.timedelta(days=7)


def _dec(x: float | None) -> Decimal | None:
    return None if x is None else Decimal(str(x))


def barrier_outcome(bars: pl.DataFrame | list[dict], direction: str | None, entry: float | None,
                    stop: float | None, target: float | None) -> dict[str, Any]:
    """First bar to touch stop or target decides; a bar touching both counts as STOP; no touch is
    EXPIRED with the signed last close in R. ``bars`` need ordered high / low / close."""
    if entry is None or stop is None or target is None or entry == stop:
        return {"outcome": "INVALID", "direction": None, "realized_r": None, "mfe_r": None, "mae_r": None,
                "bars_to_exit": None}
    long = stop < entry
    if direction is None:
        direction = "LONG" if long else "SHORT"
    long = direction == "LONG"
    risk = abs(entry - stop)
    sign = 1.0 if long else -1.0
    rows = bars.to_dicts() if isinstance(bars, pl.DataFrame) else bars
    mfe = mae = 0.0
    last_close = entry
    for n, b in enumerate(rows, start=1):
        hi, lo = float(b["high"]), float(b["low"])
        fav, adv = ((hi - entry), (lo - entry)) if long else ((entry - lo), (entry - hi))
        mfe, mae = max(mfe, fav / risk), min(mae, adv / risk)
        hit_stop = lo <= stop if long else hi >= stop
        hit_target = hi >= target if long else lo <= target
        if hit_stop:
            return {"outcome": "STOP", "direction": direction, "realized_r": sign * (stop - entry) / risk,
                    "mfe_r": mfe, "mae_r": mae, "bars_to_exit": n}
        if hit_target:
            return {"outcome": "TARGET", "direction": direction, "realized_r": sign * (target - entry) / risk,
                    "mfe_r": mfe, "mae_r": mae, "bars_to_exit": n}
        last_close = float(b["close"])
    return {"outcome": "EXPIRED", "direction": direction, "realized_r": sign * (last_close - entry) / risk,
            "mfe_r": mfe, "mae_r": mae, "bars_to_exit": len(rows)}


async def label_candidates(pool, lake, limit: int = 300, now: datetime.datetime | None = None) -> int:
    """Label up to ``limit`` unlabelled candidates whose hold has elapsed. One lake query per symbol."""
    now = now or datetime.datetime.now(datetime.UTC)
    now_ns = int(now.timestamp() * 1_000_000_000)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT sc.id, sc.instrument_id, sc.timeframe, sc.interval_end_exclusive_ns,
                      sc.suggested_entry, sc.suggested_stop, sc.suggested_target
               FROM signal_candidates sc
               LEFT JOIN candidate_outcomes co ON co.candidate_id = sc.id
               WHERE co.candidate_id IS NULL AND sc.timeframe = ANY($1::text[])
                 AND sc.interval_end_exclusive_ns + (CASE sc.timeframe WHEN '5m' THEN 12 WHEN '15m' THEN 24
                       WHEN '1h' THEN 72 ELSE 168 END)::bigint * 3600000000000 <= $2
               ORDER BY sc.interval_end_exclusive_ns
               LIMIT $3""", list(HOLD_HOURS), now_ns, limit)
    by_symbol: dict[str, list[tuple[Any, int, int]]] = defaultdict(list)
    for r in rows:
        start = int(r["interval_end_exclusive_ns"])
        by_symbol[r["instrument_id"].split(".")[0]].append((r, start, start + HOLD_HOURS[r["timeframe"]] * 3_600_000_000_000))
    out: list[tuple] = []
    for symbol, items in by_symbol.items():
        valid = [i for i in items if i[0]["suggested_entry"] != i[0]["suggested_stop"]]
        bars = None
        if valid:
            try:
                bars = lake.query_bars_df(symbol, "1m", min(i[1] for i in valid), max(i[2] for i in valid))
            except Exception as e:  # one bad symbol must not stop the rest
                logger.warning("Candidate labels: lake refused %s (%s)", symbol, e)
                continue
            if bars is not None and not bars.is_empty():
                bars = bars.sort("ts_event_ns")
        for r, start, end in items:
            entry, stop, target = (float(r[k]) for k in ("suggested_entry", "suggested_stop", "suggested_target"))
            hold_h = HOLD_HOURS[r["timeframe"]]
            if entry == stop:
                res = barrier_outcome([], None, entry, stop, target)
            else:
                w = (bars.filter((pl.col("ts_event_ns") > start) & (pl.col("ts_event_ns") <= end))
                     if bars is not None and not bars.is_empty() else None)
                if w is None or w.height < hold_h * 60 * MIN_COVERAGE:
                    if datetime.datetime.fromtimestamp(end / 1e9, datetime.UTC) + NO_DATA_AFTER < now:
                        res = {"outcome": "NO_DATA", "direction": None, "realized_r": None, "mfe_r": None,
                               "mae_r": None, "bars_to_exit": None}
                    else:
                        continue
                else:
                    res = barrier_outcome(w, None, entry, stop, target)
            out.append((r["id"], res["direction"], _dec(entry), _dec(stop), _dec(target), res["outcome"],
                        _dec(res["realized_r"]), _dec(res["mfe_r"]), _dec(res["mae_r"]), res["bars_to_exit"], hold_h))
    if out:
        async with pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO candidate_outcomes (candidate_id, direction, entry, stop, target, outcome,
                          realized_r, mfe_r, mae_r, bars_to_exit, max_hold_hours)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) ON CONFLICT (candidate_id) DO NOTHING""", out)
        logger.info("Labelled %d candidate outcomes", len(out))
    return len(out)
