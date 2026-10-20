"""The leverage plan of a perp order card (leverage plan §2.1, Stage 4).

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
    DEFAULT_FEE_ROUND_TRIP,
    LeverageLimits,
    LeveragePlan,
    plan_leveraged_order,
)
from cryptonite_execution.mandate_store import entry_room_pct
from cryptonite_execution.mandates import MARKET_BUFFER
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


async def card_leverage_plan(pool, redis, mandate: Mandate, card: dict[str, Any], *,
                             requested_leverage: int | None = None) -> LeveragePlan:
    """The plan approve would queue for a perp entry card now (read only)."""
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
                                budget_usd=budget, funding_rate=funding)
