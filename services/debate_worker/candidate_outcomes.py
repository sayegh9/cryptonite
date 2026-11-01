"""Stop/target labels for scanner candidates: walk 1m bars from the candidate's interval end and
record which barrier (stop or target) is touched first, in R multiples of the stop distance.

Phase 2 (migration 057) adds realistic net labels: entry at the open of the bar after the signal,
the preset stop/target and the profile exits each net of cost model v1 (``cryptonite_scanner.costs``)."""

import datetime
import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any

import polars as pl
from cryptonite_scanner.costs import (
    cost_in_r,
    cost_model_name,
    default_market,
    round_trip_cost_fraction,
)

logger = logging.getLogger(__name__)

HOLD_HOURS = {"5m": 12, "15m": 24, "1h": 72, "4h": 168}
NO_DATA_AFTER = datetime.timedelta(days=7)
HOUR_NS = 3_600_000_000_000

# Default profile exits (P2 plan): 5% hard stop, 2% trailing stop once the trade is +3%, max hold.
PROFILE_STOP_PCT = 5.0
PROFILE_TRAIL_ACTIVATE_PCT = 3.0
PROFILE_TRAIL_PCT = 2.0

EMPTY_NET: dict[str, Any] = {"entry_next_open": None, "net_r_preset": None, "net_r_profile": None,
                             "cost_model": None, "preset_bars": None, "profile_bars": None}


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


def profile_exit_outcome(bars: pl.DataFrame | list[dict], direction: str, entry: float | None, *,
                         stop_pct: float = PROFILE_STOP_PCT, trail_activate_pct: float = PROFILE_TRAIL_ACTIVATE_PCT,
                         trail_pct: float = PROFILE_TRAIL_PCT, max_bars: int | None = None) -> dict[str, Any]:
    """Walk ordered high / low / close bars under the profile exits: a hard stop ``stop_pct`` from entry,
    and once the best price is ``trail_activate_pct`` in favour a stop trailing ``trail_pct`` behind it.
    The stop in force at the start of a bar is checked before that bar's extreme moves the trail, so a
    bar that both hits the stop and makes a new best exits at the old stop. No exit within ``max_bars``
    is EXPIRED at the last close. Returns ``outcome`` (STOP / TRAIL / EXPIRED / INVALID / NO_DATA),
    ``exit_price`` and ``bars_to_exit``."""
    if entry is None or entry <= 0:
        return {"outcome": "INVALID", "exit_price": None, "bars_to_exit": None}
    rows = bars.to_dicts() if isinstance(bars, pl.DataFrame) else list(bars)
    if max_bars is not None:
        rows = rows[:max_bars]
    if not rows:
        return {"outcome": "NO_DATA", "exit_price": None, "bars_to_exit": None}
    long = (direction or "LONG").upper() != "SHORT"
    sign = 1.0 if long else -1.0
    active = entry * (1 - sign * stop_pct / 100)
    activate_at = entry * (1 + sign * trail_activate_pct / 100)
    best = entry
    trailing = False
    for n, b in enumerate(rows, start=1):
        hi, lo = float(b["high"]), float(b["low"])
        if (lo <= active) if long else (hi >= active):
            return {"outcome": "TRAIL" if trailing else "STOP", "exit_price": active, "bars_to_exit": n}
        best = max(best, hi) if long else min(best, lo)
        if not trailing and ((best >= activate_at) if long else (best <= activate_at)):
            trailing = True
        if trailing:
            trail = best * (1 - sign * trail_pct / 100)
            active = max(active, trail) if long else min(active, trail)
    return {"outcome": "EXPIRED", "exit_price": float(rows[-1]["close"]), "bars_to_exit": len(rows)}


def net_r(direction: str, entry: float, stop: float, exit_price: float, hold_hours: float,
          market: str | None = None) -> float | None:
    """Gross R of a fill at ``entry`` exiting at ``exit_price`` (R = entry-to-stop distance), minus the
    round-trip cost of ``market`` (default: spot for longs, perp for shorts) converted to R."""
    market = market or default_market(direction)
    cost = cost_in_r(entry, stop, round_trip_cost_fraction(market, hold_hours))
    if cost is None:
        return None
    sign = 1.0 if direction == "LONG" else -1.0
    return sign * (exit_price - entry) / abs(entry - stop) - cost


