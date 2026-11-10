"""Brain trade-ideas scorecard (read-only): what the ideas the Brain showed came to afterwards.

Every ``get_trade_ideas`` answer is saved with the Brain's reply as a ``trade_ideas`` widget (``ai_messages.widgets``).
This script reads those widgets, replays each idea on the 1m lake bars that closed after it was shown
(``cryptonite_agents.trade_ideas.score_idea``: the exit monitor's rules, next-bar fills, cost model v1) and prints a
Markdown scorecard: every idea, then the mean net R by status, side, kind and coin.

The numbers are hypothetical outcomes of ideas, never realized P&L. An idea repeated with the same coin, side, entry
and stop is counted once (its first showing), so asking twice does not count twice. It writes nothing.

Usage: DATABASE_URL=postgresql://... ANALYTICS_LAKE_PATH=/app/data/analytics_lake \
           python scripts/score_brain_ideas.py --days 14
"""

import argparse
import asyncio
import datetime as dt
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import asyncpg
from cryptonite_agents.trade_ideas import IDEA_HOLD_HOURS, IDEA_TRIGGER_BARS, TF_MINUTES, score_idea

IDEAS_SQL = """
SELECT m.id, m.created_at, w AS widget
  FROM ai_messages m, jsonb_array_elements(m.widgets) AS w
 WHERE m.role = 'assistant' AND m.created_at >= $1 AND w->>'kind' = 'trade_ideas'
 ORDER BY m.created_at, m.id
"""
SCORED = ("STRUCTURAL_STOP", "HARD_STOP", "TRAILING_STOP", "MAX_HOLD", "TP1_FULL", "EXPIRED")


def saved_ideas(rows: list[Any]) -> list[dict[str, Any]]:
    """Each distinct idea with the time it was first shown."""
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        widget = r["widget"] if isinstance(r["widget"], dict) else json.loads(r["widget"])
        shown = r["created_at"]
        for idea in widget.get("ideas") or []:
            key = (idea.get("instrument_id"), idea.get("direction"), idea.get("entry"), idea.get("stop"))
            if None in key or key in seen:
                continue
            seen.add(key)
            out.append({**idea, "shown_at": shown})
    return out


def bars_after(lake: Any, idea: dict[str, Any], now: dt.datetime) -> list[dict[str, Any]]:
    from cryptonite_schemas.profile import underlying_spot
    from cryptonite_storage.queries import lake_symbol

    tf_min = TF_MINUTES.get(idea["timeframe"], 60)
    minutes = IDEA_TRIGGER_BARS * tf_min + IDEA_HOLD_HOURS.get(idea["timeframe"], 72) * 60
    start = idea["shown_at"]
    end = min(now, start + dt.timedelta(minutes=minutes))
    df = lake.query_bars_df(lake_symbol(underlying_spot(idea["instrument_id"])), "1m",
                            int(start.timestamp() * 1e9), int(end.timestamp() * 1e9))
    if df is None or df.is_empty():
        return []
    order = "bar_open_ns" if "bar_open_ns" in df.columns else "ts_event_ns"
    return df.sort(order).select(["open", "high", "low", "close"]).to_dicts()


def mean(values: list[float]) -> str:
    return "-" if not values else f"{sum(values) / len(values):+.2f}"


def render(scored: list[dict[str, Any]], since: dt.datetime, now: dt.datetime) -> str:
    lines = [f"# Brain trade ideas scorecard, {since:%Y-%m-%d %H:%M} to {now:%Y-%m-%d %H:%M} UTC", "",
             "Hypothetical outcomes of ideas the Brain showed (exit-monitor rules, next-bar fills, cost model v1). "
             "Not realized P&L.", ""]
    done = [s for s in scored if s["outcome"] in SCORED and s["net_r"] is not None]
    counts: dict[str, int] = defaultdict(int)
    for s in scored:
        counts[s["outcome"] if s["outcome"] not in SCORED else "TRADED"] += 1
    lines += [f"Ideas: {len(scored)} distinct. " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + ".",
              f"Traded and closed: {len(done)}, mean net R {mean([s['net_r'] for s in done])}, "
              f"winners {sum(s['net_r'] > 0 for s in done)}.", ""]
    for title, key in (("status when shown", "status"), ("side", "direction"), ("kind", "kind"), ("coin", "coin"),
                       ("timeframe", "timeframe")):
        groups: dict[str, list[float]] = defaultdict(list)
        for s in done:
            groups[str(s.get(key))].append(s["net_r"])
        if groups:
            lines += [f"## By {title}", "", "| " + title + " | n | mean net R | winners |", "|---|---|---|---|"]
            lines += [f"| {g} | {len(v)} | {mean(v)} | {sum(x > 0 for x in v)} |" for g, v in sorted(groups.items())]
            lines.append("")
    lines += ["## Every idea", "", "| shown (UTC) | coin | side | tf | kind | entry | stop | target | status | outcome | "
              "net R |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in scored:
        net = "-" if s["net_r"] is None else f"{s['net_r']:+.2f}"
        lines.append(f"| {s['shown_at']:%m-%d %H:%M} | {s.get('coin')} | {s['direction']} | {s['timeframe']} | "
                     f"{s.get('kind')} | {s['entry']} | {s['stop']} | {s['target']} | {s.get('status')} | "
                     f"{s['outcome']} | {net} |")
    if len(done) < 30:
        lines += ["", f"Only {len(done)} closed ideas: too few to judge an edge either way."]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=float, default=14.0, help="how far back to read ideas (default 14)")
    args = parser.parse_args()
    now = dt.datetime.now(dt.UTC)
    since = now - dt.timedelta(days=args.days)

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        async with conn.transaction(readonly=True):
            rows = await conn.fetch(IDEAS_SQL, since)
    finally:
        await conn.close()
    ideas = saved_ideas(rows)

    from cryptonite_storage.queries import AnalyticsQueryEngine

    lake = AnalyticsQueryEngine(Path(os.getenv("ANALYTICS_LAKE_PATH", "/app/data/analytics_lake")))
    try:
        scored = [{**i, **score_idea(i, bars_after(lake, i, now))} for i in ideas]
    finally:
        lake._con.close()
    print(render(scored, since, now))


if __name__ == "__main__":
    asyncio.run(main())
