"""Leverage plan Stage 6 (Binance USD-M futures testnet, then live): what the executor does around a perp order.

Built and tested against a mock ``/fapi`` only. It is not wired into ``DemoExecutionPipeline`` yet: that wiring is
Stage 6 proper, after the Stage 5 paper gate passes (``scripts/perp_gate_status.py``).

- ``prepare_entry``: before every entry, read the symbol's margin type and leverage from the exchange
  (``positionRisk``) and set them only when they differ. The exchange is the truth (the operator can change leverage
  in the Binance app), so the state is read again after any change and an entry is refused if it still differs. A
  margin type that cannot change because a position or orders are open refuses the entry.
- ``protect_position``: right after an entry fills, the protective ``STOP_MARKET`` (``closePosition``, on the mark).
  If the exchange refuses it (the mark already went through the stop, -2021), the position is closed at once with a
  reduce-only market order: a leveraged position never stays open without a stop.
- ``reconcile``: the exchange's positions against the DB's (quantity, side, leverage, margin type).
- ``flatten_all``: the kill switch on the exchange: cancel every open perp order, close every position reduce-only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from cryptonite_execution.binance_adapter import BinanceFuturesAdapter, futures_symbol

QTY_TOLERANCE = Decimal("0.00000001")
# Binance codes this module turns into refusals
MARGIN_TYPE_LOCKED_CODES = ("[-4047]", "[-4048]")  # open orders / an open position: margin type cannot change
WOULD_TRIGGER_IMMEDIATELY = "[-2021]"


class PerpVenueError(RuntimeError):
    """A perp entry the venue state does not allow; ``code`` says why (the command's refusal reason)."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class VenueState:
    symbol: str
    margin_type: str  # ISOLATED | CROSS
    leverage: int
    position_amt: Decimal  # signed: > 0 long, < 0 short


def _state(row: Mapping) -> VenueState:
    margin = str(row.get("marginType", "")).upper()
    return VenueState(str(row["symbol"]), "CROSS" if margin in ("CROSS", "CROSSED") else margin,
                      int(row.get("leverage") or 0), Decimal(str(row.get("positionAmt") or "0")))


async def venue_state(adapter: BinanceFuturesAdapter, instrument: str) -> VenueState:
    symbol = futures_symbol(instrument)
    for row in await adapter.get_position_risk(symbol):  # one-way mode: one row per symbol (BOTH)
        if str(row.get("symbol")) == symbol:
            return _state(row)
    raise PerpVenueError("NO_VENUE_POSITION_ROW", f"positionRisk has no row for {symbol}")


async def prepare_entry(adapter: BinanceFuturesAdapter, instrument: str, leverage: int,
                        margin_mode: str = "ISOLATED") -> dict:
    """Set the symbol's margin type and leverage to what the entry needs, only where they differ. Returns what was
    changed; raises ``PerpVenueError`` when the entry must not be sent."""
    await adapter.ensure_one_way_mode()
    want_margin = "CROSS" if margin_mode.upper() in ("CROSS", "CROSSED") else margin_mode.upper()
    state = await venue_state(adapter, instrument)
    changed: list[str] = []
    if state.margin_type != want_margin:
        if state.position_amt != 0:
            raise PerpVenueError("MARGIN_TYPE_LOCKED",
                                 f"{state.symbol} is {state.margin_type} with an open position {state.position_amt}")
        try:
            await adapter.set_margin_type(instrument, want_margin)
        except RuntimeError as e:
            if any(code in str(e) for code in MARGIN_TYPE_LOCKED_CODES):
                raise PerpVenueError("MARGIN_TYPE_LOCKED", str(e)) from e
            raise
        changed.append("margin_type")
    if state.leverage != int(leverage):
        try:
            await adapter.set_leverage(instrument, int(leverage))
        except (RuntimeError, ValueError) as e:
            raise PerpVenueError("LEVERAGE_REFUSED", str(e)) from e
        changed.append("leverage")
    if changed:  # the exchange's answer is the truth: read it back
        after = await venue_state(adapter, instrument)
        if after.margin_type != want_margin or after.leverage != int(leverage):
            raise PerpVenueError("VENUE_STATE_MISMATCH", f"{after.symbol} reads {after.margin_type} {after.leverage}x "
                                                         f"after setting {want_margin} {leverage}x")
    return {"symbol": state.symbol, "margin_type": want_margin, "leverage": int(leverage), "changed": changed}


async def protect_position(adapter: BinanceFuturesAdapter, instrument: str, position_side: str, stop_price: Decimal,
                           quantity: Decimal, client_order_id: str) -> dict:
    """The protective stop right after an entry fills. If the exchange refuses it, close the position now."""
    exit_side = "SELL" if position_side.upper() == "LONG" else "BUY"
    try:
        stop = await adapter.submit_order(instrument, exit_side, "STOP_MARKET", None, None, client_order_id,
                                          stop_price=stop_price, close_position=True, working_type="MARK_PRICE")
        return {"stop": stop, "emergency_close": None}
    except RuntimeError as e:
        reason = "STOP_WOULD_TRIGGER_IMMEDIATELY" if WOULD_TRIGGER_IMMEDIATELY in str(e) else "STOP_REFUSED"
        close = await adapter.submit_order(instrument, exit_side, "MARKET", quantity, None, f"{client_order_id}-X",
                                           reduce_only=True)
        return {"stop": None, "emergency_close": close, "reason": f"{reason}: {e}"}


@dataclass(frozen=True)
class Mismatch:
    symbol: str
    field: str
    exchange: str
    db: str


def reconcile(exchange_rows: list[Mapping], db_positions: Mapping[str, tuple[Decimal, int, str]]) -> list[Mismatch]:
    """``db_positions``: symbol -> (signed quantity, leverage, margin mode). A flat symbol on both sides is fine;
    leverage and margin type are compared only where a position is open."""
    exchange = {s.symbol: s for s in (_state(r) for r in exchange_rows)}
    out: list[Mismatch] = []
    for symbol in sorted(set(exchange) | {futures_symbol(s) for s in db_positions}):
        ex = exchange.get(symbol)
        db = next((v for k, v in db_positions.items() if futures_symbol(k) == symbol), None)
        ex_qty = ex.position_amt if ex else Decimal("0")
        db_qty = db[0] if db else Decimal("0")
        if abs(ex_qty - db_qty) > QTY_TOLERANCE:
            out.append(Mismatch(symbol, "quantity", str(ex_qty), str(db_qty)))
            continue
        if ex_qty == 0 or ex is None or db is None:
            continue
        if ex.leverage != int(db[1]):
            out.append(Mismatch(symbol, "leverage", str(ex.leverage), str(db[1])))
        if ex.margin_type != str(db[2]).upper():
            out.append(Mismatch(symbol, "margin_type", ex.margin_type, str(db[2]).upper()))
    return out


async def flatten_all(adapter: BinanceFuturesAdapter, client_order_prefix: str) -> list[dict]:
    """Kill switch on the exchange: cancel every open perp order, then close every open position reduce-only."""
    for order in await adapter.get_open_orders():
        await adapter.cancel_order(order["symbol"], order["clientOrderId"])
    closed = []
    for row in await adapter.get_position_risk():
        state = _state(row)
        if state.position_amt == 0:
            continue
        side = "SELL" if state.position_amt > 0 else "BUY"
        resp = await adapter.submit_order(state.symbol, side, "MARKET", abs(state.position_amt), None,
                                          f"{client_order_prefix}-{state.symbol}", reduce_only=True)
        closed.append({"symbol": state.symbol, "side": side, "quantity": str(abs(state.position_amt)), "order": resp})
    return closed
