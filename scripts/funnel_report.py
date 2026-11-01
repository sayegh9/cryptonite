"""Phase 2 funnel report (read-only): where scanner candidates stop between the scan and a fill.

Stages, each counting the candidates that reached it:
scan -> gate (passed the deterministic gate) -> triage (debate started) -> debate (verdict) -> route (entry routed to
approval or execution) -> command (an execution command exists) -> fill (a FILL row in the decision log).

Each stage is broken down by profile, direction, preset, timeframe and UTC hour; the candidates that stopped are
grouped by refusal reason. A cooldown section shows what the candidates refused ``COOLDOWN`` would have earned
(``candidate_outcomes.net_r_profile`` / ``net_r_preset``). A fire-rate section (H5) shows what the newest bar of each
scanner evaluation came to, by coin x preset x side x timeframe, from ``scan_eval_counts``. Markdown goes to stdout.

Usage: DATABASE_URL=postgresql://... python scripts/funnel_report.py --start 2026-11-01 --end 2026-11-08
(``--end`` is exclusive; dates are UTC; default: the last 7 days).
"""

import argparse
import asyncio
import datetime as dt
import os
from collections import Counter, defaultdict
from typing import Any

import asyncpg

STAGES = ("scan", "gate", "triage", "debate", "route", "command", "fill")
DIMENSIONS = ("profile", "direction", "preset", "timeframe", "hour_utc")
_ROUTED = ("COMPLETED", "PENDING_APPROVAL")

CANDIDATES_SQL = """
WITH c AS (
    SELECT sc.id, sc.instrument_id, sc.timeframe,
           sc.metrics->>'preset_slug' AS preset,
           sc.metrics->>'mandate_id' AS metrics_mandate,
           CASE WHEN sc.suggested_stop > sc.suggested_entry THEN 'SHORT' ELSE 'LONG' END AS direction,
           extract(hour FROM sc.created_at AT TIME ZONE 'UTC')::int AS hour_utc
      FROM signal_candidates sc
     WHERE sc.created_at >= $1 AND sc.created_at < $2
)
SELECT c.*, m.name AS profile,
       r.id AS run_id, r.status, r.failure_reason,
       EXISTS (SELECT 1 FROM policy_decisions p WHERE p.candidate_id = c.id AND p.policy = 'A') AS policy_a,
       pd.recommendation,
       (SELECT d.reason FROM ai_profile_decisions d
         WHERE d.run_id = r.id AND d.stage = 'ROUTE' ORDER BY d.ts DESC LIMIT 1) AS route_reason,
       cmd.command_ids,
       (r.id IS NOT NULL AND EXISTS (
            SELECT 1 FROM ai_profile_decisions f
             WHERE f.stage = 'FILL' AND (f.run_id = r.id OR f.command_id = ANY(cmd.command_ids)))) AS filled,
       co.net_r_profile, co.net_r_preset
  FROM c
  LEFT JOIN LATERAL (SELECT * FROM debate_runs dr WHERE dr.candidate_id = c.id
                      ORDER BY dr.created_at LIMIT 1) r ON true
  LEFT JOIN LATERAL (SELECT p.recommendation FROM portfolio_decisions p WHERE p.run_id = r.id LIMIT 1) pd ON true
  LEFT JOIN LATERAL (SELECT array_agg(ec.command_id) AS command_ids FROM execution_commands ec
                      WHERE r.decision_trace_id IS NOT NULL
                        AND ec.decision_trace_id::text = r.decision_trace_id::text) cmd ON true
  LEFT JOIN ai_mandates m ON m.id::text = coalesce(r.mandate_id::text, c.metrics_mandate)
  LEFT JOIN candidate_outcomes co ON co.candidate_id = c.id
"""

POLICIES_SQL = """
SELECT p.policy, p.decision, count(*) AS n, sum(p.cost_usd) AS cost_usd
  FROM policy_decisions p JOIN signal_candidates sc ON sc.id = p.candidate_id
 WHERE sc.created_at >= $1 AND sc.created_at < $2
 GROUP BY p.policy, p.decision ORDER BY p.policy, p.decision
"""