def net_labels(bars: pl.DataFrame | list[dict], direction: str | None, stop: float | None, target: float | None,
               bar_hours: float, max_bars: int | None = None) -> dict[str, Any]:
    """The 057 columns for one candidate. ``bars`` are the ordered bars after the signal (open / high /
    low / close); the first bar's open is the fill. The preset stop and target keep their prices, and R
    for both exit policies is the distance from that fill to the preset stop. A fill already beyond the
    stop (or target) leaves the net R of that policy NULL. ``preset_bars`` / ``profile_bars`` count the
    bars held, for callers that need the exit time."""
    rows = bars.to_dicts() if isinstance(bars, pl.DataFrame) else list(bars)
    if max_bars is not None:
        rows = rows[:max_bars]
    if not rows or stop is None or target is None or stop == target:
        return dict(EMPTY_NET)
    direction = (direction or ("LONG" if stop < target else "SHORT")).upper()
    market = default_market(direction)
    entry = float(rows[0]["open"])
    long = direction == "LONG"
    out = dict(EMPTY_NET, entry_next_open=entry, cost_model=cost_model_name(market))
    if not (stop < entry if long else stop > entry):
        return out
    if (entry < target) if long else (entry > target):
        res = barrier_outcome(rows, direction, entry, stop, target)
        exit_price = {"STOP": stop, "TARGET": target}.get(res["outcome"], float(rows[res["bars_to_exit"] - 1]["close"]))
        out["net_r_preset"] = net_r(direction, entry, stop, exit_price, res["bars_to_exit"] * bar_hours, market)
        out["preset_bars"] = res["bars_to_exit"]
    prof = profile_exit_outcome(rows, direction, entry)
    out["net_r_profile"] = net_r(direction, entry, stop, prof["exit_price"], prof["bars_to_exit"] * bar_hours, market)
    out["profile_bars"] = prof["bars_to_exit"]
    return out


def _symbol_bars(lake, symbol: str, start_ns: int, end_ns: int) -> pl.DataFrame | None:
    bars = lake.query_bars_df(symbol, "1m", start_ns, end_ns)
    return bars.sort("ts_event_ns") if bars is not None and not bars.is_empty() else None


def _window(bars: pl.DataFrame | None, start: int, end: int) -> pl.DataFrame | None:
    if bars is None:
        return None
    return bars.filter((pl.col("ts_event_ns") > start) & (pl.col("ts_event_ns") <= end))


