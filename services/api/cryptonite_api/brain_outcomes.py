"""What an approved Brain order card's order actually did (2026-10-13).

An approved card used to read "Approved" forever, even when the orchestrator refused the order a second later. A card
now follows its paper command to its order: WORKING (a resting limit), FILLED, or NOT_EXECUTED (refused, failed, or
cancelled with nothing filled), with the reason. The change is written into the conversation once (the worker's
``expect_status`` makes it exactly-once), as a system line, so the Brain knows on the next turn.

``sync_session_cards`` runs when a conversation is loaded, before each Brain turn, and when the web asks about one
card while it waits.
"""

import json
import uuid
from decimal import Decimal
from typing import Any

# Execution states a card can show after APPROVED; FILLED and NOT_EXECUTED are final
FOLLOWED = ("APPROVED", "WORKING")
FINAL = ("FILLED", "NOT_EXECUTED")
PENDING_COMMAND = ("PENDING", "CLAIMED", "RUNNING", "SENDING")
DONE_ORDER = ("FILLED",)
DEAD_ORDER = ("CANCELED", "CANCELLED", "REJECTED", "EXPIRED")


def client_order_id(command_id: str, payload: dict[str, Any]) -> str:
    """The paper order id the orchestrator gives an AI command (``AI-`` + the first 12 hex digits of the command)."""
    return payload.get("client_order_id") or f"AI-{str(command_id).replace('-', '')[:12].upper()}"


def _num(value: Any) -> str | None:
    if value is None:
        return None
    d = Decimal(str(value))
    return format(d.normalize(), "f") if d else None


async def command_outcome(conn, command_id: str) -> dict[str, Any] | None:
    """The state of an approved card's order: QUEUED, WORKING, FILLED or NOT_EXECUTED, with the reason or the fill.
    None when the command is unknown."""
    try:
        cid = uuid.UUID(str(command_id))
    except ValueError:
        return None
    cmd = await conn.fetchrow("SELECT status, error_message, payload FROM execution_commands WHERE command_id = $1", cid)
    if cmd is None:
        return None
    if cmd["status"] in PENDING_COMMAND:
        return {"state": "QUEUED"}
    if cmd["status"] in ("REJECTED", "FAILED"):
        return {"state": "NOT_EXECUTED", "reason": cmd["error_message"] or f"command {cmd['status'].lower()}"}
    payload = cmd["payload"] if isinstance(cmd["payload"], dict) else json.loads(cmd["payload"] or "{}")
    order = await conn.fetchrow(
        """SELECT status, quantity, filled_qty, avg_fill_price, price, reject_reason
           FROM paper_orders WHERE client_order_id = $1""", client_order_id(str(cid), payload))
    if order is None:
        plan = payload.get("dry_run_plan")
        if cmd["status"] == "COMPLETED" and isinstance(plan, dict):  # a dry-run profile records the order, places none
            return {"state": "NOT_EXECUTED", "reason": (
                f"Dry run: profile in dry run, order recorded (qty {plan.get('quantity')} at "
                f"{plan.get('entry_price')}), not placed")}
        return {"state": "QUEUED"}  # dispatched, not booked yet
    filled = Decimal(str(order["filled_qty"] or 0))
    fill = {"filled_qty": _num(order["filled_qty"]), "avg_fill_price": _num(order["avg_fill_price"])}
    if order["status"] in DONE_ORDER:
        return {"state": "FILLED", **fill}
    if order["status"] in DEAD_ORDER:
        if filled > 0:  # partly filled, then the rest was cancelled: a position exists
            return {"state": "FILLED", **fill, "reason": f"partly filled; the rest was {order['status'].lower()}"}
        return {"state": "NOT_EXECUTED", "reason": order["reject_reason"] or f"order {order['status'].lower()}"}
    return {"state": "WORKING", "limit_price": _num(order["price"]), **fill}


