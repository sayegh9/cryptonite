"""The orchestrator side of the F4 cross-mandate exposure check (``cryptonite_execution.cross_exposure``).

Called from ``mandate_gate.gate_ai_order`` once ``check_mandate`` has accepted a risk-adding AI order,
before ``GuardedOrderDispatcher``. It gathers every mandate's exposure by instrument (the same sources the
mandate check uses: spot fills from ``paper_orders``, perps owned by the instance in the dispatcher, the
instance's working entry orders), values it at the order's reference price / the dispatcher's mark /
the last marked position price / the average fill price, and refuses the order when the combined
beta-weighted BTC exposure would exceed the cap. Every mandate that is not ARCHIVED counts, whatever its
status: a HALTED or PAUSED mandate's open positions are still account risk.
"""

from collections import defaultdict
from decimal import Decimal

from cryptonite_execution.cross_exposure import check_cross_mandate_exposure
from cryptonite_execution.mandates import MandateOrder, order_notional
from cryptonite_schemas.mandate import Mandate, canonical_instrument, mandate_instance_id

from cryptonite_orchestrator.risk_overrides import STATE, RiskState


async def mandate_instances(conn, current: Mandate) -> set[str]:
    rows = await conn.fetch("SELECT id FROM ai_mandates WHERE status <> 'ARCHIVED'")
    return {mandate_instance_id(r["id"]) for r in rows} | {current.instance_id}


async def cross_mandate_holdings(conn, engine, instances: set[str], ref_prices: dict[str, Decimal]) -> dict[str, Decimal]:
    """canonical instrument -> USD exposure summed over the given mandate instances."""
    from cryptonite_orchestrator.mandate_gate import instance_spot_holdings

    d = engine.dispatcher
    out: dict[str, Decimal] = defaultdict(Decimal)
    for instance_id in sorted(instances):
        marks = {r["instrument_id"]: Decimal(str(r["current_price"])) for r in await conn.fetch(
            "SELECT instrument_id, current_price FROM paper_positions WHERE instance_id = $1 AND status = 'OPEN'",
            instance_id)}
        for inst, (qty, avg_px) in (await instance_spot_holdings(conn, instance_id)).items():
            if qty <= 0:
                continue
            px = ref_prices.get(inst) or marks.get(inst) or avg_px
            out[canonical_instrument(inst)] += qty * Decimal(str(px))
    for inst, pos in getattr(d, "perp_positions", {}).items():
        if getattr(pos, "instance_id", None) in instances and pos.signed_qty != 0:
            px = ref_prices.get(inst) or d.mark_prices.get(inst) or pos.entry_price
            out[canonical_instrument(inst)] += abs(pos.signed_qty) * Decimal(str(px))
    for info in getattr(d, "open_orders", {}).values():
        if info.get("instance_id") not in instances or info.get("reduce_only"):
            continue
        if not (info.get("is_perp") or info.get("side") == "BUY"):
            continue
        inst = info.get("instrument_id", "")
        notional = Decimal(str(info.get("reserved_notional") or 0))
        if info.get("is_perp"):
            px = getattr(info.get("intent"), "price", None) or ref_prices.get(inst) or d.mark_prices.get(inst)
            notional = Decimal(str(info.get("quantity") or 0)) * Decimal(str(px or 0))
        out[canonical_instrument(inst)] += notional
    return dict(out)


async def cross_mandate_reject(conn, engine, mandate: Mandate, order: MandateOrder, ref_prices: dict[str, Decimal],
                               state: RiskState = STATE) -> str | None:
    """None when the risk-adding ``order`` fits under the cross-mandate cap, else the refusal text."""
    notional = order_notional(order)
    if notional is None:  # the mandate check refuses this first (NO_REFERENCE_PRICE)
        return None
    holdings = await cross_mandate_holdings(conn, engine, await mandate_instances(conn, mandate), ref_prices)
    equity = Decimal(str(engine.dispatcher.portfolio_equity()))
    return check_cross_mandate_exposure(holdings, order.instrument_id, notional, state.betas, equity, state.cross_cap_pct)
