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