EVAL_OUTCOMES = ("MATCHED_FRESH", "REJECTED_RR", "STALE", "NO_TRIGGER")
EVAL_COUNTS_SQL = """
SELECT instrument_id, preset_slug, side, timeframe, outcome, sum(n)::bigint AS n
  FROM scan_eval_counts
 WHERE bucket_start >= $1 AND bucket_start < $2
 GROUP BY instrument_id, preset_slug, side, timeframe, outcome
"""


def classify(row: dict[str, Any]) -> tuple[int, str | None]:
    """(index of the last stage the candidate reached, why it went no further; None for a fill)."""
    status, reason = row.get("status"), row.get("failure_reason")
    if row.get("run_id") is None:
        return 0, "NOT_DISPATCHED"
    if status == "GATE_REFUSED":
        # Refused after the deterministic gate (rules, triage, caps) when policy A saw it
        after_gate = row.get("policy_a") or str(reason or "").startswith("TRIAGE")
        return (1 if after_gate else 0), reason or "GATE_REFUSED"
    if row.get("recommendation") is None:  # the debate started but produced no verdict
        if status == "NO_PRICE" and not row.get("policy_a"):
            return 0, "NO_PRICE"
        return 2, reason or ("IN_PROGRESS" if status == "RUNNING" else status or "NO_VERDICT")
    if status not in _ROUTED or row["recommendation"] not in ("BUY", "SELL"):
        return 3, "HOLD" if row["recommendation"] == "HOLD" else (row.get("route_reason") or status or "NO_ROUTE")
    if not row.get("command_ids"):
        return 4, status  # PENDING_APPROVAL (not approved) or routed without a command
    if not row.get("filled"):
        return 5, "NOT_FILLED"
    return 6, None


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


def _r(values: list[float]) -> tuple[str, str, str]:
    if not values:
        return "-", "-", "-"
    total = sum(values)
    wins = sum(1 for v in values if v > 0)
    return f"{total:.2f}", f"{total / len(values):.3f}", f"{100 * wins / len(values):.0f}%"


def fire_rate_section(eval_rows: list[dict[str, Any]] | None) -> list[str]:
    """H5: newest-bar outcomes per coin x preset x side x timeframe; fire rate = MATCHED_FRESH / evaluations."""
    lines = ["## Preset fire rate (newest bar per evaluation)", ""]
    if eval_rows is None:
        return lines + ["scan_eval_counts unavailable (migration 058 not applied).", ""]
    if not eval_rows:
        return lines + ["No evaluations counted.", ""]
    grid: dict[tuple[str, ...], Counter] = defaultdict(Counter)
    for r in eval_rows:
        grid[(str(r["instrument_id"]).split(".")[0], r["preset_slug"], r["side"], r["timeframe"])][r["outcome"]] += \
            int(r["n"])
    body = []
    for key, c in sorted(grid.items(), key=lambda kv: (-kv[1]["MATCHED_FRESH"], -sum(kv[1].values()), kv[0])):
        total = sum(c.values())
        body.append([*key, total, *(c[o] for o in EVAL_OUTCOMES), f"{100 * c['MATCHED_FRESH'] / total:.1f}%"])
    return lines + [_table(["coin", "preset", "side", "timeframe", "evaluations", *EVAL_OUTCOMES, "fire rate"], body),
                    ""]


