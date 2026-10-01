"""Research tools for agents (the Brain): queue an idea, read a report.

Queuing starts paid work later (drafting, backtests, a Pro review), so it is flagged
``side_effects`` and evals strip it; it changes no exposure (READ).
"""

import json
import os

from cryptonite_agents.gateway import RiskClass, tool
from cryptonite_agents.research.loop import REPORTS_DIR
from cryptonite_agents.research.queue import QUEUE_KEY, TIMEFRAME_BAR


def queue_strategy_research(idea: str, instrument_id: str, timeframe: str) -> str:
    """Queue a trading idea for overnight research: the system drafts a strategy, backtests and
    optimizes it in-sample, checks it out-of-sample and reviews it. Returns the queue position.
    timeframe is one of 1m, 5m, 15m, 30m, 1h, 4h, 1d; instrument_id like BTCUSDT.BINANCE."""
    if timeframe not in TIMEFRAME_BAR:
        return f"Error: timeframe must be one of {', '.join(TIMEFRAME_BAR)}"
    import redis

    client = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))
    try:
        position = client.rpush(QUEUE_KEY, json.dumps({"idea": idea, "instrument_id": instrument_id, "timeframe": timeframe}))
    finally:
        client.close()
    return f"Queued for research (position {position}). Results arrive in the research digest."


def read_research_report(report_id: str) -> str:
    """Read a finished research report by id: status, reason, best parameters, in-sample and
    out-of-sample metrics, overfitting flags and the reviewer's verdict."""
    safe = "".join(c for c in report_id if c.isalnum() or c == "-")
    path = REPORTS_DIR / f"{safe}.json"
    if not path.exists():
        return f"No research report {safe}"
    data = json.loads(path.read_text())
    data.pop("trials", None)
    return json.dumps(data, default=str)


QUEUE_RESEARCH = tool(RiskClass.READ, side_effects=True)(queue_strategy_research)
READ_RESEARCH_REPORT = tool(RiskClass.READ)(read_research_report)
