"""The orchestrator side of the mandate check (AI-first blueprint §4.4, A5.1).

Every AI-originated order command resolves its mandate here and passes ``check_mandate`` before it
reaches ``GuardedOrderDispatcher``. Nothing here changes the dispatcher or its limits; a command the
mandate check accepts is still evaluated by the dispatcher in full.

An order is AI-originated when it is a ``SUBMIT_ORDER`` (the AI order command), targets an AI
instance, or was requested by an ``ai:`` service. Such an order without a mandate is rejected
(``MANDATE_REQUIRED``): no AI order escapes its mandate.
"""

import datetime
import json
import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from cryptonite_execution import profile_lifecycle
from cryptonite_execution.leverage import (
    ACCOUNT_EXPOSURE_CAP,
    DEFAULT_FEE_ROUND_TRIP,
    DEFAULT_GRADE_CEILINGS,
    DEFAULT_SLIPPAGE_ALLOWANCE,
    LeverageLimits,
    LeveragePlan,
    check_leveraged_order,
    plan_leveraged_order,
    plan_spot_margin_order,
)
from cryptonite_execution.mandate_store import (
    instance_spot_holdings,
    load_mandate,
    record_event,
    session_verdict_for,
)
from cryptonite_execution.mandates import (
    MandateExposure,
    MandateOrder,
    action_class,
    check_mandate,
    max_entry_quantity,
    order_notional,
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
from cryptonite_schemas.profile import Market, base_asset, market_of
from cryptonite_schemas.session import SessionVerdict

logger = logging.getLogger("cryptonite.orchestrator.mandates")


async def fresh_autotrade_switch_on(conn) -> bool:
    """Read the committed switch at the final risk gate, without the display/routing cache.

    A savepoint keeps an unreadable setting from aborting the command transaction. Missing,
    malformed and unreadable values all count as OFF.
    """
    try:
        async with conn.transaction():
            raw = await conn.fetchval(
                # Hold the row through dispatch. An OFF update waits for this command to finish;
                # the next command then sees OFF rather than racing a cached ON decision.
                "SELECT value FROM system_settings WHERE key = $1 FOR SHARE",
                profile_lifecycle.AUTOTRADE_ENABLED_KEY,
            )
    except Exception as exc:
        logger.warning("Fresh auto-trade switch read failed; treating it as OFF: %s", exc)
        return False
    return profile_lifecycle.parse_settings({profile_lifecycle.AUTOTRADE_ENABLED_KEY: raw}).enabled


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


# Sizing keeps a margin to the check, so a mark tick between sizing and the dispatcher cannot flip a boundary: 1% of
# the risk budget and of the room, and 0.1% more slippage than the dispatcher allows
PERP_SIZING_MARGIN = Decimal("0.99")
PERP_SIZING_EXTRA_SLIPPAGE = Decimal("0.001")
# An AI perp entry without a system grade is held to grade C's ceiling (the safer reading of §2.3)
UNGRADED_PERP_GRADE = "C"


def mandate_risk_budget(mandate: Mandate) -> Decimal:
    """The loss at the stop one trade may take: the budget x ``max_risk_per_trade_pct`` (ADR 0009 D6)."""
    return mandate.capital_budget_usd * mandate.max_risk_per_trade_pct / Decimal("100")


def perp_entry_price(engine, instrument_id: str, limit_price: Decimal | None) -> Decimal | None:
    """The price a perp entry is valued at by the dispatcher, so sizing values it alike: its limit, else the mark."""
    if limit_price is not None and limit_price > 0:
        return limit_price
    mark = getattr(engine.dispatcher, "mark_prices", {}).get(instrument_id)
    return Decimal(str(mark)) if mark else None


def grade_ceiling(payload: dict, ceilings: dict[str, int] | None = None) -> int:
    grade = str(payload.get("setup_grade") or UNGRADED_PERP_GRADE).upper()
    table = ceilings or DEFAULT_GRADE_CEILINGS
    return table.get(grade, table[UNGRADED_PERP_GRADE])


async def plan_perp_entry(conn, engine, mandate: Mandate, instrument_id: str, side: str, payload: dict,
                          quote) -> LeveragePlan:
    """Size a mandate's perp entry, long or short (leverage plan 2.4): the margin is the allocation's share of the
    budget, the leverage the highest the ceilings allow (at most what the card asked for), and the notional fits the
    mandate's room (valued as the mandate check values it) and 25% of account equity."""
    d = engine.dispatcher
    order_type = str(payload.get("order_type", "MARKET")).upper()
    limit_px = Decimal(str(payload["price"])) if payload.get("price") else None
    entry = perp_entry_price(engine, instrument_id, limit_px)
    stop = Decimal(str(payload["stop_loss"])) if payload.get("stop_loss") else None
    margin = mandate.capital_budget_usd * Decimal(str(payload.get("allocation_pct", 5.0))) / Decimal("100")
    if entry is None:
        return LeveragePlan(instrument_id, side, "NO_MARK_PRICE", 0, payload.get("leverage"), 0, None, False)
    room_qty, _, _ = await mandate_entry_room(conn, engine, mandate, instrument_id, side, limit_px, quote, order_type)
    held = abs(d.get_perp_position_qty(instrument_id)) * entry if hasattr(d, "get_perp_position_qty") else Decimal("0")
    equity = d.portfolio_equity()
    account_room = ACCOUNT_EXPOSURE_CAP * equity - held
    max_notional = max(Decimal("0"), min(room_qty * entry, account_room)) * PERP_SIZING_MARGIN
    limits = LeverageLimits(
        risk_budget_usd=mandate_risk_budget(mandate) * PERP_SIZING_MARGIN,
        max_notional_usd=max_notional,
        max_leverage=mandate.max_leverage,
        grade_ceiling=grade_ceiling(payload, getattr(engine, "grade_ceilings", None)),
        slippage_pct=getattr(d, "slippage_allowance_pct", DEFAULT_SLIPPAGE_ALLOWANCE) + PERP_SIZING_EXTRA_SLIPPAGE,
        fee_pct=2 * getattr(d, "perp_taker_fee", DEFAULT_FEE_ROUND_TRIP / 2),
        # the dispatcher's 1% minimum, with the same margin the other way
        min_notional_usd=getattr(d, "min_order_equity_pct", Decimal("0")) * equity / PERP_SIZING_MARGIN,
    )
    requested = int(payload["leverage"]) if payload.get("leverage") else None
    return plan_leveraged_order(instrument_id, side, entry, stop, margin, limits, requested_leverage=requested,
                                budget_usd=mandate.capital_budget_usd, shrink_margin_to_fit=True)


async def plan_spot_margin_entry(conn, engine, mandate: Mandate, instrument_id: str, side: str, payload: dict,
                                 quote) -> LeveragePlan:
    """Size an isolated spot-margin entry to the mandate's room and 25% account clamp."""
    order_type = str(payload.get("order_type", "MARKET")).upper()
    limit_px = Decimal(str(payload["price"])) if payload.get("price") else None
    entry = mandate_ref_price(limit_px, side, quote)
    stop = Decimal(str(payload["stop_loss"])) if payload.get("stop_loss") else None
    margin = mandate.capital_budget_usd * Decimal(str(payload.get("allocation_pct", 5.0))) / Decimal("100")
    if entry is None:
        return LeveragePlan(instrument_id, side, "NO_MARK_PRICE", 0, payload.get("leverage"), 0, None, False)
    room_qty, _, _ = await mandate_entry_room(conn, engine, mandate, instrument_id, side, limit_px, quote, order_type)
    d = engine.dispatcher
    held = abs(d.get_position_qty(instrument_id)) * entry if hasattr(d, "get_position_qty") else Decimal("0")
    equity = d.portfolio_equity()
    max_notional = max(Decimal("0"), min(room_qty * entry, ACCOUNT_EXPOSURE_CAP * equity - held)) * PERP_SIZING_MARGIN
    pair = instrument_id.split("-", 1)[0].split(".", 1)[0].upper()
    runtime_pairs = getattr(getattr(engine, "margin_settings", None), "pair_settings", None)
    pair_settings = runtime_pairs.get(pair, {}) if runtime_pairs is not None else None
    limits = LeverageLimits(
        risk_budget_usd=mandate_risk_budget(mandate) * PERP_SIZING_MARGIN,
        max_notional_usd=max_notional,
        max_leverage=mandate.max_leverage,
        slippage_pct=getattr(d, "slippage_allowance_pct", DEFAULT_SLIPPAGE_ALLOWANCE) + PERP_SIZING_EXTRA_SLIPPAGE,
        fee_pct=getattr(d, "spot_taker_fee", Decimal("0.001")),
        min_notional_usd=getattr(d, "min_order_equity_pct", Decimal("0")) * equity / PERP_SIZING_MARGIN,
    )
    requested = int(payload["leverage"]) if payload.get("leverage") else None
    return plan_spot_margin_order(
        instrument_id, side, entry, stop, margin, limits, requested_leverage=requested,
        pair_settings=pair_settings, base_asset=base_asset(instrument_id), budget_usd=mandate.capital_budget_usd,
        shrink_margin_to_fit=True,
    )


def perp_entry_reject(engine, mandate: Mandate, intent) -> str | None:
    """The mandate gate's check of a perp entry, with the dispatcher's own valuation: a stop is required, and it must
    sit inside half the liquidation distance with its loss within the mandate's risk budget."""
    if intent.stop_loss is None:
        return "STOP_REQUIRED"
    d = engine.dispatcher
    entry = perp_entry_price(engine, intent.instrument_id, intent.price)
    if entry is None:
        return None  # no mark: the dispatcher refuses it (NO_MARK_PRICE)
    return check_leveraged_order(
        intent.instrument_id, intent.side, intent.quantity, entry, intent.stop_loss, intent.leverage or 1,
        risk_budget_usd=mandate_risk_budget(mandate),
        slippage_pct=getattr(d, "slippage_allowance_pct", DEFAULT_SLIPPAGE_ALLOWANCE),
        fee_pct=2 * getattr(d, "perp_taker_fee", DEFAULT_FEE_ROUND_TRIP / 2),
    )


async def approval_granted(conn, row: Any, payload: dict, mandate: Mandate, *, autotrade_on: bool = False) -> bool:
    """ADD_RISK below L3 needs an operator approval bound to exactly this command: an ``ai_approvals``
    row for this mandate, ADD_RISK, APPROVED (or, at L2, a veto window that ran out with policy
    PROCEED), whose command is this one. While the master switch is off (``autotrade_on`` False) a veto window that ran
    out proves nothing: only an operator's APPROVED counts (ADR 0011 D10)."""
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
    return (autotrade_on and a["status"] == ApprovalStatus.EXPIRED_PROCEEDED
            and mandate.autonomy_level >= AutonomyLevel.L2)


async def coin_lock_owner(conn, instrument_id: str) -> uuid.UUID | None:
    """The mandate holding the coin lock on the instrument's base asset (ADR 0011 D6), loaded once per check. An
    instrument whose base asset cannot be read, or a database without the lock table, reads as no owner."""
    try:
        return await conn.fetchval(
            """SELECT l.mandate_id FROM ai_profile_coin_locks l JOIN ai_mandates m ON m.id = l.mandate_id
               WHERE l.base_asset = $1 AND m.status IN ('ACTIVE', 'PAUSED', 'STOPPING', 'HALTED')""",
            base_asset(instrument_id))
    except Exception as e:
        logger.debug("coin lock lookup unavailable for %s: %s", instrument_id, e)
        return None


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
    action = action_class(order)
    if action is ActionClass.ADD_RISK:
        mandate = await load_mandate(conn, mandate.id, for_update=True)
        if mandate is None:
            return MandateReject.NOT_FOUND
        if market_of(intent.instrument_id) is Market.SPOT and side == "BUY":
            try:
                stop = Decimal(str(intent.stop_loss))
                entry = Decimal(str(ref))
                ask = Decimal(str(quote.ask_price))
            except (AttributeError, InvalidOperation, TypeError, ValueError):
                return "STOP_REQUIRED"
            if (not stop.is_finite() or not entry.is_finite() or not ask.is_finite()
                    or stop <= 0 or entry <= 0 or ask <= 0 or stop >= entry or stop >= ask):
                return "STOP_REQUIRED"
    exposure = await mandate_exposure(conn, engine, mandate, intent.instrument_id, ref_prices)
    coin_owner = await coin_lock_owner(conn, intent.instrument_id)
    # The switch is authoritative only for an entry. Exits never consult it.
    switch_on = await fresh_autotrade_switch_on(conn) if action is ActionClass.ADD_RISK else True
    ok = await approval_granted(conn, row, payload, mandate, autotrade_on=switch_on)
    # ADR 0010: today's session (loss latch, profit target, profit lock), read at execution, so an entry approved
    # before the latch is refused too. Only an ADD_RISK order reads it: a failed read can never fail an exit
    session = SessionVerdict.ACTIVE
    if action_class(order) is ActionClass.ADD_RISK:
        session = await session_verdict_for(conn, mandate, datetime.datetime.now(datetime.UTC))
    reject = check_mandate(mandate, order, exposure, approval_ok=ok, session=session, coin_owner=coin_owner,
                         autotrade_on=switch_on)
    if reject is MandateReject.BUDGET_EXCEEDED and action_class(order) is ActionClass.ADD_RISK:
        # Cash exhaustion due to already-held exposure/reservations is an expected profile refusal, not an AI breach.
        # Preserve BUDGET_EXCEEDED when the cash check does not establish that condition.
        cash_reject = await profile_cash_reject(conn, engine, mandate, order)
        if cash_reject is not None:
            return cash_reject
    if reject is None and order.is_perp and action_class(order) is ActionClass.ADD_RISK:
        # Leverage plan 2.4: the same valuation the sizing used; a refusal, never a breach
        if (perp_reject := perp_entry_reject(engine, mandate, intent)) is not None:
            logger.warning("AI perp entry %s refused by mandate %s: %s", _row_get(row, "command_id"), mandate.name, perp_reject)
            return perp_reject
    if reject is None and action_class(order) is ActionClass.ADD_RISK:
        cash_reject = await profile_cash_reject(conn, engine, mandate, order)
        if cash_reject is not None:
            return cash_reject
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


async def profile_cash_reject(conn, engine, mandate: Mandate, order: MandateOrder) -> str | None:
    """ADR 0011 D11: only this profile's cash and margin may fund its entries.

    Serialize against the profile row for the caller's claim transaction. Spot cost, posted perp margin and
    working entry reservations consume its budget. Durable reservations use the reservation manager's active
    states; an order present in memory is counted there once, rather than also counting its durable reservation.
    The account's remaining cash is still independently guarded by GuardedOrderDispatcher.
    """
    if action_class(order) is ActionClass.REDUCE_RISK:
        return None
    await conn.fetchrow("SELECT id FROM ai_mandates WHERE id = $1 FOR UPDATE", mandate.id)
    d = engine.dispatcher
    used = sum((cost for cost in (await instance_spot_cost_basis(conn, mandate.instance_id)).values()), Decimal("0"))
    used += sum((pos.initial_margin for pos in getattr(d, "perp_positions", {}).values()
                 if getattr(pos, "instance_id", None) == mandate.instance_id and pos.signed_qty != 0), Decimal("0"))
    working_ids = []
    for cid, info in getattr(d, "open_orders", {}).items():
        if info.get("instance_id") != mandate.instance_id or info.get("reduce_only"):
            continue
        if info.get("is_perp") or info.get("side") == "BUY":
            working_ids.append(cid)
            used += Decimal(str(info.get("reserved_notional") or 0))
    durable_working = await conn.fetchval(
        """SELECT COALESCE(SUM(
                    CASE WHEN c.payload->>'profile_cash_reserve_usd' IS NOT NULL THEN
                         (p.quantity - p.filled_qty) / NULLIF(p.quantity, 0) *
                         (c.payload->>'profile_cash_reserve_usd')::numeric
                    WHEN COALESCE(p.price, p.avg_fill_price) IS NULL THEN $3::numeric
                    ELSE (p.quantity - p.filled_qty) * COALESCE(p.price, p.avg_fill_price, 0) *
                         CASE WHEN p.instrument_id LIKE '%-PERP.%' THEN
                             1 / GREATEST(COALESCE(NULLIF(c.payload->>'leverage', '')::numeric, 1), 1) + 0.0005
                              ELSE 1.001 END
                    END), 0)
           FROM paper_orders p
           LEFT JOIN execution_commands c
             ON c.payload->>'client_order_id' = p.client_order_id
             OR p.client_order_id IN (
                 'AI-' || upper(substr(replace(c.command_id::text, '-', ''), 1, 12)),
                 'MANUAL-' || upper(substr(replace(c.command_id::text, '-', ''), 1, 12)))
           WHERE p.instance_id = $1 AND p.status IN ('PENDING', 'SUBMITTED', 'ACCEPTED')
             AND p.filled_qty < p.quantity AND p.order_type <> 'STOP_MARKET'
             AND (p.side = 'BUY' OR p.instrument_id LIKE '%-PERP.%')
             AND COALESCE(c.payload->>'reduce_only', 'false') <> 'true'
             AND NOT EXISTS (
                 SELECT 1 FROM execution_reservations r
                 WHERE r.client_order_id = p.client_order_id AND r.account_id = p.account_id
                   AND r.environment = 'PAPER' AND r.asset = 'USDT'
                   AND r.state IN ('PRE_SEND', 'INFLIGHT', 'ACKED_UNREFLECTED'))
             AND NOT (p.client_order_id = ANY($2::text[]))""",
        mandate.instance_id, working_ids, mandate.capital_budget_usd)
    used += Decimal(str(durable_working or 0))
    reserved = await conn.fetchval(
        """SELECT COALESCE(SUM(r.amount + r.fee_slippage_headroom), 0)
           FROM execution_reservations r JOIN execution_commands c ON c.command_id = r.command_id
           WHERE (c.mandate_id = $1 OR c.payload->>'mandate_id' = $1::text)
             AND r.account_id = $2 AND r.environment = 'PAPER' AND r.asset = 'USDT'
             AND r.state IN ('PRE_SEND', 'INFLIGHT', 'ACKED_UNREFLECTED')
             AND NOT (r.client_order_id = ANY($3::text[]))""",
        mandate.id, getattr(engine, "account_id", "PAPER-ACCOUNT-001"), working_ids)
    notional = order_notional(order)
    if notional is None:
        return None  # the mandate and dispatcher refuse an entry without a usable price
    fee = getattr(d, "perp_taker_fee", Decimal("0.0005")) if order.is_perp else getattr(d, "taker_fee", Decimal("0.001"))
    needed = notional / Decimal(order.leverage or 1) if order.is_perp else notional
    needed += notional * fee
    # A single instruction larger than the profile's entire budget remains a mandate breach. This refusal
    # covers otherwise affordable entries that no longer fit because existing holdings/orders consumed cash.
    return ("PROFILE_CASH_EXHAUSTED"
            if needed <= mandate.capital_budget_usd
            and used + Decimal(str(reserved or 0)) + needed > mandate.capital_budget_usd else None)


async def instance_spot_cost_basis(conn, instance_id: str) -> dict[str, Decimal]:
    """Return remaining spot cost by instrument, resetting after each round trip to flat."""
    rows = await conn.fetch(
        """SELECT instrument_id, side, filled_qty, COALESCE(avg_fill_price, price, 0) AS fill_price
           FROM paper_orders
           WHERE instance_id = $1 AND filled_qty > 0 AND instrument_id NOT LIKE '%-PERP.%'
           ORDER BY created_at, updated_at, client_order_id""",
        instance_id,
    )
    positions: dict[str, tuple[Decimal, Decimal]] = {}
    for row in rows:
        side = row.get("side")
        if side not in ("BUY", "SELL"):
            continue
        instrument_id = row["instrument_id"]
        quantity, cost = positions.get(instrument_id, (Decimal("0"), Decimal("0")))
        filled_qty = Decimal(str(row["filled_qty"]))
        fill_price = Decimal(str(row["fill_price"]))
        if side == "BUY":
            quantity += filled_qty
            cost += filled_qty * fill_price
        elif quantity > 0:
            closed_qty = min(filled_qty, quantity)
            cost -= (cost / quantity) * closed_qty
            quantity -= closed_qty
            if quantity == 0:
                cost = Decimal("0")
        positions[instrument_id] = (quantity, max(cost, Decimal("0")))
    return {instrument_id: cost for instrument_id, (quantity, cost) in positions.items() if quantity > 0}


async def record_breach(conn, mandate: Mandate, reject: MandateReject, row: Any, payload: dict) -> None:
    """ADR 0011 D3: a breach is recorded and alerted; it never changes the execution mode. The BREACH event is what the
    health score counts."""
    detail = {"reject": reject.value, "command_id": str(_row_get(row, "command_id")),
              "instrument_id": payload.get("instrument_id"), "side": payload.get("side")}
    await record_event(conn, mandate.id, "BREACH", from_level=int(mandate.autonomy_level),
                       to_level=int(mandate.autonomy_level), reason=reject.value, detail=detail,
                       actor="risk:mandate_check")
    from cryptonite_orchestrator.mandate_monitor import (
        alert,  # lazy: the monitor imports this package's siblings
    )

    await alert(conn, None, "MANDATE_BREACH", f"Mandate breach — {mandate.name}: {reject.value}. Mode unchanged.",
                {"mandate_id": str(mandate.id), "mandate": mandate.name, **detail}, tier="ACTION",
                dedupe_key=f"MANDATE_BREACH:{mandate.id}:{detail['command_id']}")
    logger.warning("Mandate breach on %s: %s (%s)", mandate.name, reject, json.dumps(detail, default=str)[:300])
