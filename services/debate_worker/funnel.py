"""H13c: the learner's funnel pass: provisional what-ifs, alarms when the funnel silently stops, one daily summary.

Reads and reports only: it publishes alerts and writes ``candidate_provisional_outcomes``, and it reaches no order path.
``run_pass`` never raises, so it can break neither the learner's labels nor its heartbeat. ``funnel_findings`` is pure.
"""

import datetime
import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from candidate_outcomes import (
    HOLD_HOURS,
    HOUR_NS,
    _dec,
    _settings_for,
    _symbol_bars,
    _window,
    load_live_exit_context,
)
from cryptonite_schemas.exit_rules import simulate_live_exit
from cryptonite_schemas.funnel import (
    APPROVED,
    EXPIRED,
    FAILED_STATUSES,
    MISSED_STAGES,
    PROPOSED,
    REJECTED,
    ROUTED,
    ROWS_SQL,
    UNACCOUNTED,
    UNACCOUNTED_AFTER,
    coin_of,
    funnel_counts,
    funnel_row,
)
from cryptonite_schemas.profile import RunState, run_state_of, underlying_spot

logger = logging.getLogger("cryptonite.debate_worker.funnel")

SYSTEM_ALERTS_CHANNEL = "channel:system_alerts"
ALARM_KIND = "FUNNEL_ALARM"
DAILY_KIND = "FUNNEL_DAILY"

PROVISIONAL_LOOKBACK_HOURS = 48
PROVISIONAL_BATCH = 100
ALARM_LOOKBACK = datetime.timedelta(hours=24)
ALARM_DEDUPE_S = 6 * 3600
DAILY_KEY_TTL_S = 2 * 86400
DAILY_AFTER = datetime.time(0, 5)
SCANNER_SILENT_AFTER = datetime.timedelta(minutes=15)
FAILING_DEBATES_WINDOW = 5
FAILING_DEBATES_MIN = 3
MOVE_PCT = 3.0
MOVE_WINDOW = datetime.timedelta(hours=24)
MOVE_BAR_TOLERANCE = datetime.timedelta(hours=2)  # a stale or gapped 1h series is no measured move
ALL_EXPIRED_MIN = 5
EXAMPLES_MAX = 5

_NS = 1_000_000_000


@dataclass
class FunnelSnapshot:
    """What the alarms read: raw candidate rows of the last 24 hours (``funnel.ROWS_SQL``), and the rest of the funnel."""
    rows: list[dict[str, Any]] = field(default_factory=list)
    last_committed_scan_at: datetime.datetime | None = None
    recent_debates: list[dict[str, Any]] = field(default_factory=list)  # newest first, without gate refusals
    moves: dict[str, float] = field(default_factory=dict)  # coin -> 24h close move, percent (covered coins only)
    evaluations: dict[str, dict[str, int]] = field(default_factory=dict)  # coin -> outcome -> n


def _examples(views: list[dict[str, Any]]) -> str:
    shown = ", ".join(f"{v['coin']} {v['timeframe']} ({v['reason'] or 'no reason'})" for v in views[:EXAMPLES_MAX])
    return shown + (f" and {len(views) - EXAMPLES_MAX} more" if len(views) > EXAMPLES_MAX else "")


def _is_missing_proposal(row: dict[str, Any], now: datetime.datetime) -> bool:
    return (row.get("trigger_source") == "scanner" and str(row.get("recommendation") or "").upper() == "HOLD"
            and row.get("policy_a") == "TAKE" and not row.get("proposal_status") and not row.get("stop_stage")
            and row.get("debate_at") is not None and now - row["debate_at"] >= UNACCOUNTED_AFTER)


def _move_finding(views: list[dict[str, Any]], snapshot: FunnelSnapshot) -> dict[str, Any] | None:
    by_coin: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for v in views:
        by_coin[v["coin"]].append(v)
    lines, coins = [], []
    for coin, move in sorted(snapshot.moves.items()):
        seen = by_coin.get(coin, [])
        if abs(move) < MOVE_PCT or any(v["stage"] in (PROPOSED, APPROVED, REJECTED, EXPIRED, ROUTED) for v in seen):
            continue
        reasons = Counter(f"{v['stage']}: {v['reason']}" if v["reason"] else v["stage"] for v in seen)
        evals = snapshot.evaluations.get(coin, {})
        why = ", ".join(f"{k} x{n}" for k, n in reasons.most_common(EXAMPLES_MAX)) or "the scanner found nothing"
        scans = ", ".join(f"{k} {n}" for k, n in sorted(evals.items())) or "none recorded"
        lines.append(f"{coin} moved {move:+.1f}% in 24h: 0 proposals, 0 routed entries, {len(seen)} candidates ({why}). "
                     f"Evaluations: {scans}.")
        coins.append({"coin": coin, "move_pct": round(move, 2), "candidates": len(seen), "reasons": dict(reasons)})
    return {"check": "MOVE_NO_PROPOSALS", "message": "\n".join(lines), "coins": coins} if lines else None


