"""The orchestrator side of the mandate check (AI-first blueprint §4.4, A5.1).

Every AI-originated order command resolves its mandate here and passes ``check_mandate`` before it
reaches ``GuardedOrderDispatcher``. Nothing here changes the dispatcher or its limits; a command the
mandate check accepts is still evaluated by the dispatcher in full.

An order is AI-originated when it is a ``SUBMIT_ORDER`` (the AI order command), targets an AI
instance, or was requested by an ``ai:`` service. Such an order without a mandate is rejected
(``MANDATE_REQUIRED``): no AI order escapes its mandate.
"""

import json
import logging
import uuid
from decimal import Decimal
from typing import Any

from cryptonite_execution.mandates import (
    MandateExposure,
    MandateOrder,
    check_mandate,
    same_instrument,
)
from cryptonite_schemas.mandate import (
    BREACH_CODES,
    ApprovalStatus,
    AutonomyLevel,
    Mandate,
    MandateReject,
    is_ai_instance,
)

logger = logging.getLogger("cryptonite.orchestrator.mandates")

MANDATE_COLUMNS = ("id, name, capital_budget_usd, instruments, max_position_pct, max_drawdown_pct, allow_perps, "
                   "max_leverage, strategies, autonomy_level, status, active_hours_utc, veto_window_s")


def is_ai_command(cmd_type: str, target: str | None, requester: str | None) -> bool:
    return cmd_type == "SUBMIT_ORDER" or is_ai_instance(target) or str(requester or "").startswith("ai:")


def _row_get(row: Any, key: str) -> Any:
    try:
        return row.get(key)
    except (AttributeError, KeyError):
        return None


def command_mandate_id(row: Any, payload: dict) -> str | None:
    value = _row_get(row, "mandate_id") or payload.get("mandate_id")
    return str(value) if value else None


