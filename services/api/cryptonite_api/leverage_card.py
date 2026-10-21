"""The leverage plan of a perp order card (leverage plan §2.1, Stage 4), and the manual ticket's preview (4.5).

The card's dry run and its approval value a perp entry with ``plan_leveraged_order``, exactly as the orchestrator
sizes it (``cryptonite_orchestrator.mandate_gate.plan_perp_entry``): the allocation is the margin, the leverage the
highest the ceilings allow (at most what the card or the operator asks for), the notional within the mandate's room
(as the mandate check values it, market buffer included) and 25% of account equity, the loss at the stop within the
mandate's risk budget, with the same sizing margin. The API has no dispatcher, so the entry price is the perp's mark
(``perp:mark:<SYM>``, the price the dispatcher values a perp at), the equity is the paper account's, and the bracket
tables are loaded from the Redis cache the derivatives feed fills.
"""

import json
import logging
from decimal import Decimal
from typing import Any

from cryptonite_execution.brackets import load_brackets
from cryptonite_execution.leverage import (
    ACCOUNT_EXPOSURE_CAP,
    ACCOUNT_MAX_LEVERAGE,
    DEFAULT_FEE_ROUND_TRIP,
    MANUAL_RISK_PER_TRADE_PCT,
    LeverageLimits,
    LeveragePlan,
    check_leveraged_order,
    liquidation_distance,
    plan_leveraged_order,
    stop_distance,
)
from cryptonite_execution.mandate_store import entry_room_pct
from cryptonite_execution.mandates import MARKET_BUFFER
from cryptonite_execution.margin import bracket_source
from cryptonite_orchestrator.mandate_gate import (
    PERP_SIZING_EXTRA_SLIPPAGE,
    PERP_SIZING_MARGIN,
    grade_ceiling,
    mandate_risk_budget,
)
from cryptonite_schemas.mandate import Mandate, canonical_instrument
from cryptonite_schemas.marks import read_marks
from cryptonite_schemas.runtime_settings import (
    LEVERAGE_KEYS,
    default_leverage_settings,
    read_leverage_settings,
)

logger = logging.getLogger("cryptonite.api.leverage_card")

PAPER_ACCOUNT_ID = "PAPER-ACCOUNT-001"
MIN_ORDER_EQUITY = Decimal("0.01")  # the dispatcher's 1% minimum order
# A leverage at or above this needs a second confirmation on approve (leverage plan 4.8)
HIGH_LEVERAGE_CONFIRM = 8


def is_perp_entry(card: dict[str, Any]) -> bool:
    """A perp card that adds risk: BUY opens or adds to a long, SELL to a short; ``reduce_pct`` makes it a reduction."""
    return canonical_instrument(card["instrument_id"]).split(".")[0].endswith("-PERP") and not card.get("reduce_pct")


async def perp_mark(redis, instrument_id: str) -> tuple[Decimal | None, Decimal | None]:
    """(mark, funding rate) from the derivatives feed's cache (15 s TTL: a present key is fresh), or (None, None)."""
    if redis is None:
        return None, None
    sym = canonical_instrument(instrument_id).split(".")[0].removesuffix("-PERP")
    try:
        raw = await redis.get(f"perp:mark:{sym}")
        if not raw:
            return None, None
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        rate = data.get("funding_rate")
        return Decimal(str(data["mark_price"])), Decimal(str(rate)) if rate is not None else None
    except Exception as e:
        logger.warning("Could not read the perp mark of %s: %s", instrument_id, e)
        return None, None


async def card_entry_price(redis, card: dict[str, Any]) -> Decimal | None:
    """The price a card's entry is measured from for its R:R (ADR 0010): its limit price, else the perp's mark or the
    spot's last price from the market cache. None when there is none (the R:R filter is then skipped)."""
    limit = card.get("limit_price") if str(card.get("order_type", "MARKET")).upper() == "LIMIT" else None
    if limit:
        return Decimal(str(limit))
    inst = canonical_instrument(card["instrument_id"])
    if inst.split(".")[0].endswith("-PERP"):
        return (await perp_mark(redis, inst))[0]
    if redis is None:
        return None
    try:
        raw = await redis.get(f"market:24h:{inst.split('.')[0]}")
        price = Decimal(str(json.loads(raw.decode() if isinstance(raw, bytes) else raw).get("last_price") or 0)) if raw else None
        return price if price and price > 0 else None
    except Exception as e:
        logger.warning("Could not read the last price of %s: %s", inst, e)
        return None