def funnel_findings(snapshot: FunnelSnapshot, now: datetime.datetime) -> list[dict[str, Any]]:
    """Each check that fires, as ``{check, message}``. Pure: everything it reads is in ``snapshot``."""
    findings: list[dict[str, Any]] = []
    missing = [r for r in snapshot.rows if _is_missing_proposal(r, now)]
    missing_ids = {r["candidate_id"] for r in missing}
    views = [funnel_row(r, now) for r in snapshot.rows]
    # A missing proposal is reported once, under the check that names it exactly
    unaccounted = [v for r, v in zip(snapshot.rows, views, strict=True)
                   if v["stage"] == UNACCOUNTED and r["candidate_id"] not in missing_ids]
    if unaccounted:
        findings.append({"check": "UNACCOUNTED", "message": (
            f"{len(unaccounted)} setups have no recorded end after {int(UNACCOUNTED_AFTER.total_seconds() // 60)} "
            f"minutes: {_examples(unaccounted)}.")})
    if missing:
        shown = [funnel_row(r, now) for r in missing]
        findings.append({"check": "PROPOSAL_MISSING", "message": (
            f"{len(missing)} scanner debates said HOLD on a setup policy A would take, with neither a card nor a recorded "
            f"reason: {_examples(shown)}.")})
    last = snapshot.last_committed_scan_at
    if last is None or now - last >= SCANNER_SILENT_AFTER:
        age = "no run on record" if last is None else f"the last was {int((now - last).total_seconds() // 60)} minutes ago"
        findings.append({"check": "SCANNER_SILENT", "message": f"No committed-bar scan run: {age}."})
    recent = snapshot.recent_debates[:FAILING_DEBATES_WINDOW]
    failed = [d for d in recent if d.get("status") in FAILED_STATUSES or d.get("failure_reason")]
    if len(failed) >= FAILING_DEBATES_MIN:
        reasons = Counter(str(d.get("failure_reason") or d.get("status")) for d in failed)
        findings.append({"check": "DEBATES_FAILING", "message": (
            f"{len(failed)} of the last {len(recent)} scanner debates failed: "
            f"{', '.join(f'{k} x{n}' for k, n in reasons.most_common())}.")})
    if (move := _move_finding(views, snapshot)) is not None:
        findings.append(move)
    decided = sum(v["stage"] in (APPROVED, REJECTED) for v in views)
    lapsed = sum(v["stage"] == EXPIRED for v in views)
    if lapsed >= ALL_EXPIRED_MIN and decided == 0:
        findings.append({"check": "ALL_EXPIRED", "message": (
            f"{lapsed} setup cards expired in 24 hours and none was decided: the cards may not be reaching you.")})
    return findings


def _mean(values: list[float]) -> str:
    return f"{sum(values) / len(values):+.2f}R (n={len(values)})" if values else "none"


def daily_message(views: list[dict[str, Any]]) -> str:
    """The FUNNEL_DAILY text over one day's ``funnel_row`` views."""
    c = funnel_counts(views)
    verdicts = ", ".join(f"{k} {n}" for k, n in sorted(c["verdicts"].items())) or "none"
    proposals = ", ".join(f"{k.lower()} {n}" for k, n in c["proposals"].items())
    scored = [v for v in views if v["what_if_r"] is not None]
    missed = sorted((v for v in scored if v["stage"] in MISSED_STAGES), key=lambda v: v["what_if_r"])
    taken = [v["what_if_r"] for v in scored if v["stage"] == APPROVED]

    def setup(v: dict[str, Any]) -> str:
        return f"{v['coin']} {v['direction'] or ''} {v['timeframe']} {v['what_if_r']:+.2f}R".replace("  ", " ")

    lines = [f"Last 24h: {c['candidates']} setups, {c['gate_passed']} passed the gate, {c['gate_refused']} refused.",
             f"Verdicts: {verdicts}.", f"Cards: {proposals}; approved {c['approved']}.",
             f"Mean what-if (model figures, setups overlap): not taken {_mean([v['what_if_r'] for v in missed])}, "
             f"approved {_mean(taken)}."]
    if missed:
        lines.append(f"Best missed: {setup(missed[-1])}. Worst missed: {setup(missed[0])}.")
    if c["unaccounted"]:
        lines.append(f"Unaccounted: {c['unaccounted']}.")
    return "\n".join(lines)


