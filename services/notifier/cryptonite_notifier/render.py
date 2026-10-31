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


def _money(v: Any) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "unknown"


def _price(v: Any) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "none"


def perp_lines(payload: dict[str, Any]) -> list[str]:
    """The perp fields a Brain perp card shows, for a perp entry approval (a debate's, auto-trade Phase B): side, the
    setup grade and its reasons, the leverage the dry run allows and its binding ceiling, the liquidation price and
    the loss at the stop. Empty for any other approval."""
    plan = payload.get("perp_plan") if isinstance(payload, dict) else None
    if not isinstance(plan, dict):
        return []
    grade = payload.get("grade") if isinstance(payload.get("grade"), dict) else {}
    reasons = grade.get("reasons") or payload.get("grade_reasons") or []
    loss_pct = plan.get("loss_at_stop_pct")
    pct = f" ({float(loss_pct):.2f}% of budget)" if loss_pct is not None else ""
    return [
        f"Perp {payload.get('perp_side') or ('LONG' if plan.get('side') == 'BUY' else 'SHORT')} · ISOLATED · "
        f"stop {payload.get('stop_loss')} · target {payload.get('take_profit')}",
        f"Grade {payload.get('setup_grade') or grade.get('grade') or 'ungraded'}"
        + (f": {'; '.join(str(r) for r in reasons[:6])}" if reasons else ""),
        f"Leverage the dry run allows: {plan.get('leverage')}x (binding: {plan.get('binding_ceiling')})",
        f"Liquidation {_price(plan.get('liquidation_price'))} · loss at stop {_money(plan.get('loss_at_stop_usd'))}{pct}",
        "The orchestrator sizes it again at execution (plan_leveraged_order).",
    ]


def approval_text(row: dict[str, Any], approval: dict[str, Any], now: datetime.datetime) -> str:
    add = approval["action_class"] == "ADD_RISK"
    label = "🟡 ADD RISK" if add else "🟢 REDUCE RISK"
    timeout = "no" if approval["timeout_policy"] == "DENY" else "proceeds"
    lines = [
        f"{label} · {row['title']}          ⏱ {countdown(approval['expires_at'], now)}",
        approval["summary"] or row.get("body") or "",
        *perp_lines(approval.get("payload") or {}),
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
    profile = alert.get("mandate", "Profile")
    if kind == "PROFILE_BREAKER":
        return f"{profile} · breaker", f"Profile paused: {alert.get('reason', '')}. Exits continue."
    if kind == "PROFILE_BREAKER_RESUMED":
        return f"{profile} · resumed", f"Profile resumed automatically: {alert.get('reason', '')}."
    if kind == "PROFILE_FILL":
        return f"{profile} · fill", (
            f"{alert.get('side')} {alert.get('market')} {alert.get('instrument_id')} · {alert.get('mode')}\n"
            f"Quantity {alert.get('qty')} @ {_price(alert.get('price'))} · {alert.get('leverage')}x · "
            f"stop {_price(alert.get('stop'))}")
    if kind == "PROFILE_EXIT":
        return f"{profile} · exit", (
            f"{alert.get('side')} {alert.get('market')} {alert.get('instrument_id')} · {alert.get('reason')}\n"
            f"Quantity {alert.get('qty')} @ {_price(alert.get('price'))} · "
            f"P&L {_money(alert.get('realized_pnl'))} · {alert.get('r_multiple') if alert.get('r_multiple') is not None else 'unknown '}R")
    if kind == "PROFILE_DAILY_SUMMARY":
        refusals = alert.get('refusals')
        digest = f"\nRefusals: {refusals}" if refusals is not None else ''
        return f"{profile} · daily summary", (
            f"{alert.get('day', '')} · P&L {_money(alert.get('pnl'))} · {alert.get('trades')} trades\n"
            f"Wins {alert.get('wins')} / losses {alert.get('losses')} · "
            f"Breaker events {alert.get('breaker_events')}{digest}")
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