async def load_mandate(conn, mandate_id: str | uuid.UUID) -> Mandate | None:
    try:
        mid = uuid.UUID(str(mandate_id))
    except ValueError:
        return None
    row = await conn.fetchrow(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE id = $1", mid)
    return Mandate.from_row(row) if row else None


async def ensure_mandate_instance(conn, mandate: Mandate, account_id: str) -> bool:
    """Book the mandate's orders and positions under its own paper instance (created on first use,
    status SYSTEM so the strategy loader never tries to run it)."""
    await conn.execute(
        """
        INSERT INTO paper_strategy_instances (
            instance_id, account_id, strategy_name, strategy_version_id, strategy_source_hash, risk_policy_hash,
            last_processed_bar_ns, instrument_id, timeframe, resolved_parameters, allocation_usd, status,
            created_at, updated_at)
        SELECT $1, $2, 'AIMandate', v.id, 'ai_mandate', 'ai_mandate', 0, 'AI.MANDATE', '1m',
               jsonb_build_object('mandate_id', $3::text), $4, 'SYSTEM', now(), now()
        FROM (SELECT strategy_version_id AS id FROM paper_strategy_instances WHERE instance_id = 'AI_DEBATE_WORKER'
              UNION ALL SELECT id FROM (SELECT id FROM strategy_versions ORDER BY created_at LIMIT 1) s) v
        LIMIT 1
        ON CONFLICT (instance_id) DO NOTHING
        """,
        mandate.instance_id, account_id, str(mandate.id), mandate.capital_budget_usd,
    )
    return bool(await conn.fetchval("SELECT 1 FROM paper_strategy_instances WHERE instance_id = $1", mandate.instance_id))


async def mandate_exposure(conn, engine, mandate: Mandate, instrument_id: str, ref_prices: dict[str, Decimal]) -> MandateExposure:
    """The mandate's open positions (DB, valued at their marked price) plus its working entry orders
    (dispatcher, at their reserved notional), and whether anyone else holds or works the instrument."""
    d = engine.dispatcher
    account_id = getattr(engine, "account_id", "PAPER-ACCOUNT-001")
    rows = await conn.fetch(
        """SELECT instance_id, instrument_id, side, quantity, current_price FROM paper_positions
           WHERE account_id = $1 AND status = 'OPEN' AND quantity > 0
             AND (instance_id = $2 OR instrument_id = $3)""",
        account_id, mandate.instance_id, instrument_id,
    )
    gross = inst_usd = inst_qty = Decimal("0")
    inst_side = None
    other_owner = False
    for r in rows:
        qty = Decimal(str(r["quantity"]))
        value = qty * Decimal(str(ref_prices.get(r["instrument_id"]) or r["current_price"]))
        if r["instance_id"] == mandate.instance_id:
            gross += value
            if same_instrument(r["instrument_id"], instrument_id):
                inst_usd += value
                inst_qty += qty
                inst_side = r["side"]
        elif same_instrument(r["instrument_id"], instrument_id):
            other_owner = True
    for info in getattr(d, "open_orders", {}).values():
        entry = not info.get("reduce_only") and (info.get("is_perp") or info.get("side") == "BUY")
        if info.get("instance_id") == mandate.instance_id:
            if entry:
                notional = Decimal(str(info.get("reserved_notional") or 0))
                if info.get("is_perp"):
                    intent = info.get("intent")
                    px = getattr(intent, "price", None) or ref_prices.get(info.get("instrument_id"))
                    notional = Decimal(str(info.get("quantity") or 0)) * Decimal(str(px or 0))
                gross += notional
                if same_instrument(info.get("instrument_id", ""), instrument_id):
                    inst_usd += notional
            elif same_instrument(info.get("instrument_id", ""), instrument_id):
                inst_qty -= Decimal(str(info.get("quantity") or 0))  # already being sold
        elif same_instrument(info.get("instrument_id", ""), instrument_id):
            other_owner = True
    return MandateExposure(gross, inst_usd, max(inst_qty, Decimal("0")), inst_side, other_owner)


async def approval_granted(conn, row: Any, payload: dict, mandate: Mandate) -> bool:
    """ADD_RISK below L3 needs an operator approval bound to exactly this command: an ``ai_approvals``
    row for this mandate, ADD_RISK, APPROVED (or, at L2, a veto window that ran out with policy
    PROCEED), whose command is this one."""
    approval_id = payload.get("approval_id")
    if not approval_id:
        return False
    try:
        aid = uuid.UUID(str(approval_id))
    except ValueError:
        return False
    a = await conn.fetchrow(
        "SELECT mandate_id, action_class, status, command_id FROM ai_approvals WHERE id = $1", aid,
    )
    if a is None or str(a["mandate_id"]) != str(mandate.id) or a["action_class"] != "ADD_RISK":
        return False
    if a["command_id"] is None or str(a["command_id"]) != str(_row_get(row, "command_id")):
        return False
    if a["status"] == ApprovalStatus.APPROVED:
        return True
    return a["status"] == ApprovalStatus.EXPIRED_PROCEEDED and mandate.autonomy_level >= AutonomyLevel.L2


async def gate_ai_order(conn, engine, row: Any, payload: dict, intent, mandate: Mandate | None, quote) -> str | None:
    """None when the AI order may go on to the dispatcher, else the reject reason for the command."""
    if mandate is None:
        return MandateReject.REQUIRED if not command_mandate_id(row, payload) else MandateReject.NOT_FOUND
    side = intent.side.upper()
    ref = intent.price
    if ref is None and quote is not None:
        ref = Decimal(str(quote.ask_price if side == "BUY" else quote.bid_price))
    ref_prices = {intent.instrument_id: ref} if ref else {}
    order = MandateOrder(
        instrument_id=intent.instrument_id, side=side, quantity=intent.quantity, ref_price=ref,
        order_type=intent.order_type, reduce_only=intent.reduce_only, leverage=intent.leverage,
        conviction_level=intent.conviction_level,
    )
    exposure = await mandate_exposure(conn, engine, mandate, intent.instrument_id, ref_prices)
    ok = await approval_granted(conn, row, payload, mandate)
    reject = check_mandate(mandate, order, exposure, approval_ok=ok)
    if reject is None and not await ensure_mandate_instance(conn, mandate, getattr(engine, "account_id", "PAPER-ACCOUNT-001")):
        reject = MandateReject.INSTANCE_UNAVAILABLE
    if reject is not None:
        logger.warning("AI order %s rejected by mandate %s: %s", _row_get(row, "command_id"), mandate.name, reject)
        if reject in BREACH_CODES:
            await record_breach(conn, mandate, reject, row, payload)
    return reject.value if reject is not None else None


async def record_breach(conn, mandate: Mandate, reject: MandateReject, row: Any, payload: dict) -> None:
    """Hook for the autonomy state machine (A5.5); here only logged."""
    logger.warning("Mandate breach attempt on %s: %s (%s)", mandate.name, reject, json.dumps(payload, default=str)[:300])