async def update_provisional(pool, lake, limit: int = PROVISIONAL_BATCH, now: datetime.datetime | None = None) -> int:
    """One batch of what-ifs for recent candidates without ``net_r_live``, never computed ones first, then the open ones
    whose figure is oldest. One lake query per symbol; a symbol the lake refuses waits for the next pass."""
    now = now or datetime.datetime.now(datetime.UTC)
    now_ns = int(now.timestamp() * _NS)
    async with pool.acquire() as conn:
        live_by_base, live_t = await load_live_exit_context(conn)
        rows = await conn.fetch(
            """SELECT sc.id, sc.instrument_id, sc.timeframe, sc.interval_end_exclusive_ns,
                      sc.suggested_entry, sc.suggested_stop, sc.suggested_target
               FROM signal_candidates sc
               JOIN scan_runs sr ON sr.id = sc.scan_run_id AND sr.trigger_reason <> 'HISTORICAL_PRIMING'
               LEFT JOIN candidate_outcomes co ON co.candidate_id = sc.id
               LEFT JOIN candidate_provisional_outcomes p ON p.candidate_id = sc.id
               WHERE sc.created_at >= $1 AND co.net_r_live IS NULL AND sc.timeframe = ANY($2::text[])
                 AND sc.suggested_entry <> sc.suggested_stop AND (p.candidate_id IS NULL OR p.state = 'OPEN')
               ORDER BY p.computed_at NULLS FIRST, sc.created_at DESC
               LIMIT $3""",
            now - datetime.timedelta(hours=PROVISIONAL_LOOKBACK_HOURS), list(HOLD_HOURS), limit)
    by_symbol: dict[str, list[tuple[Any, int, int]]] = defaultdict(list)
    for r in rows:
        start = int(r["interval_end_exclusive_ns"])
        by_symbol[r["instrument_id"].split(".")[0]].append((r, start, start + HOLD_HOURS[r["timeframe"]] * HOUR_NS))
    updates: list[tuple] = []
    for symbol, items in by_symbol.items():
        try:
            bars = _symbol_bars(lake, symbol, min(i[1] for i in items), min(max(i[2] for i in items), now_ns))
        except Exception as e:
            logger.warning("Provisional what-if: lake refused %s (%s)", symbol, e)
            continue
        for r, start, end in items:
            window = _window(bars, start, end)
            if window is None or window.is_empty():
                continue  # nothing to replay yet: the row stays PENDING
            entry, stop, target = (float(r[k]) for k in ("suggested_entry", "suggested_stop", "suggested_target"))
            direction = "LONG" if stop < entry else "SHORT"
            res = simulate_live_exit(window.to_dicts(), direction, stop, target,
                                     settings=_settings_for(live_by_base, r["instrument_id"]), thresholds=live_t)
            # Bars ran out before the hold ended: the trade would still be open, marked at the last close
            state = "OPEN" if res.outcome == "EXPIRED" and end > now_ns else res.outcome
            updates.append((r["id"], _dec(res.gross_r(direction, stop)), state, now))
    if updates:
        async with pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO candidate_provisional_outcomes (candidate_id, gross_r, state, computed_at)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (candidate_id) DO UPDATE SET gross_r = EXCLUDED.gross_r, state = EXCLUDED.state,
                       computed_at = EXCLUDED.computed_at""", updates)
    return len(updates)


def coin_move_pct(lake, symbol: str, now: datetime.datetime) -> float | None:
    """Percent move of the 1h close over the last 24 hours; None when the series cannot measure it."""
    now_ns = int(now.timestamp() * _NS)
    span_ns = int((MOVE_WINDOW + 2 * MOVE_BAR_TOLERANCE).total_seconds()) * _NS
    bars = lake.query_bars_df(symbol, "1h", now_ns - span_ns, now_ns)
    if bars is None or bars.is_empty():
        return None
    rows = bars.sort("ts_event_ns").to_dicts()
    tolerance_ns = int(MOVE_BAR_TOLERANCE.total_seconds()) * _NS
    newest = rows[-1]
    if now_ns - int(newest["ts_event_ns"]) > tolerance_ns:
        return None
    cutoff = int(newest["ts_event_ns"]) - int(MOVE_WINDOW.total_seconds()) * _NS
    earlier = [r for r in rows if int(r["ts_event_ns"]) <= cutoff]
    if not earlier or cutoff - int(earlier[-1]["ts_event_ns"]) > tolerance_ns or not float(earlier[-1]["close"]):
        return None
    return (float(newest["close"]) / float(earlier[-1]["close"]) - 1.0) * 100.0


def _covered_coins(mandates: list[Any]) -> dict[str, str]:
    """Coin -> lake symbol, for the instruments of running profiles."""
    coins: dict[str, str] = {}
    for m in mandates:
        try:
            if run_state_of(m["status"]) is not RunState.RUNNING:
                continue
        except ValueError:
            continue
        for instrument in m["instruments"] or []:
            try:
                coins[coin_of(instrument)] = underlying_spot(instrument).split(".")[0]
            except ValueError:
                continue
    return coins


async def load_snapshot(pool, lake, now: datetime.datetime) -> FunnelSnapshot:
    since = now - ALARM_LOOKBACK
    async with pool.acquire() as conn:
        rows = await conn.fetch(ROWS_SQL, since)
        last_scan = await conn.fetchval(
            "SELECT max(started_at) FROM scan_runs WHERE trigger_reason = 'COMMITTED_BAR_EVENT'")
        recent = await conn.fetch(
            """SELECT r.status, r.failure_reason FROM debate_runs r
               LEFT JOIN signal_candidates sc ON sc.id = r.candidate_id
               LEFT JOIN scan_runs sr ON sr.id = sc.scan_run_id
               WHERE r.trigger_source = 'scanner' AND r.status NOT IN ('GATE_REFUSED', 'RUNNING') AND r.created_at >= $1
                 AND sr.trigger_reason IS DISTINCT FROM 'HISTORICAL_PRIMING'
               ORDER BY r.created_at DESC LIMIT $2""", since, FAILING_DEBATES_WINDOW)
        evaluations = await conn.fetch(
            """SELECT instrument_id, outcome, sum(n)::bigint AS n FROM scan_eval_counts
               WHERE bucket_start >= $1 GROUP BY instrument_id, outcome""", since)
        mandates = await conn.fetch("SELECT status, instruments FROM ai_mandates")
    snapshot = FunnelSnapshot(rows=[dict(r) for r in rows], last_committed_scan_at=last_scan,
                              recent_debates=[dict(r) for r in recent])
    for r in evaluations:
        by_outcome = snapshot.evaluations.setdefault(coin_of(r["instrument_id"]), {})
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + r["n"]
    for coin, symbol in _covered_coins(mandates).items():
        try:
            move = coin_move_pct(lake, symbol, now)
        except Exception as e:  # a coin the lake cannot read costs only that coin's check
            logger.warning("Funnel: lake refused %s (%s)", symbol, e)
            continue
        if move is None:
            logger.warning("Funnel: no 24h 1h-bar move for %s", symbol)
        else:
            snapshot.moves[coin] = move
    return snapshot


async def _publish_once(redis_client, key: str, ttl_s: int, alert: dict[str, Any]) -> bool:
    """Publish under a Redis guard. The guard is released when nobody received it, so the next pass tries again."""
    if not await redis_client.set(key, "1", nx=True, ex=ttl_s):
        return False
    received = 0
    try:
        received = await redis_client.publish(SYSTEM_ALERTS_CHANNEL, json.dumps(alert))
    finally:
        if not received:
            await redis_client.delete(key)
    return bool(received)


async def publish_findings(redis_client, findings: list[dict[str, Any]]) -> int:
    """One alert per check per ``ALARM_DEDUPE_S``. ``action`` is the check, which the notifier's dedupe key reads."""
    sent = 0
    for f in findings:
        try:
            alert = {"kind": ALARM_KIND, "severity": "CRITICAL", "check": f["check"], "action": f["check"],
                     "message": f["message"]}
            sent += await _publish_once(redis_client, f"funnel:alarm:{f['check']}", ALARM_DEDUPE_S, alert)
        except Exception as e:
            logger.warning("Funnel alarm %s not published: %s", f["check"], e)
    return sent