async def card_leverage_plan(pool, redis, mandate: Mandate, card: dict[str, Any], *,
                             requested_leverage: int | None = None,
                             shrink_margin_to_fit: bool = False) -> LeveragePlan:
    """The plan approve would queue for a perp entry card now (read only). ``shrink_margin_to_fit``: cut a margin that
    not even 1x fits down to what does, as the orchestrator sizes an entry (a debate's perp entry, whose allocation is
    the model's, not the operator's); a Brain card keeps it off, so its own allocation is refused rather than cut."""
    inst = canonical_instrument(card["instrument_id"])
    side = str(card["side"]).upper()
    if redis is not None:
        try:
            await load_brackets(redis)
        except Exception as e:  # the cached tables stay as they are; a missing table refuses the entry
            logger.warning("Could not load leverage brackets: %s", e)
    mark, funding = await perp_mark(redis, inst)
    limit = card.get("limit_price") if str(card.get("order_type", "MARKET")).upper() == "LIMIT" else None
    entry = Decimal(str(limit)) if limit else mark
    if entry is None or entry <= 0:
        return LeveragePlan(inst, side, "NO_MARK_PRICE", 0, requested_leverage, 0, None, False)
    marks = await read_marks(redis)
    async with pool.acquire() as conn:
        room_pct = await entry_room_pct(conn, mandate, inst, marks)
        equity = await conn.fetchval("SELECT total_equity FROM paper_accounts WHERE account_id = $1", PAPER_ACCOUNT_ID)
        rows = await conn.fetch("SELECT key, value FROM system_settings WHERE key = ANY($1)", list(LEVERAGE_KEYS))
    settings, _problems = read_leverage_settings({r["key"]: r["value"] for r in rows}, default_leverage_settings())
    budget = mandate.capital_budget_usd
    buffer = MARKET_BUFFER if limit is None else Decimal("0")
    room_usd = budget * room_pct / Decimal("100") / (1 + buffer)  # the mandate check adds the market buffer
    equity_d = Decimal(str(equity)) if equity is not None else None
    max_notional = room_usd if equity_d is None else min(room_usd, ACCOUNT_EXPOSURE_CAP * equity_d)
    limits = LeverageLimits(
        risk_budget_usd=mandate_risk_budget(mandate) * PERP_SIZING_MARGIN,
        max_notional_usd=max(Decimal("0"), max_notional) * PERP_SIZING_MARGIN,
        max_leverage=mandate.max_leverage,
        grade_ceiling=grade_ceiling({"setup_grade": card.get("setup_grade")}, settings.grade_ceilings),
        slippage_pct=Decimal(str(settings.slippage_pct)) / Decimal("100") + PERP_SIZING_EXTRA_SLIPPAGE,
        fee_pct=DEFAULT_FEE_ROUND_TRIP,
        min_notional_usd=(MIN_ORDER_EQUITY * equity_d / PERP_SIZING_MARGIN) if equity_d is not None else Decimal("0"),
    )
    margin = budget * Decimal(str(card["allocation_pct"])) / Decimal("100")
    stop = Decimal(str(card["stop_loss"])) if card.get("stop_loss") else None
    requested = requested_leverage if requested_leverage is not None else card.get("leverage")
    return plan_leveraged_order(inst, side, entry, stop, margin, limits, requested_leverage=requested,
                                budget_usd=budget, funding_rate=funding, shrink_margin_to_fit=shrink_margin_to_fit)


async def _equity_and_slippage(pool) -> tuple[Decimal | None, Decimal]:
    async with pool.acquire() as conn:
        equity = await conn.fetchval("SELECT total_equity FROM paper_accounts WHERE account_id = $1", PAPER_ACCOUNT_ID)
        rows = await conn.fetch("SELECT key, value FROM system_settings WHERE key = ANY($1)", list(LEVERAGE_KEYS))
    settings, _problems = read_leverage_settings({r["key"]: r["value"] for r in rows}, default_leverage_settings())
    return (Decimal(str(equity)) if equity is not None else None), Decimal(str(settings.slippage_pct)) / Decimal("100")