async def label_candidates(pool, lake, limit: int = 300, now: datetime.datetime | None = None) -> int:
    """Label up to ``limit`` unlabelled candidates whose hold has elapsed. One lake query per symbol.
    Afterwards one batch of older labels without the 057 net columns is backfilled (oldest first)."""
    # Debate-worker module, imported here so the pure functions above import anywhere (walk-forward script).
    from outcomes import MIN_COVERAGE

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
        by_symbol[r["instrument_id"].split(".")[0]].append((r, start, start + HOLD_HOURS[r["timeframe"]] * HOUR_NS))
    out: list[tuple] = []
    for symbol, items in by_symbol.items():
        valid = [i for i in items if i[0]["suggested_entry"] != i[0]["suggested_stop"]]
        bars = None
        if valid:
            try:
                bars = _symbol_bars(lake, symbol, min(i[1] for i in valid), max(i[2] for i in valid))
            except Exception as e:  # one bad symbol must not stop the rest
                logger.warning("Candidate labels: lake refused %s (%s)", symbol, e)
                continue
        for r, start, end in items:
            entry, stop, target = (float(r[k]) for k in ("suggested_entry", "suggested_stop", "suggested_target"))
            hold_h = HOLD_HOURS[r["timeframe"]]
            net = EMPTY_NET
            if entry == stop:
                res = barrier_outcome([], None, entry, stop, target)
            else:
                w = _window(bars, start, end)
                if w is None or w.height < hold_h * 60 * MIN_COVERAGE:
                    if datetime.datetime.fromtimestamp(end / 1e9, datetime.UTC) + NO_DATA_AFTER < now:
                        res = {"outcome": "NO_DATA", "direction": None, "realized_r": None, "mfe_r": None,
                               "mae_r": None, "bars_to_exit": None}
                    else:
                        continue
                else:
                    res = barrier_outcome(w, None, entry, stop, target)
                    net = net_labels(w, res["direction"], stop, target, bar_hours=1 / 60)
            out.append((r["id"], res["direction"], _dec(entry), _dec(stop), _dec(target), res["outcome"],
                        _dec(res["realized_r"]), _dec(res["mfe_r"]), _dec(res["mae_r"]), res["bars_to_exit"], hold_h,
                        _dec(net["entry_next_open"]), _dec(net["net_r_preset"]), _dec(net["net_r_profile"]),
                        net["cost_model"]))
    if out:
        async with pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO candidate_outcomes (candidate_id, direction, entry, stop, target, outcome,
                          realized_r, mfe_r, mae_r, bars_to_exit, max_hold_hours,
                          entry_next_open, net_r_preset, net_r_profile, cost_model)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                   ON CONFLICT (candidate_id) DO NOTHING""", out)
        logger.info("Labelled %d candidate outcomes", len(out))
    try:
        await backfill_net_labels(pool, lake, limit)
    except Exception as e:  # the backfill is best-effort and retried next pass
        logger.warning("Candidate net-label backfill failed: %s", e)
    return len(out)


async def backfill_net_labels(pool, lake, limit: int = 300) -> int:
    """Fill the 057 columns on one batch of existing labels, oldest signal first. Idempotent: only rows
    with ``cost_model IS NULL`` are read and written, so repeated passes walk forward to the newest."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT co.candidate_id, co.direction, co.stop, co.target, co.max_hold_hours,
                      sc.instrument_id, sc.interval_end_exclusive_ns
               FROM candidate_outcomes co JOIN signal_candidates sc ON sc.id = co.candidate_id
               WHERE co.cost_model IS NULL AND co.outcome IN ('STOP', 'TARGET', 'EXPIRED')
               ORDER BY sc.interval_end_exclusive_ns, co.candidate_id
               LIMIT $1""", limit)
    by_symbol: dict[str, list[tuple[Any, int, int]]] = defaultdict(list)
    for r in rows:
        start = int(r["interval_end_exclusive_ns"])
        by_symbol[r["instrument_id"].split(".")[0]].append((r, start, start + int(r["max_hold_hours"]) * HOUR_NS))
    updates: list[tuple] = []
    for symbol, items in by_symbol.items():
        try:
            bars = _symbol_bars(lake, symbol, min(i[1] for i in items), max(i[2] for i in items))
        except Exception as e:  # retried on the next pass
            logger.warning("Candidate net backfill: lake refused %s (%s)", symbol, e)
            continue
        for r, start, end in items:
            direction = r["direction"]
            stop, target = float(r["stop"]), float(r["target"])
            w = _window(bars, start, end)
            net = net_labels(w if w is not None else [], direction, stop, target, bar_hours=1 / 60)
            # No bars left in the lake: record the model anyway so the walk moves on; net R stays NULL.
            model = net["cost_model"] or cost_model_name(default_market(direction))
            updates.append((r["candidate_id"], _dec(net["entry_next_open"]), _dec(net["net_r_preset"]),
                            _dec(net["net_r_profile"]), model))
    if updates:
        async with pool.acquire() as conn:
            await conn.executemany(
                """UPDATE candidate_outcomes SET entry_next_open = $2, net_r_preset = $3, net_r_profile = $4,
                          cost_model = $5
                   WHERE candidate_id = $1 AND cost_model IS NULL""", updates)
        logger.info("Backfilled net labels on %d candidate outcomes", len(updates))
    return len(updates)
