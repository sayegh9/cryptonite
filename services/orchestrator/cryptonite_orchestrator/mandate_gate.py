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

from cryptonite_execution.autonomy import AutonomyState, AutonomyTrigger, transition
from cryptonite_execution.mandate_store import (
    apply_transition,
    instance_spot_holdings,
    record_event,
)
from cryptonite_execution.mandates import (
    MandateExposure,
    MandateOrder,
    action_class,
    check_mandate,
    max_entry_quantity,
    same_instrument,
)
from cryptonite_schemas.mandate import (
    BREACH_CODES,
    ActionClass,
    ApprovalStatus,
    AutonomyLevel,
    Mandate,
    MandateReject,
    is_ai_instance,
)

logger = logging.getLogger("cryptonite.orchestrator.mandates")


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
    """The mandate's holdings (spot: net fills from ``paper_orders``; perps: the dispatcher's position
    owned by its instance), valued at the live/marked price, plus its working entry orders; and whether
    anyone else holds or works the instrument."""
    d = engine.dispatcher
    account_id = getattr(engine, "account_id", "PAPER-ACCOUNT-001")
    marks = {r["instrument_id"]: Decimal(str(r["current_price"])) for r in await conn.fetch(
        "SELECT instrument_id, current_price FROM paper_positions WHERE instance_id = $1 AND status = 'OPEN'",
        mandate.instance_id)}
    gross = inst_usd = inst_qty = Decimal("0")
    inst_side = None
    for inst, (qty, avg_px) in (await instance_spot_holdings(conn, mandate.instance_id)).items():
        if qty <= 0:
            continue
        value = qty * Decimal(str(ref_prices.get(inst) or marks.get(inst) or avg_px))
        gross += value
        if same_instrument(inst, instrument_id):
            inst_usd += value
            inst_qty += qty
            inst_side = "LONG"
    for inst, pos in getattr(d, "perp_positions", {}).items():
        if getattr(pos, "instance_id", None) == mandate.instance_id and pos.signed_qty != 0:
            px = Decimal(str(ref_prices.get(inst) or d.mark_prices.get(inst) or pos.entry_price))
            value = abs(pos.signed_qty) * px
            gross += value
            if same_instrument(inst, instrument_id):
                inst_usd += value
                inst_qty += abs(pos.signed_qty)
                inst_side = "LONG" if pos.signed_qty > 0 else "SHORT"
    other_owner = bool(await conn.fetchval(
        """SELECT 1 FROM paper_positions WHERE account_id = $1 AND status = 'OPEN' AND quantity > 0
           AND instrument_id = $2 AND instance_id <> $3 LIMIT 1""",
        account_id, instrument_id, mandate.instance_id))
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


def mandate_ref_price(limit_price: Decimal | None, side: str, quote) -> Decimal | None:
    """The one price a mandate values an order at, for sizing and for the check alike: its limit price, else the
    live ask (buy) or bid (sell). Sizing at one price and checking at another refused entries the orchestrator had
    just sized to fit (2026-10-13: a limit above the ask)."""
    if limit_price is not None and limit_price > 0:
        return limit_price
    if quote is None:
        return None
    return Decimal(str(quote.ask_price if side.upper() == "BUY" else quote.bid_price))


async def mandate_entry_room(conn, engine, mandate: Mandate, instrument_id: str, side: str,
                             limit_price: Decimal | None, quote,
                             order_type: str) -> tuple[Decimal, Decimal | None, MandateExposure | None]:
    """The largest entry quantity the mandate check will accept, the price it was valued at and the exposure: the
    same reference price and the same exposure valuation that ``gate_ai_order`` uses."""
    ref = mandate_ref_price(limit_price, side, quote)
    if ref is None:
        return Decimal("0"), None, None
    exposure = await mandate_exposure(conn, engine, mandate, instrument_id, {instrument_id: ref})
    return max_entry_quantity(mandate, exposure, ref, order_type=order_type), ref, exposure


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