def outcome_line(card: dict[str, Any], out: dict[str, Any]) -> str:
    """The system line the conversation records when a card's order changes state."""
    what = f"{card.get('side')} {card.get('instrument_id')}"
    cmd = card.get("command_id")
    if out["state"] == "FILLED":
        extra = f" ({out['reason']})" if out.get("reason") else ""
        return (f"Order for card {card['id']} FILLED: {what}, {out.get('filled_qty')} at {out.get('avg_fill_price')}"
                f"{extra}. Paper command {cmd}.")
    if out["state"] == "WORKING":
        return (f"Order for card {card['id']} is WORKING: {what} limit at {out.get('limit_price')}, not filled yet. "
                f"Paper command {cmd}.")
    return (f"Order for card {card['id']} was NOT EXECUTED: {out.get('reason')}. No position was opened. "
            f"Paper command {cmd}.")


def followed_cards(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Approved order cards whose order may still change state."""
    return [w for m in session.get("messages", []) for w in (m.get("widgets") or [])
            if w.get("kind") == "order_card" and w.get("command_id") and w.get("status") in FOLLOWED]


def card_patch(card: dict[str, Any], out: dict[str, Any]) -> dict[str, Any] | None:
    """The note that moves ``card`` to the state of ``out``, or None when nothing changed."""
    state = out["state"]
    if state not in ("WORKING", *FINAL) or state == card.get("status"):
        return None
    return {"widget_id": card["id"], "status": state, "expect_status": card["status"],
            "execution_reason": (out.get("reason") or None) and str(out["reason"])[:300],
            "filled_qty": out.get("filled_qty"), "avg_fill_price": out.get("avg_fill_price"),
            "content": outcome_line(card, out)}


# --- Debate cards (WS-3C): a debate card follows its run to the verdict ---------------------------------------------

# The queue the Brain's debate trigger writes to. A run records its message as "<stream>/<id>"; the tool returns "<id>"
DEBATE_STREAM = "stream:debate_queue"
DEBATE_FOLLOWED = ("QUEUED", "RUNNING")  # a debate card whose run may still change
DEBATE_FINAL = ("VERDICT", "NO_TRADE", "GATE_REFUSED", "FAILED")
APPROVAL_TAIL = {  # how a verdict line words its approval: the operator's decision, or what became of it
    "PENDING": "waiting for your approval", "APPROVED": "approved by the operator", "VETOED": "vetoed",
    "EXPIRED": "expired with no order", "EXPIRED_PROCEEDED": "expired and proceeded", "CANCELLED": "cancelled"}
GATE_REASONS = {  # the words for a gate refusal, in the conversation line (the codes are shown too)
    "KILL_SWITCH_LATCHED": "the kill switch is latched",
    "DAILY_LOSS_LATCHED": "the daily-loss latch is on",
    "POSITION_AT_CAP": "the position is at its cap",
}


def _plain(value: Any) -> str:
    """A number as the conversation prints it: 0.62 reads 0.62, 5.00 reads 5, 0 reads 0."""
    return format(Decimal(str(value)).normalize(), "f")


async def debate_read(conn, message_ref: str) -> tuple[dict[str, Any], str | None]:
    """The state of a Brain debate card's run, found by the message id the staging tool returned: QUEUED (no run yet),
    RUNNING, VERDICT (its decision, with the approval of that run), NO_TRADE, GATE_REFUSED (the safety gate refused
    it before any model call), or FAILED. Read only. Also returns the run's own status (None with no run): the
    conversation line words the approval by it, since a run that needed no approval has no approval row."""
    run = await conn.fetchrow(
        """SELECT id, status, failure_reason FROM debate_runs
           WHERE stream_message_id = $1 OR stream_message_id = $2 ORDER BY created_at DESC LIMIT 1""",
        message_ref, f"{DEBATE_STREAM}/{message_ref}")
    if run is None:
        return {"status": "QUEUED", "run_id": None, "verdict": None, "reason": None}, None
    run_id, status = str(run["id"]), run["status"]

    def out(state: str, verdict: dict[str, Any] | None = None, reason: str | None = None) -> dict[str, Any]:
        return {"status": state, "run_id": run_id, "verdict": verdict, "reason": reason}

    if status in ("PENDING", "RUNNING"):
        return out("RUNNING" if status == "RUNNING" else "QUEUED"), status
    if status in ("NO_TRADE", "NO_PRICE"):
        return out("NO_TRADE", reason=status), status
    if status == "GATE_REFUSED":
        return out("GATE_REFUSED", reason=run["failure_reason"]), status
    if status == "FAILED":
        return out("FAILED", reason=run["failure_reason"]), status
    if status in ("PENDING_APPROVAL", "COMPLETED", "REJECTED"):
        decision = await conn.fetchrow(
            """SELECT recommendation, conviction_score, allocation_pct FROM portfolio_decisions
               WHERE run_id = $1 ORDER BY created_at DESC LIMIT 1""", run["id"])
        if decision is None:  # a decided run with no decision cannot show a verdict: it is not followed further
            return out("FAILED", reason="NO_DECISION"), status
        # approve writes the run's approval as source 'debate', source_ref = the run id (one per run)
        approval = await conn.fetchrow(
            """SELECT status FROM ai_approvals WHERE source = 'debate' AND source_ref = $1
               ORDER BY created_at DESC LIMIT 1""", run_id)
        verdict = {"recommendation": decision["recommendation"],
                   "conviction_score": float(decision["conviction_score"]),
                   "allocation_pct": float(decision["allocation_pct"]),
                   "approval_status": approval["status"] if approval else None}
        return out("VERDICT", verdict=verdict), status
    return out("FAILED", reason=status), status  # a status this code does not know cannot be followed to a verdict


async def debate_outcome(conn, message_ref: str) -> dict[str, Any]:
    """The card's state (see debate_read) as the route returns it."""
    return (await debate_read(conn, message_ref))[0]


def followed_debates(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Debate cards whose run may still change: queued or running, with the message id the run is recorded under."""
    return [w for m in session.get("messages", []) for w in (m.get("widgets") or [])
            if w.get("kind") == "debate_summary" and w.get("message_ref") and w.get("status") in DEBATE_FOLLOWED]


def debate_line(card: dict[str, Any], out: dict[str, Any], run_status: str | None = None) -> str:
    """The system line a debate card's final state writes. Server-built from the run's own values, never model text."""
    inst = card.get("instrument_id")
    state = out["status"]
    if state == "VERDICT":
        v = out["verdict"]
        approval = v["approval_status"]
        if approval:
            tail = APPROVAL_TAIL.get(approval, f"approval {approval.lower()}")
        elif run_status == "COMPLETED":  # sent under a mandate with no approval step: nothing for the operator to do
            tail = "no approval needed"
        elif run_status == "REJECTED":
            tail = "rejected"
        else:  # PENDING_APPROVAL: the operator decides
            tail = "waiting for approval"
        return (f"Debate for {inst}: verdict {v['recommendation']} {_plain(v['allocation_pct'])}% "
                f"(conviction {_plain(v['conviction_score'])}), {tail}.")
    reason = out.get("reason") or "UNKNOWN"
    if state == "NO_TRADE":
        return f"Debate for {inst} ended with no trade ({reason})."
    if state == "GATE_REFUSED":
        why = GATE_REASONS.get(reason)
        if why:
            return f"Debate for {inst} was not run: {why} ({reason})."
        return f"Debate for {inst} was not run ({reason})."
    return f"Debate for {inst} failed ({reason})."


def debate_patch(card: dict[str, Any], out: dict[str, Any], run_status: str | None = None) -> dict[str, Any] | None:
    """The note that moves a debate card to its final state, or None while the run is still going."""
    if out["status"] not in DEBATE_FINAL:
        return None
    return {"widget_id": card["id"], "status": out["status"], "expect_status": card["status"],
            "content": debate_line(card, out, run_status)}