def render(rows: list[dict[str, Any]], policy_rows: list[dict[str, Any]], start: dt.date, end: dt.date,
           eval_rows: list[dict[str, Any]] | None = None) -> str:
    stages = [(row, *classify(row)) for row in rows]
    n = len(rows)
    lines = [f"# Candidate funnel {start} to {end} (UTC, end exclusive)", "", f"{n} scanner candidates.", ""]

    reached = [sum(1 for _, last, _ in stages if last >= i) for i in range(len(STAGES))]
    lines += ["## Stages", "", _table(["stage", "candidates", "% of scan"],
                                       [[s, reached[i], f"{100 * reached[i] / n:.1f}%" if n else "-"]
                                        for i, s in enumerate(STAGES)]), ""]

    stops = Counter((STAGES[last], reason) for _, last, reason in stages if reason is not None)
    lines += ["## Where candidates stopped", "", "Stage = the last stage reached; reason = why it went no further.",
              "", _table(["stage", "reason", "candidates"],
                         [[s, r, c] for (s, r), c in sorted(stops.items(), key=lambda kv: (STAGES.index(kv[0][0]),
                                                                                           -kv[1]))]), ""]

    for dim in DIMENSIONS:
        counts: dict[Any, list[int]] = defaultdict(lambda: [0] * len(STAGES))
        reasons: dict[Any, Counter] = defaultdict(Counter)
        for row, last, reason in stages:
            key = row.get(dim) if row.get(dim) is not None else "(none)"
            for i in range(last + 1):
                counts[key][i] += 1
            if reason is not None:
                reasons[key][reason] += 1
        body = [[k, *v, ", ".join(f"{r} {c}" for r, c in reasons[k].most_common(3))]
                for k, v in sorted(counts.items(), key=lambda kv: (-kv[1][0], str(kv[0])))]
        lines += [f"## By {dim}", "", _table([dim, *STAGES, "top refusals"], body), ""]

    cooled = [row for row, _, reason in stages if reason == "COOLDOWN"]
    prof = [float(r["net_r_profile"]) for r in cooled if r.get("net_r_profile") is not None]
    pres = [float(r["net_r_preset"]) for r in cooled if r.get("net_r_preset") is not None]
    lines += ["## Cooldown impact", "",
              f"{len(cooled)} candidates refused COOLDOWN; {len(prof)} labelled (profile costs), "
              f"{len(pres)} labelled (preset costs).", "",
              _table(["label", "labelled", "sum net R", "mean net R", "win rate"],
                     [["net_r_profile", len(prof), *_r(prof)], ["net_r_preset", len(pres), *_r(pres)]]), ""]
    by_tf: dict[Any, list[float]] = defaultdict(list)
    for r in cooled:
        if r.get("net_r_profile") is not None:
            by_tf[r.get("timeframe")].append(float(r["net_r_profile"]))
    if by_tf:
        lines += [_table(["timeframe", "labelled", "sum net R (profile)", "mean", "win rate"],
                         [[tf, len(v), *_r(v)] for tf, v in sorted(by_tf.items(), key=lambda kv: str(kv[0]))]), ""]

    lines += ["## Shadow policies", "", _table(["policy", "decision", "candidates", "cost USD"],
                                               [[p["policy"], p["decision"], p["n"], p["cost_usd"]]
                                                for p in policy_rows]) if policy_rows else "No policy rows.", ""]
    lines += fire_rate_section(eval_rows)
    return "\n".join(lines)


async def report(dsn: str, start: dt.date, end: dt.date) -> str:
    lo = dt.datetime.combine(start, dt.time(), dt.UTC)
    hi = dt.datetime.combine(end, dt.time(), dt.UTC)
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction(readonly=True):
            rows = [dict(r) for r in await conn.fetch(CANDIDATES_SQL, lo, hi)]
            policy_rows = [dict(r) for r in await conn.fetch(POLICIES_SQL, lo, hi)]
        eval_rows: list[dict[str, Any]] | None
        try:
            async with conn.transaction(readonly=True):
                eval_rows = [dict(r) for r in await conn.fetch(EVAL_COUNTS_SQL, lo, hi)]
        except asyncpg.UndefinedTableError:
            eval_rows = None
    finally:
        await conn.close()
    return render(rows, policy_rows, start, end, eval_rows)


def main() -> None:
    today = dt.datetime.now(dt.UTC).date()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--start", type=dt.date.fromisoformat, default=today - dt.timedelta(days=7))
    parser.add_argument("--end", type=dt.date.fromisoformat, default=today + dt.timedelta(days=1))
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is required")
    print(asyncio.run(report(dsn, args.start, args.end)))


if __name__ == "__main__":
    main()