async def publish_daily(redis_client, snapshot: FunnelSnapshot, now: datetime.datetime) -> bool:
    if now.time() < DAILY_AFTER:
        return False
    date = now.date().isoformat()
    alert = {"kind": DAILY_KIND, "severity": "ACTION", "date": date,
             "message": daily_message([funnel_row(r, now) for r in snapshot.rows])}
    return await _publish_once(redis_client, f"funnel:daily:{date}", DAILY_KEY_TTL_S, alert)


async def run_pass(pool, lake, redis_client, now: datetime.datetime | None = None) -> dict[str, int]:
    """One learner pass. Each step is on its own; none raises."""
    now = now or datetime.datetime.now(datetime.UTC)
    out = {"provisional": 0, "alarms": 0}
    try:
        out["provisional"] = await update_provisional(pool, lake, now=now)
    except Exception as e:
        logger.warning("Provisional what-ifs failed: %s", e)
    try:
        snapshot = await load_snapshot(pool, lake, now)
    except Exception as e:
        logger.exception("Funnel snapshot failed: %s", e)
        return out
    try:
        out["alarms"] = await publish_findings(redis_client, funnel_findings(snapshot, now))
        await publish_daily(redis_client, snapshot, now)
    except Exception as e:
        logger.exception("Funnel alarms failed: %s", e)
    return out
