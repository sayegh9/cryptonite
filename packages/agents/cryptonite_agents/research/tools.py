"""Research tools for agents (the Brain): queue an idea, read a report.

Queuing starts paid work later (drafting, backtests, a Pro review), so it is flagged
``side_effects`` and evals strip it. It changes no exposure, and it does not queue anything itself:
the request waits for the operator's approval (research_approvals, migration 042).
"""

import json
import os

import httpx
from cryptonite_agents.gateway import RiskClass, tool
from cryptonite_agents.research.loop import REPORTS_DIR
from cryptonite_agents.research.queue import TIMEFRAME_BAR


def queue_strategy_research(idea: str, instrument_id: str, timeframe: str) -> str:
    """Propose a trading idea for overnight research (the system drafts a strategy, backtests and optimizes
    it, checks it out of sample and reviews it). Nothing runs until the operator approves it in Research
    Lab. timeframe is one of 1m, 5m, 15m, 30m, 1h, 4h, 1d; instrument_id like BTCUSDT.BINANCE."""
    if timeframe not in TIMEFRAME_BAR:
        return f"Error: timeframe must be one of {', '.join(TIMEFRAME_BAR)}"
    token = os.getenv("CRYPTONITE_AGENT_API_TOKEN", "")
    if not token:
        return "Error: the research approval is not configured (no API token)"
    base = os.getenv("API_BASE_URL", "http://api:8000").rstrip("/")
    body = {
        "kind": "QUEUE_RESEARCH",
        "summary": f"Research {instrument_id} {timeframe}: {idea[:300]}",
        "payload": {"idea": idea, "instrument_id": instrument_id, "timeframe": timeframe},
    }
    try:
        resp = httpx.post(f"{base}/api/v1/research/approvals", json=body,
                          headers={"X-API-Token": token}, timeout=10.0)
    except httpx.HTTPError as e:
        return f"Error: could not request the approval ({e})"
    if resp.status_code >= 400:
        return f"Error: could not request the approval ({resp.status_code})"
    approval_id = resp.json().get("id", "")
    return (f"Requested. Waiting for the operator's approval in Research Lab (approval {approval_id}); "
            "nothing is queued until it is approved.")


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