async def gate_ai_order(conn, engine, row: Any, payload: dict, intent, mandate: Mandate | None, quote, *,
                        system_sized: bool = False) -> str | None:
    """None when the AI order may go on to the dispatcher, else the reject reason for the command.

    ``system_sized``: the orchestrator chose the quantity (from the allocation, sized to the room). A budget or
    position-limit rejection of such an order means sizing and the check disagree: a system fault, so it is refused
    as ``SIZING_CONFLICT`` and logged as an error, and never recorded as the AI's breach."""
    if mandate is None:
        return MandateReject.REQUIRED if not command_mandate_id(row, payload) else MandateReject.NOT_FOUND
    side = intent.side.upper()
    ref = mandate_ref_price(intent.price, side, quote)
    ref_prices = {intent.instrument_id: ref} if ref else {}
    order = MandateOrder(
        instrument_id=intent.instrument_id, side=side, quantity=intent.quantity, ref_price=ref,
        order_type=intent.order_type, reduce_only=intent.reduce_only, leverage=intent.leverage,
        conviction_level=intent.conviction_level, margin_mode=intent.margin_mode,
    )
    exposure = await mandate_exposure(conn, engine, mandate, intent.instrument_id, ref_prices)
    ok = await approval_granted(conn, row, payload, mandate)
    reject = check_mandate(mandate, order, exposure, approval_ok=ok)
    if reject is None and action_class(order) is ActionClass.ADD_RISK:
        # F4: an extra refusal on top of the mandate check (never a breach); exits are never refused
        from cryptonite_orchestrator.cross_mandate import cross_mandate_reject
        cross = await cross_mandate_reject(conn, engine, mandate, order, ref_prices)
        if cross is not None:
            logger.warning("AI order %s refused by the cross-mandate exposure check: %s", _row_get(row, "command_id"), cross)
            return cross
    if reject is None and not await ensure_mandate_instance(conn, mandate, getattr(engine, "account_id", "PAPER-ACCOUNT-001")):
        reject = MandateReject.INSTANCE_UNAVAILABLE
    if reject in (MandateReject.BUDGET_EXCEEDED, MandateReject.POSITION_PCT_EXCEEDED) and system_sized:
        logger.error("AI order %s: the orchestrator sized it but mandate %s refused it (%s); sizing and the check "
                     "disagree. Refused as %s, not recorded as a breach", _row_get(row, "command_id"), mandate.name,
                     reject, MandateReject.SIZING_CONFLICT)
        return MandateReject.SIZING_CONFLICT.value
    if reject is not None:
        logger.warning("AI order %s rejected by mandate %s: %s", _row_get(row, "command_id"), mandate.name, reject)
        if reject in BREACH_CODES:
            await record_breach(conn, mandate, reject, row, payload)
    return reject.value if reject is not None else None


async def record_breach(conn, mandate: Mandate, reject: MandateReject, row: Any, payload: dict) -> None:
    """§4.5: any breach demotes one level (L1 stays L1). The BREACH event is what the gate counts."""
    detail = {"reject": reject.value, "command_id": str(_row_get(row, "command_id")),
              "instrument_id": payload.get("instrument_id"), "side": payload.get("side")}
    async with conn.transaction():
        await record_event(conn, mandate.id, "BREACH", from_level=int(mandate.autonomy_level),
                           to_level=int(mandate.autonomy_level), reason=reject.value, detail=detail,
                           actor="risk:mandate_check")
        t = transition(AutonomyState(mandate.autonomy_level, mandate.status, mandate.allow_perps),
                       AutonomyTrigger.BREACH, reason=f"breach: {reject.value}")
        await apply_transition(conn, mandate.id, t, actor="risk:mandate_check", detail=detail)
    logger.warning("Mandate breach on %s: %s (%s)", mandate.name, reject, json.dumps(detail, default=str)[:300])
