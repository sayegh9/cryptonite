"""Research queue and morning digest (AI-first blueprint A4.4).

Ideas are queued on Redis (``research:queue``) by the operator, the Brain or the CLI and worked off one
at a time by ``python -m cryptonite_agents.research worker``, typically overnight. ``digest`` summarizes
the reports of the last N hours and publishes the summary on ``channel:system_alerts``.

Deviation from the blueprint: the research loop is iterative (draft -> backtest -> optimize -> review),
so Gemini's Batch API (results hours later) does not fit it; the loop runs on standard calls under the
``optional`` budget priority, which stops first when the day's budget runs low.
"""

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptonite_agents.research.loop import REPORTS_DIR, ResearchLoop, ResearchSpec

QUEUE_KEY = "research:queue"
DIGEST_CHANNEL = "channel:system_alerts"
TIMEFRAME_BAR = {"1m": "1-MINUTE", "5m": "5-MINUTE", "15m": "15-MINUTE", "30m": "30-MINUTE",
                 "1h": "1-HOUR", "4h": "4-HOUR", "1d": "1-DAY"}
DAY_NS = 86_400 * 10**9


def bar_type_for(instrument_id: str, timeframe: str) -> str:
    return f"{instrument_id}-{TIMEFRAME_BAR[timeframe]}-LAST-EXTERNAL"


def spec_from(item: dict[str, Any], now_ns: int | None = None) -> ResearchSpec:
    """Build in-sample / out-of-sample windows ending now: [warmup | in-sample | out-of-sample]."""
    now_ns = now_ns or time.time_ns()
    oos_days, is_days, warmup_days = item.get("oos_days", 30), item.get("is_days", 90), item.get("warmup_days", 10)
    oos_start = now_ns - oos_days * DAY_NS
    is_start = oos_start - is_days * DAY_NS
    return ResearchSpec(
        idea=item["idea"],
        instrument_id=item["instrument_id"],
        bar_type=bar_type_for(item["instrument_id"], item["timeframe"]),
        timeframe=item["timeframe"],
        in_sample=(is_start - warmup_days * DAY_NS, is_start, oos_start - 1),
        out_of_sample=(oos_start - warmup_days * DAY_NS, oos_start, now_ns),
        n_trials=int(item.get("n_trials", 12)),
    )


async def enqueue(redis, idea: str, instrument_id: str, timeframe: str, **options: Any) -> int:
    if timeframe not in TIMEFRAME_BAR:
        raise ValueError(f"unsupported timeframe {timeframe!r}")
    item = {"idea": idea, "instrument_id": instrument_id, "timeframe": timeframe, **options,
            "queued_at": datetime.now(UTC).isoformat()}
    return await redis.rpush(QUEUE_KEY, json.dumps(item))


async def work_once(redis, loop: ResearchLoop, block_s: int = 5) -> str | None:
    """Run the next queued idea, if any; returns the report id."""
    popped = await redis.blpop(QUEUE_KEY, timeout=block_s)
    if not popped:
        return None
    item = json.loads(popped[1])
    report = await loop.run(spec_from(item))
    return report.id


def build_digest(reports_dir: Path = REPORTS_DIR, hours: int = 24, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=hours)
    reports = []
    for path in sorted(reports_dir.glob("*.json")) if reports_dir.exists() else []:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if datetime.fromisoformat(data["created_at"]) >= since:
            reports.append(data)
    by_status: dict[str, int] = {}
    for r in reports:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    lines = [f"Research digest, last {hours} h: {len(reports)} idea(s) — "
             + (", ".join(f"{n} {s}" for s, n in sorted(by_status.items())) or "none")]
    for r in sorted(reports, key=lambda r: r["status"] != "PROMOTE"):
        oos = r.get("out_of_sample") or {}
        lines.append(
            f"- [{r['status']}] {r.get('strategy_name') or r['idea'][:60]}: OOS Sharpe {oos.get('sharpe_ratio', 'n/a')}, "
            f"return {oos.get('total_return', 'n/a')}, trades {oos.get('total_trades', 'n/a')} — {r['reason'][:160]}"
        )
    return {"text": "\n".join(lines), "count": len(reports), "by_status": by_status,
            "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in reports), 4)}


async def publish_digest(redis, reports_dir: Path = REPORTS_DIR, hours: int = 24) -> dict[str, Any]:
    digest = build_digest(reports_dir, hours)
    await redis.publish(DIGEST_CHANNEL, json.dumps({"type": "RESEARCH_DIGEST", "severity": "INFO", **digest}))
    return digest
