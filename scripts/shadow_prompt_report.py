#!/usr/bin/env python3
"""H13d: print the shadow prompt's verdict against its pass mark (docs/adr/0012). Read-only: one READ ONLY transaction and
lake reads, nothing is written and nothing acts on the verdict.

Run in the learner container (it has the lake path and the database role); the image has no ``scripts/``, so the script
goes in on stdin:

    docker compose -f infra/docker-compose.yml exec -T learner python - < scripts/shadow_prompt_report.py
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sys
from collections import Counter
from pathlib import Path

# On stdin (``python - < scripts/...``, the documented way in) there is no __file__: the container cwd is /app
_HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd() / "scripts"
for _path in (_HERE.parent / "services" / "debate_worker", _HERE):
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import asyncpg  # noqa: E402
from cryptonite_storage.queries import AnalyticsQueryEngine  # noqa: E402
from shadow_eval import PROMPT_VERSIONS, REGIMES, Row, evaluate, regime_at  # noqa: E402

_NS = 10**9
_REGIME_MARGIN_NS = 26 * 3600 * _NS

_ROWS = """
    SELECT sv.state, sv.prompt_version, sv.recommendation, sv.cost_usd, sv.created_at, sc.instrument_id,
           sc.bar_close_inclusive_ns, co.direction, co.net_r_live, sr.trigger_reason, pb.decision AS live_decision
    FROM shadow_debate_verdicts sv
    JOIN signal_candidates sc ON sc.id = sv.candidate_id
    LEFT JOIN scan_runs sr ON sr.id = sc.scan_run_id
    LEFT JOIN candidate_outcomes co ON co.candidate_id = sv.candidate_id
    LEFT JOIN policy_decisions pb ON pb.candidate_id = sv.candidate_id AND pb.policy = 'B'
"""


def _regimes(lake, raw: list) -> dict[str, list[tuple[int, float]]]:
    """1h bars per coin, read once at report time (the hot path never computes a regime)."""
    wanted = [r for r in raw if r["net_r_live"] is not None]
    bars: dict[str, list[tuple[int, float]]] = {}
    for instrument in {r["instrument_id"] for r in wanted}:
        stamps = [int(r["bar_close_inclusive_ns"]) for r in wanted if r["instrument_id"] == instrument]
        try:
            df = lake.query_bars_df(instrument.replace("-PERP", ""), "1h", min(stamps) - _REGIME_MARGIN_NS, max(stamps))
            bars[instrument] = [] if df is None or df.is_empty() else [
                (int(b["ts_event_ns"]), float(b["close"])) for b in df.sort("ts_event_ns").to_dicts()]
        except Exception as e:
            print(f"no 1h bars for {instrument}: {e}")
            bars[instrument] = []
    return bars


def _row(raw, bars: dict[str, list[tuple[int, float]]]) -> Row:
    signal_ns = int(raw["bar_close_inclusive_ns"])
    labelled = raw["net_r_live"] is not None
    return Row(day=datetime.datetime.fromtimestamp(signal_ns / _NS, datetime.UTC).date(), state=raw["state"],
               prompt_version=raw["prompt_version"], recommendation=raw["recommendation"], direction=raw["direction"],
               net_r=float(raw["net_r_live"]) if labelled else None,
               regime=regime_at(bars.get(raw["instrument_id"], []), signal_ns) if labelled else None,
               trigger_reason=raw["trigger_reason"], live_decision=raw["live_decision"])


def _r(value) -> str:
    return "n/a" if value is None else f"{value:+.3f}R"


def _line(name: str, f: dict) -> str:
    return (f"{name:<8} rows {f['rows']:>4}  TAKE {f['takes']:>4} mean {_r(f['mean_take']):>9}  "
            f"SKIP {f['skips']:>4} mean {_r(f['mean_skip']):>9}  difference {_r(f['edge']):>9}")


def render(raw: list, bars: dict[str, list[tuple[int, float]]]) -> str:
    return "\n\n".join(_render_version(version, [r for r in raw if r["prompt_version"] == version], bars)
                       for version in PROMPT_VERSIONS)


def _render_version(version: str, raw: list, bars: dict[str, list[tuple[int, float]]]) -> str:
    verdict = evaluate([_row(r, bars) for r in raw], version)
    s = verdict.stats
    today = datetime.datetime.now(datetime.UTC).date()
    states = Counter(r["state"] for r in raw)
    spent = sum(float(r["cost_usd"] or 0) for r in raw)
    spent_today = sum(float(r["cost_usd"] or 0) for r in raw if r["created_at"].astimezone(datetime.UTC).date() == today)
    lines = [f"VERDICT {verdict.result} (prompt {s['prompt_version']}; prints only, nothing acts on it)",
             *[f"  - {reason}" for reason in verdict.reasons],
             "", f"counts: {len(raw)} shadow rows {dict(states)}; {s['overall']['rows']} usable over {s['days']} days; "
                 f"TAKE rate {s['take_rate']:.1%}; 90% lower bound {_r(s['lower_bound'])}",
             _line("overall", s["overall"]), *[_line(g, s["regimes"][g]) for g in REGIMES],
             "", "live policy B on the same setups, for reference only:", _line("live", s["live_reference"]),
             "", f"spend: ${spent_today:.4f} today, ${spent:.4f} in all"]
    return "\n".join(lines)


async def report(pool, lake) -> str:
    async with pool.acquire() as conn, conn.transaction(readonly=True):
        raw = await conn.fetch(_ROWS)
    return render(raw, _regimes(lake, raw))


async def main() -> None:
    lake = AnalyticsQueryEngine(Path(os.getenv("ANALYTICS_LAKE_PATH", "/app/data/analytics_lake")))
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"),
                                     min_size=1, max_size=2)
    try:
        print(await report(pool, lake))
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