def _s(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


async def manual_ticket_preview(pool, redis, instrument_id: str, side: str, quantity: Decimal, leverage: int, *,
                                price: Decimal | None = None, stop_loss: Decimal | None = None) -> dict[str, Any]:
    """What the dispatcher would make of a manual perp entry (read only). The ticket fixes the quantity, so the notional
    does not move with the leverage: each pill (1..10x) is judged with ``check_leveraged_order``, the dispatcher's own
    check (bracket, and with a stop the liquidation buffer and a loss at the stop within 2% of equity), and the
    chosen leverage gets its margin, ISOLATED liquidation price, buffer, losses and funding. The margin mode the
    operator picks does not change the check; a CROSS liquidation price is not shown here (it depends on the wallet)."""
    inst = canonical_instrument(instrument_id)
    side = side.upper()
    if redis is not None:
        try:
            await load_brackets(redis)
        except Exception as e:
            logger.warning("Could not load leverage brackets: %s", e)
    mark, funding = await perp_mark(redis, inst)
    entry = price if price else mark
    base: dict[str, Any] = {"instrument_id": inst, "side": side, "leverage": leverage, "margin_mode": "ISOLATED",
                            "account_max_leverage": ACCOUNT_MAX_LEVERAGE, "stop_loss": _s(stop_loss)}
    if entry is None or entry <= 0:
        return {**base, "ok": False, "reject": "NO_MARK_PRICE", "pills": {}, "recommended_leverage": None}
    if (bracket_reject := bracket_source(inst).entry_reject) is not None:
        return {**base, "ok": False, "reject": bracket_reject, "pills": {}, "recommended_leverage": None}
    equity, slippage = await _equity_and_slippage(pool)
    notional = quantity * entry
    risk_budget = equity * MANUAL_RISK_PER_TRADE_PCT / Decimal("100") if (equity is not None and stop_loss) else None
    fee = DEFAULT_FEE_ROUND_TRIP
    # Leverage-independent refusals of the dispatcher: the 1% minimum order and the 25% concentration limit
    order_reject = None
    if equity is not None and notional < MIN_ORDER_EQUITY * equity:
        order_reject = "ORDER_BELOW_MIN_NOTIONAL"
    elif equity is not None and notional > ACCOUNT_EXPOSURE_CAP * equity:
        order_reject = "CONCENTRATION_LIMIT_BREACHED"
    elif stop_loss and stop_distance(side, entry, stop_loss) is None:
        order_reject = "STOP_WRONG_SIDE"
    pills = {lev: order_reject or check_leveraged_order(inst, side, quantity, entry, stop_loss, lev,
                                                        risk_budget_usd=risk_budget, slippage_pct=slippage, fee_pct=fee)
             for lev in range(1, ACCOUNT_MAX_LEVERAGE + 1)}
    allowed = [lev for lev, why in pills.items() if why is None]
    reject = pills.get(leverage, "LEVERAGE_ABOVE_MAXIMUM")
    margin = notional / Decimal(leverage)
    try:
        liq, dist = liquidation_distance(inst, side, quantity, entry, margin)
    except ValueError:
        liq, dist = None, None
    d = stop_distance(side, entry, stop_loss) if stop_loss else None
    loss = notional * (d + slippage + fee) if d is not None else None
    sign = Decimal("1") if side == "BUY" else Decimal("-1")
    return {
        **base, "ok": reject is None, "reject": reject, "pills": {str(k): v for k, v in pills.items()},
        "recommended_leverage": max(allowed) if allowed else None,
        "entry_price": _s(entry), "quantity": _s(quantity), "notional_usd": _s(notional), "margin_usd": _s(margin),
        "liquidation_price": _s(liq), "liquidation_distance_pct": _s(dist * 100 if dist is not None else None),
        "buffer_used_pct": _s((d + slippage) / dist * 100 if d is not None and dist else None),
        "loss_at_stop_usd": _s(loss), "loss_at_stop_pct": _s(loss / equity * 100 if loss is not None and equity else None),
        "loss_if_liquidated_usd": _s(margin), "risk_budget_usd": _s(risk_budget),
        "funding_8h_usd": _s(-sign * notional * funding if funding is not None else None),
    }
