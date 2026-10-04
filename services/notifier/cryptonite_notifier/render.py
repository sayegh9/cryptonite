"""Message templates (AI-first blueprint §7.2). Plain text: no parse mode, nothing to escape.

Risk-adding and risk-reducing actions differ in colour *and* label (§7 principles).
"""

import datetime
from typing import Any

from cryptonite_schemas import notifier as contract

TIER_ICON = {"INFO": "🔵 INFO", "ACTION": "🟡 ACTION", "CRITICAL": "🔴 CRITICAL"}


def countdown(expires_at: datetime.datetime, now: datetime.datetime) -> str:
    left = max(0, int((expires_at - now).total_seconds()))
    return f"{left // 60:02d}:{left % 60:02d}"


def approval_text(row: dict[str, Any], approval: dict[str, Any], now: datetime.datetime) -> str:
    add = approval["action_class"] == "ADD_RISK"
    label = "🟡 ADD RISK" if add else "🟢 REDUCE RISK"
    timeout = "no" if approval["timeout_policy"] == "DENY" else "proceeds"
    lines = [
        f"{label} · {row['title']}          ⏱ {countdown(approval['expires_at'], now)}",
        approval["summary"] or row.get("body") or "",
        ("Approving new risk needs your TOTP code." if add else "Risk reducing: one tap."),
        f"At {approval['expires_at'].astimezone(datetime.UTC):%H:%M} UTC the answer is {timeout}.",
    ]
    if approval.get("trace_id"):
        lines.append(f"Why: trace #{str(approval['trace_id'])[:8]}")
    return "\n".join(lines)


def approval_buttons(secret: str, approval: dict[str, Any]) -> list[list[dict[str, str]]]:
    add = approval["action_class"] == "ADD_RISK"
    return [[
        {"text": "Approve 🔐" if add else "Approve", "callback_data": contract.callback_data(secret, "a", approval["id"])},
        {"text": "Veto", "callback_data": contract.callback_data(secret, "v", approval["id"])},
        {"text": "Details", "callback_data": contract.callback_data(secret, "d", approval["id"])},
    ]]


def plain_text(tier: str, title: str, body: str) -> str:
    return f"{TIER_ICON.get(tier, tier)} · {title}\n{body}".strip()


def alert_text(alert: dict[str, Any]) -> tuple[str, str]:
    """(title, body) for a system alert from ``channel:system_alerts`` or ``send_alert``."""
    kind = str(alert.get("kind") or alert.get("reason") or alert.get("type") or "ALERT")
    if alert.get("message"):
        return kind.replace("_", " ").title(), str(alert["message"])
    detail = ", ".join(f"{k}={v}" for k, v in alert.items() if k not in ("type", "v", "severity", "kind", "message"))
    return kind.replace("_", " ").title(), detail


def details_text(approval: dict[str, Any]) -> str:
    p = approval.get("payload") or {}
    keys = ("instrument_id", "side", "order_type", "allocation_pct", "quantity", "price", "reason", "source")
    lines = [f"Approval {approval['id']}", f"{approval['action_class']} · {approval['command_type']} · {approval['status']}",
             f"Mandate level at creation: L{approval['level_at_creation']}"]
    lines += [f"{k}: {p[k]}" for k in keys if p.get(k) is not None]
    if approval.get("trace_id"):
        lines.append(f"Trace: {approval['trace_id']} (open it in Mission Control)")
    return "\n".join(lines)
