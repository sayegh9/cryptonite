"""H10: a coin's open paper positions across its spot, spot-margin and perpetual instruments.

An entry debate on ``BTCUSDT.BINANCE`` must know about an open ``BTCUSDT-PERP.BINANCE`` short. Each row is one owner's
position in one instrument: ownership and exposure are never merged across owners or markets. The orchestrator's
execution-time recheck reads the same rows, so a debate entry approved after an opposite position opened is refused.
"""
from __future__ import annotations

from typing import Any

from cryptonite_schemas.mandate import canonical_instrument, mandate_id_from_instance
from cryptonite_schemas.profile import Market, instrument_for, market_of

OPPOSITE_POSITION_OPEN = "OPPOSITE_POSITION_OPEN"

_RELATED_SQL = """
    SELECT p.instrument_id, p.instance_id, p.side, p.quantity, p.entry_price, p.leverage, p.margin_mode,
           p.liquidation_price, p.opened_at
    FROM paper_positions p
    WHERE p.account_id = $1 AND p.status = 'OPEN' AND p.quantity > 0 AND p.instrument_id = ANY($2::text[])
    ORDER BY p.instrument_id, p.opened_at, p.instance_id
"""


def related_instruments(instrument_id: str) -> dict[str, str]:
    """{market: instrument id} of the coin's spot, spot-margin and perpetual instruments."""
    return {m.value: instrument_for(instrument_id, m) for m in Market}


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def read_related_positions(conn, instrument_id: str, account_id: str) -> list[dict[str, Any]]:
    """Every open position (one row per owner and instrument) on the coin's related instruments. Raises on a failed
    read: the caller records that as an unresolved position state, never as 'no position'."""
    instruments = [canonical_instrument(i) for i in related_instruments(instrument_id).values()]
    rows = await conn.fetch(_RELATED_SQL, account_id, instruments)
    mids = {m for r in rows if (m := mandate_id_from_instance(r["instance_id"]))}
    names = ({r["id"]: r["name"] for r in await conn.fetch("SELECT id, name FROM ai_mandates WHERE id = ANY($1::uuid[])",
                                                            list(mids))} if mids else {})
    out = []
    for r in rows:
        mid = mandate_id_from_instance(r["instance_id"])
        out.append({
            "instrument_id": r["instrument_id"], "market": market_of(r["instrument_id"]).value,
            "side": r["side"], "quantity": _float(r["quantity"]), "entry_price": _float(r["entry_price"]),
            "leverage": int(r["leverage"] or 1), "margin_mode": r["margin_mode"],
            "liquidation_price": _float(r["liquidation_price"]),
            "owner_instance": r["instance_id"], "mandate_id": str(mid) if mid else None,
            "mandate_name": names.get(mid),
            "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None,
        })
    return out


def opposite_positions(positions: list[dict[str, Any]], side: str) -> list[dict[str, Any]]:
    """The positions a ``side`` (BUY / SELL) entry would trade against: a BUY against a SHORT, a SELL against a LONG."""
    against = {"BUY": "SHORT", "SELL": "LONG"}.get(str(side).upper())
    return [p for p in positions if against and p.get("side") == against]


def position_label(p: dict[str, Any]) -> str:
    """'BTC perp SHORT 2x' style label for one position."""
    from cryptonite_schemas.profile import base_asset

    market = {"SPOT": "spot", "SPOT_MARGIN": "margin", "PERP": "perp"}.get(p.get("market"), p.get("market"))
    lev = p.get("leverage") or 1
    return f"{base_asset(p['instrument_id'])} {market} {p.get('side')}" + (f" {lev}x" if lev and lev != 1 else "")
