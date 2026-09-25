"""Synchronous, node-thread-owned GuardedOrderDispatcher with zero async locks and zero DB I/O."""

import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from cryptonite_schemas.identifiers import parse_instrument_id
from nautilus_trader.model import (
    ClientOrderId,
    InstrumentId,
    LimitOrder,
    MarketOrder,
    QuoteTick,
)
from pydantic import BaseModel, Field

from cryptonite_execution.derivatives import (
    FundingPaymentEvent,
    LiquidationEvent,
    MarginMode,
    OrderSide,
    PositionSide,
)
from cryptonite_execution.ingress import IngressHealthMonitor
from cryptonite_execution.kill_switch import LatchedKillSwitch, send_alert
from cryptonite_execution.margin import (
    calculate_funding_payment,
    calculate_liquidation_price,
    calculate_maintenance_margin,
)

logger = logging.getLogger("cryptonite.execution.risk")


class OrderIntent(BaseModel):
    """Normalized order intent emitted by strategies."""
    client_order_id: str
    instance_id: str
    instrument_id: str
    side: str  # "BUY" or "SELL"
    order_type: str  # "MARKET" or "LIMIT"
    quantity: Decimal
    price: Decimal | None = None
    is_kill_switch_exit: bool = False
    conviction_level: str | None = None
    # Perpetual-only flags; spot intents must leave them at their defaults
    reduce_only: bool = False
    leverage: int | None = None  # perps: 1..max_perp_leverage, defaults to 1
    margin_mode: str | None = None  # perps: ISOLATED or CROSS, defaults to CROSS


@dataclass
class PerpPositionState:
    signed_qty: Decimal  # >0 long, <0 short
    entry_price: Decimal
    leverage: int = 1
    margin_mode: str = MarginMode.CROSS
    initial_margin: Decimal = Decimal("0")  # locked from available cash while the position is open
    instance_id: str = ""  # strategy instance that owns it (paper_positions key); "" = unowned (seeded)
    cumulative_funding: Decimal = Decimal("0")  # Σ funding cash flows while open


class GuardedOrderDispatcher:
    """Authoritative synchronous risk guardrail and order dispatcher.
    
    Runs entirely within the dedicated LiveNode thread:
    - ZERO async locks
    - ZERO database I/O
    - Pre-submission cash/exposure reservation
    - Spot cash-only invariant (no short selling)
    - Dual freshness enforcement
    - Asymmetric price collars and conservative market buy fill estimation
    - Perpetuals (``perp_trading_enabled`` kill flag, on by default): leverage/margin-mode validation,
      reduce-only guard, 1% minimum order notional, 25% mark-notional concentration, initial-margin
      reservation, and signed-quantity fill accounting with flips
    """

    def __init__(
        self,
        account_id: str,
        starting_cash: Decimal,
        kill_switch: LatchedKillSwitch,
        ingress_health: IngressHealthMonitor,
        node_cache: Any = None,
        taker_fee: Decimal = Decimal("0.001"),
        collar_pct: Decimal = Decimal("0.02"),
        slippage_buffer_pct: Decimal = Decimal("0.005"),
        max_daily_loss_pct: Decimal = Decimal("0.05"),
        max_position_concentration_pct: Decimal = Decimal("0.25"),
        min_order_equity_pct: Decimal = Decimal("0.01"),
        max_perp_leverage: int = 20,
        perp_trading_enabled: bool = True,
        liquidation_clearance_pct: Decimal = Decimal("0.005"),
        max_mark_age_s: float = 10.0,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.leverage_brackets: dict[str, list[dict]] = {}
        self.account_id = account_id
        self.cash_balance: Decimal = starting_cash
        self.reserved_cash: Decimal = Decimal("0")
        self.positions: dict[str, Decimal] = {}  # instrument_id -> base quantity
        self.reserved_positions: dict[str, Decimal] = {}  # instrument_id -> reserved sell qty
        self.open_orders: dict[str, dict[str, Any]] = {}
        
        self.kill_switch = kill_switch
        self.ingress_health = ingress_health
        self.node_cache = node_cache
        
        self.taker_fee = taker_fee
        self.collar_pct = collar_pct
        self.slippage_buffer_pct = slippage_buffer_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_position_concentration_pct = max_position_concentration_pct
        self.daily_realized_pnl: Decimal = Decimal("0")
        self.daily_loss_latched: bool = False

        # Perpetuals: cash_balance is the USDT wallet. Perp fills never move notional through cash;
        # they lock initial margin and settle realized PnL and fees into cash.
        self.min_order_equity_pct = min_order_equity_pct
        self.max_perp_leverage = max_perp_leverage
        self.perp_trading_enabled = perp_trading_enabled
        self.locked_margin: Decimal = Decimal("0")  # Σ initial margin of open perp positions
        self.liquidation_clearance_pct = liquidation_clearance_pct
        self.insurance_fund_deficit: Decimal = Decimal("0")  # cumulative cost of liquidation shortfalls
        self.last_funding_time_ns: dict[str, int] = {}  # per instrument; settlements are applied once, in order
        self.perp_positions: dict[str, PerpPositionState] = {}
        self.mark_prices: dict[str, Decimal] = {}
        self.mark_ts_ns: dict[str, int] = {}  # exchange event time of each mark
        self.max_mark_age_ns = int(max_mark_age_s * 1_000_000_000)
        self._clock_ns = clock_ns

    def set_node_cache(self, cache: Any) -> None:
        self.node_cache = cache

    @property
    def available_cash(self) -> Decimal:
        return max(Decimal("0"), self.cash_balance - self.reserved_cash - self.locked_margin)

    def open_position_count(self) -> int:
        """Spot longs plus perpetual positions in either direction (kill-switch reset gate)."""
        spot = sum(1 for q in self.positions.values() if q > 0)
        return spot + sum(1 for p in self.perp_positions.values() if p.signed_qty != 0)

    @staticmethod
    def is_entry_order(order_info: dict[str, Any]) -> bool:
        """Entry = adds exposure: spot BUYs, and perp orders of either side unless reduce-only."""
        if order_info.get("is_perp"):
            return not order_info.get("reduce_only")
        return order_info["side"] == "BUY"

    def get_position_qty(self, instrument_id: str) -> Decimal:
        return self.positions.get(instrument_id, Decimal("0"))

    def fillable_quantity(self, order_info: dict[str, Any]) -> Decimal:
        """How much of a working order may fill right now.

        Exit orders are capped at the position they reduce, which can shrink while they rest
        (CLOSE_POSITION, another exit, a liquidation): a reduce-only perp order at the opposite
        position, a spot SELL at the held quantity. 0 means the order must be cancelled.
        """
        qty = order_info["quantity"]
        inst = order_info["instrument_id"]
        if order_info.get("is_perp"):
            if not order_info.get("reduce_only"):
                return qty
            signed = self.get_perp_position_qty(inst)
            closable = -signed if order_info["side"] == "BUY" else signed
            return max(Decimal("0"), min(qty, closable))
        if order_info["side"] == "SELL":
            return max(Decimal("0"), min(qty, self.get_position_qty(inst)))
        return qty

    def get_available_position_qty(self, instrument_id: str) -> Decimal:
        pos = self.positions.get(instrument_id, Decimal("0"))
        res = self.reserved_positions.get(instrument_id, Decimal("0"))
        return max(Decimal("0"), pos - res)

    def update_mark_price(self, instrument_id: str, mark_price: Decimal, ts_event_ns: int | None = None) -> None:
        """Record a mark price and its exchange event time (defaults to now)."""
        self.mark_prices[instrument_id] = mark_price
        self.mark_ts_ns[instrument_id] = ts_event_ns if ts_event_ns is not None else self._clock_ns()

    def is_mark_stale(self, instrument_id: str) -> bool:
        """True when the mark is missing, has no timestamp, or is older than ``max_mark_age_s``."""
        ts = self.mark_ts_ns.get(instrument_id)
        return ts is None or self._clock_ns() - ts > self.max_mark_age_ns

    def fresh_mark_prices(self) -> dict[str, Decimal]:
        return {inst: px for inst, px in self.mark_prices.items() if not self.is_mark_stale(inst)}

    def set_perp_position(
        self,
        instrument_id: str,
        signed_qty: Decimal,
        entry_price: Decimal,
        leverage: int = 1,
        margin_mode: str = MarginMode.CROSS,
        instance_id: str = "",
    ) -> None:
        """Seed or overwrite a perp position (reconciliation/tests); locks its initial margin."""
        old = self.perp_positions.pop(instrument_id, None)
        if old is not None:
            self.locked_margin -= old.initial_margin
        if signed_qty != 0:
            im = abs(signed_qty) * entry_price / Decimal(leverage)
            self.perp_positions[instrument_id] = PerpPositionState(
                signed_qty, entry_price, leverage, margin_mode, im, instance_id
            )
            self.locked_margin += im

    def flatten_perp_position(
        self, instrument_id: str, fill_price: Decimal, commission: Decimal, client_order_id: str
    ) -> tuple[str, Decimal, Decimal] | None:
        """Close a perp position with an opposing reduce-only order filled at ``fill_price``.

        Returns (side, quantity, gross realized PnL) of the closing order, or None when flat.
        """
        signed_qty = self.get_perp_position_qty(instrument_id)
        if signed_qty == 0:
            return None
        side = "BUY" if signed_qty < 0 else "SELL"
        qty = abs(signed_qty)
        self.open_orders[client_order_id] = {
            "intent": None,
            "reserved_notional": Decimal("0"),
            "side": side,
            "quantity": qty,
            "instrument_id": instrument_id,
            "is_perp": True,
            "reduce_only": True,
        }
        realized = self.handle_fill(client_order_id, qty, fill_price, commission, side=side, instrument_id=instrument_id)
        return side, qty, realized or Decimal("0")

    def fill_event_fields(self, instrument_id: str, realized_pnl: Decimal | None, fill_price: Decimal) -> dict[str, Any]:
        """Extra ORDER_FILLED fields for EventBridgeWorker: perp realized PnL and post-fill position."""
        if not parse_instrument_id(instrument_id)[2]:
            return {}
        return {
            "realized_pnl": str(realized_pnl) if realized_pnl is not None else None,
            "position": self.perp_position_snapshot(instrument_id, fill_price),
        }

    def perp_position_snapshot(self, instrument_id: str, reference_price: Decimal | None = None) -> dict[str, Any]:
        """Post-fill position state for persistence (strings for Decimal fields).

        Liquidation price and maintenance margin are valued at mark, else ``reference_price``, else
        entry. CROSS uses the cross wallet (cash minus isolated margins) and the other CROSS
        positions' MM and uPnL. Both are None when the symbol has no bracket table.
        """
        pos = self.perp_positions.get(instrument_id)
        if pos is None:
            return {"signed_qty": "0"}
        maintenance, liquidation = self._perp_risk_levels(instrument_id, reference_price)
        isolated = pos.margin_mode == MarginMode.ISOLATED
        return {
            "signed_qty": str(pos.signed_qty),
            "entry_price": str(pos.entry_price),
            "leverage": pos.leverage,
            "margin_mode": str(pos.margin_mode),
            "isolated_margin": str(pos.initial_margin if isolated else Decimal("0")),
            "liquidation_price": str(liquidation) if liquidation is not None else None,
            "maintenance_margin": str(maintenance) if maintenance is not None else None,
        }

    def _perp_risk_levels(
        self, instrument_id: str, reference_price: Decimal | None = None
    ) -> tuple[Decimal | None, Decimal | None]:
        """(maintenance margin, liquidation price) for an open perp position; (None, None) without brackets."""
        pos = self.perp_positions[instrument_id]

        def price_for(inst: str, p: PerpPositionState) -> Decimal:
            return self.mark_prices.get(inst) or (reference_price if inst == instrument_id else None) or p.entry_price

        qty = abs(pos.signed_qty)
        side = OrderSide.BUY if pos.signed_qty > 0 else OrderSide.SELL
        isolated = pos.margin_mode == MarginMode.ISOLATED
        try:
            maintenance = calculate_maintenance_margin(instrument_id, qty * price_for(instrument_id, pos))
            if isolated:
                liquidation = calculate_liquidation_price(
                    instrument_id, side, qty, pos.entry_price, pos.initial_margin, MarginMode.ISOLATED
                )
            else:
                wallet = self.cash_balance - sum(
                    (p.initial_margin for p in self.perp_positions.values() if p.margin_mode == MarginMode.ISOLATED),
                    Decimal("0"),
                )
                others_mm, others_upnl = Decimal("0"), Decimal("0")
                for inst, other in self.perp_positions.items():
                    if inst == instrument_id or other.margin_mode == MarginMode.ISOLATED:
                        continue
                    px = price_for(inst, other)
                    others_mm += calculate_maintenance_margin(inst, abs(other.signed_qty) * px)
                    others_upnl += other.signed_qty * (px - other.entry_price)
                liquidation = calculate_liquidation_price(
                    instrument_id, side, qty, pos.entry_price, wallet, MarginMode.CROSS, others_mm, others_upnl
                )
        except ValueError:
            return None, None
        return maintenance, liquidation

    def apply_funding_settlement(
        self, instrument_id: str, mark_price: Decimal, funding_rate: Decimal, funding_time_ns: int
    ) -> list[FundingPaymentEvent]:
        """Apply one funding settlement to the open position on ``instrument_id``.

        Cash moves by ``-size * mark * rate``. For ISOLATED positions the payment also moves the
        isolated margin (Binance semantics), so a funding debit pulls the liquidation price closer;
        callers should run a liquidation sweep afterwards. Idempotent and monotonic per
        instrument: a settlement at or before the last applied funding time is ignored.
        """
        if funding_time_ns <= self.last_funding_time_ns.get(instrument_id, -1):
            return []
        self.last_funding_time_ns[instrument_id] = funding_time_ns
        pos = self.perp_positions.get(instrument_id)
        if pos is None or pos.signed_qty == 0:
            return []

        cash_flow = calculate_funding_payment(pos.signed_qty, mark_price, funding_rate)
        self.cash_balance += cash_flow
        self.daily_realized_pnl += cash_flow
        if pos.margin_mode == MarginMode.ISOLATED:
            pos.initial_margin += cash_flow
            self.locked_margin += cash_flow
        pos.cumulative_funding += cash_flow
        return [FundingPaymentEvent(
            account_id=self.account_id,
            instance_id=pos.instance_id or "MANUAL_TRADE",
            instrument_id=instrument_id,
            margin_mode=pos.margin_mode,
            signed_qty=pos.signed_qty,
            mark_price=mark_price,
            funding_rate=funding_rate,
            cash_flow=cash_flow,
            cumulative_funding=pos.cumulative_funding,
            funding_time_ns=funding_time_ns,
        )]

    def check_liquidations(self, mark_prices: dict[str, Decimal]) -> list[LiquidationEvent]:
        """Force-close perp positions whose mark breached the liquidation price.

        Long: mark <= P_liq. Short: mark >= P_liq. Positions are checked largest maintenance
        margin first, and each check sees the account after earlier liquidations (cross positions
        share the wallet). A breached position has its resting orders on the symbol cancelled
        and is closed by an opposing reduce-only order for its full size. Fees: taker fee plus
        the clearance fee on the fill notional. Losses stop at the isolated margin (ISOLATED) or
        at zero cash (CROSS); the shortfall is credited back and booked to
        ``insurance_fund_deficit``. Runs regardless of the kill switch: it only reduces exposure.
        """
        self.mark_prices.update(mark_prices)
        events: list[LiquidationEvent] = []

        def mm_at_mark(inst: str) -> Decimal:
            return self._perp_risk_levels(inst)[0] or Decimal("0")

        for inst in sorted(self.perp_positions, key=mm_at_mark, reverse=True):
            pos = self.perp_positions.get(inst)
            mark = self.mark_prices.get(inst)
            if pos is None or mark is None:
                continue
            _, liq_price = self._perp_risk_levels(inst)
            if liq_price is None:
                logger.warning("Cannot evaluate liquidation for %s: no bracket table", inst)
                continue
            is_long = pos.signed_qty > 0
            if not (mark <= liq_price if is_long else mark >= liq_price):
                continue
            events.append(self._liquidate(inst, pos, mark, liq_price))
        return events

    def _liquidate(
        self, instrument_id: str, pos: PerpPositionState, mark: Decimal, liq_price: Decimal
    ) -> LiquidationEvent:
        qty = abs(pos.signed_qty)
        is_long = pos.signed_qty > 0
        isolated = pos.margin_mode == MarginMode.ISOLATED
        snapshot = PerpPositionState(**vars(pos))

        bankruptcy = None
        fill_price = mark
        if isolated:
            bankruptcy = pos.entry_price - pos.initial_margin / qty if is_long else pos.entry_price + pos.initial_margin / qty
            if (is_long and mark < bankruptcy) or (not is_long and mark > bankruptcy):
                fill_price = bankruptcy  # gapped through: trader is closed at bankruptcy, the fund eats the gap
        gap_cost = abs(mark - fill_price) * qty

        for cid in [c for c, o in self.open_orders.items() if o["instrument_id"] == instrument_id]:
            self.release_reservation(cid)

        notional = qty * fill_price
        trading_fee = notional * self.taker_fee
        clearance_fee = notional * self.liquidation_clearance_pct
        client_order_id = f"LIQ-{uuid.uuid4().hex[:12].upper()}"
        cash_before = self.cash_balance
        realized = self.flatten_perp_position(
            instrument_id, fill_price, trading_fee + clearance_fee, client_order_id
        )[2]

        shortfall = Decimal("0")
        if isolated and self.cash_balance - cash_before < -snapshot.initial_margin:
            shortfall = -snapshot.initial_margin - (self.cash_balance - cash_before)
        elif not isolated and self.cash_balance < 0:
            shortfall = -self.cash_balance
        self.cash_balance += shortfall
        self.daily_realized_pnl += shortfall
        self.insurance_fund_deficit += shortfall + gap_cost

        event = LiquidationEvent(
            client_order_id=client_order_id,
            account_id=self.account_id,
            instance_id=snapshot.instance_id or "MANUAL_TRADE",
            instrument_id=instrument_id,
            margin_mode=snapshot.margin_mode,
            side=PositionSide.LONG if is_long else PositionSide.SHORT,
            quantity=qty,
            entry_price=snapshot.entry_price,
            mark_price=mark,
            liquidation_price=liq_price,
            bankruptcy_price=bankruptcy,
            fill_price=fill_price,
            realized_pnl=realized,
            trading_fee=trading_fee,
            clearance_fee=clearance_fee,
            shortfall_covered=shortfall,
            insurance_fund_deficit=shortfall + gap_cost,
        )
        logger.critical(
            "LIQUIDATED %s %s %s @ %s (mark %s, liq %s): realized %s, fees %s, fund deficit %s",
            instrument_id, event.side, qty, fill_price, mark, liq_price, realized,
            trading_fee + clearance_fee, event.insurance_fund_deficit,
        )
        return event

    def get_perp_position_qty(self, instrument_id: str) -> Decimal:
        pos = self.perp_positions.get(instrument_id)
        return pos.signed_qty if pos else Decimal("0")

    def _resting_reduce_only_qty(self, instrument_id: str) -> Decimal:
        return sum(
            (o["quantity"] for o in self.open_orders.values()
             if o["instrument_id"] == instrument_id and o.get("reduce_only")),
            Decimal("0"),
        )

    def _portfolio_equity(self) -> Decimal:
        """Cash + spot longs at bid + perpetual unrealized PnL at mark."""
        total_equity = self.cash_balance
        for p_inst_id, qty in self.positions.items():
            if qty > 0:
                try:
                    p_quote = self.node_cache.quote(InstrumentId.from_str(p_inst_id)) if self.node_cache else None
                    if p_quote:
                        total_equity += qty * Decimal(str(p_quote.bid_price))
                except Exception:
                    pass
        for p_inst_id, pos in self.perp_positions.items():
            mark = self.mark_prices.get(p_inst_id)
            if mark is not None:
                total_equity += pos.signed_qty * (mark - pos.entry_price)
        return total_equity

    def _derivative_flag_reject(self, intent: OrderIntent, is_perp: bool) -> str | None:
        """Validate (and default) leverage / margin mode / reduce-only against the instrument type."""
        if not is_perp:
            if intent.leverage not in (None, 1):
                return f"SPOT_LEVERAGE_NOT_ALLOWED: leverage {intent.leverage} on spot instrument"
            if intent.margin_mode is not None or intent.reduce_only:
                return "SPOT_DERIVATIVE_FLAG_NOT_ALLOWED: margin_mode/reduce_only are perpetual-only"
            return None

        leverage = 1 if intent.leverage is None else intent.leverage
        if not 1 <= leverage <= self.max_perp_leverage:
            return f"LEVERAGE_OUT_OF_RANGE: {leverage} not in 1..{self.max_perp_leverage}"
        mode = (intent.margin_mode or MarginMode.CROSS).upper()
        if mode not in MarginMode.__members__:
            return f"INVALID_MARGIN_MODE: {intent.margin_mode}"
        intent.leverage, intent.margin_mode = leverage, mode
        return None

    def _evaluate_perp(self, intent: OrderIntent, bid_price: Decimal, ask_price: Decimal) -> tuple[bool, str, Decimal]:
        """Perpetual rules. Returns (approved, reason, margin_to_reserve)."""
        side = intent.side.upper()
        if side not in ("BUY", "SELL"):
            return False, f"INVALID_SIDE: {intent.side}", Decimal("0")

        mark = self.mark_prices.get(intent.instrument_id)
        if mark is None or mark <= 0:
            return False, "NO_MARK_PRICE", Decimal("0")
        if not intent.reduce_only and self.is_mark_stale(intent.instrument_id):
            age_s = (self._clock_ns() - self.mark_ts_ns.get(intent.instrument_id, 0)) / 1e9
            return False, f"STALE_MARK_PRICE: mark is {age_s:.1f}s old (max {self.max_mark_age_ns / 1e9:.0f}s)", Decimal("0")

        price_estimate = mark
        if intent.order_type.upper() == "LIMIT" and intent.price is not None:
            if side == "BUY" and intent.price > ask_price * (Decimal("1") + self.collar_pct):
                return False, f"PRICE_COLLAR_REJECT: buy limit {intent.price} exceeds allowable ask collar", Decimal("0")
            if side == "SELL" and intent.price < bid_price * (Decimal("1") - self.collar_pct):
                return False, f"PRICE_COLLAR_REJECT: sell limit {intent.price} is below allowable bid collar", Decimal("0")
            price_estimate = intent.price

        # One owner per perp position: the DB keeps one row per (instance, instrument), so a second
        # instance trading the same symbol would write the netted position onto the wrong row
        owner = self.perp_positions[intent.instrument_id].instance_id if intent.instrument_id in self.perp_positions else ""
        if owner and owner != intent.instance_id:
            return False, f"PERP_POSITION_OWNED_BY_OTHER_INSTANCE: {intent.instrument_id} is held by {owner}", Decimal("0")

        pos_qty = self.get_perp_position_qty(intent.instrument_id)
        order_is_long = side == "BUY"

        # Reduce-only: may only shrink an existing position; no floor, concentration, or margin checks
        if intent.reduce_only:
            if pos_qty == 0:
                return False, "REDUCE_ONLY_NO_POSITION", Decimal("0")
            if (pos_qty > 0) == order_is_long:
                return False, "REDUCE_ONLY_INVALID_DIRECTION", Decimal("0")
            closable = abs(pos_qty) - self._resting_reduce_only_qty(intent.instrument_id)
            if intent.quantity > closable:
                return (
                    False,
                    f"REDUCE_ONLY_OVERSIZE: quantity {intent.quantity} > closable {closable} "
                    f"(position {abs(pos_qty)} minus resting reduce-only orders)",
                    Decimal("0"),
                )
            return True, "APPROVED", Decimal("0")

        equity = self._portfolio_equity()
        if equity <= 0:
            return False, "NON_POSITIVE_EQUITY", Decimal("0")

        order_notional = intent.quantity * price_estimate
        min_notional = self.min_order_equity_pct * equity
        if order_notional < min_notional:
            return (
                False,
                f"ORDER_BELOW_MIN_NOTIONAL: {order_notional:.4f} < {self.min_order_equity_pct:.0%} of equity ({min_notional:.4f})",
                Decimal("0"),
            )

        signed_order = intent.quantity if order_is_long else -intent.quantity
        new_notional = abs(pos_qty + signed_order) * mark
        if new_notional > self.max_position_concentration_pct * equity:
            return (
                False,
                f"CONCENTRATION_LIMIT_BREACHED: position notional {new_notional / equity:.1%} of equity "
                f"> limit {self.max_position_concentration_pct:.1%}",
                Decimal("0"),
            )

        # Initial margin only on the exposure this order opens; closing part of an opposite position frees margin
        if pos_qty == 0 or (pos_qty > 0) == order_is_long:
            opening_qty = intent.quantity
        else:
            opening_qty = max(Decimal("0"), intent.quantity - abs(pos_qty))
        required_margin = (
            opening_qty * price_estimate / Decimal(intent.leverage) + order_notional * self.taker_fee
        )
        if required_margin > self.available_cash:
            return (
                False,
                f"INSUFFICIENT_MARGIN: required {required_margin:.4f} > available {self.available_cash:.4f}",
                required_margin,
            )
        return True, "APPROVED", required_margin

    def evaluate_intent(self, intent: OrderIntent) -> tuple[bool, str, Decimal]:
        """Synchronously evaluate risk rules for an order intent.
        
        Returns (is_approved, reject_reason, estimated_notional).
        For perpetuals the third element is the initial margin to reserve (0 for reduce-only).
        """
        is_perp = parse_instrument_id(intent.instrument_id)[2]
        if (flag_reject := self._derivative_flag_reject(intent, is_perp)) is not None:
            return False, flag_reject, Decimal("0")
        if is_perp and not self.perp_trading_enabled:
            return False, "PERP_TRADING_DISABLED", Decimal("0")

        # AI Agent Conviction Sizing (never resizes a reduce-only close)
        if intent.conviction_level and not intent.reduce_only:
            conviction = intent.conviction_level.upper()
            multiplier = Decimal("1.0")
            if conviction == "OVERWEIGHT":
                multiplier = Decimal("1.5")
            elif conviction == "UNDERWEIGHT":
                multiplier = Decimal("0.5")
            elif conviction == "HOLD":
                return False, "AGENT_CONVICTION_HOLD_REJECT", Decimal("0")
            
            # Apply dynamic sizing
            intent.quantity = intent.quantity * multiplier

        # Exits may pass the latches: kill-switch exits, and perp reduce-only orders (the reduce-only
        # guard below guarantees they can only shrink an existing position, never add or flip)
        exit_only = intent.is_kill_switch_exit or (is_perp and intent.reduce_only)

        # 1. Kill switch latch check
        if self.kill_switch.is_latched and not exit_only:
            return False, "KILL_SWITCH_ENGAGED", Decimal("0")

        # 2. Daily loss latch check
        if self.daily_loss_latched and not exit_only:
            return False, "DAILY_LOSS_LIMIT_BREACHED", Decimal("0")

        # 3. Freshness check
        if not self.ingress_health.is_fresh(intent.instrument_id, self.node_cache):
            return False, "DATA_STALE_OR_DISCONTINUOUS", Decimal("0")

        # Retrieve quote from cache
        inst = InstrumentId.from_str(intent.instrument_id)
        quote: QuoteTick | None = self.node_cache.quote(inst) if self.node_cache else None
        if quote is None:
            return False, "NO_LIVE_QUOTE_IN_CACHE", Decimal("0")

        ask_price = Decimal(str(quote.ask_price))
        bid_price = Decimal(str(quote.bid_price))

        if is_perp:
            return self._evaluate_perp(intent, bid_price, ask_price)

        # 4. Risk Margin & Spot Cash Invariants
        if intent.side.upper() == "SELL":
            avail_pos = self.get_available_position_qty(intent.instrument_id)
            if intent.quantity > avail_pos:
                return (
                    False,
                    f"SPOT_INVARIANT_VIOLATION: intent quantity {intent.quantity} exceeds available long position {avail_pos}",
                    Decimal("0"),
                )

            # Price collar check on sell
            if intent.order_type.upper() == "LIMIT" and intent.price is not None:
                min_acceptable_bid = bid_price * (Decimal("1") - self.collar_pct)
                if intent.price < min_acceptable_bid:
                    return (
                        False,
                        f"PRICE_COLLAR_REJECT: sell limit {intent.price} is below allowable bid collar {min_acceptable_bid}",
                        Decimal("0"),
                    )

            est_notional = intent.quantity * (intent.price or bid_price)
            return True, "APPROVED", est_notional

        # 5. Buy Order evaluation & cash reservation
        if intent.side.upper() == "BUY":
            # Determine conservative execution price
            if intent.order_type.upper() == "LIMIT" and intent.price is not None:
                max_acceptable_ask = ask_price * (Decimal("1") + self.collar_pct)
                if intent.price > max_acceptable_ask:
                    return (
                        False,
                        f"PRICE_COLLAR_REJECT: buy limit {intent.price} exceeds allowable ask collar {max_acceptable_ask}",
                        Decimal("0"),
                    )
                effective_price = intent.price
            else:
                # Market buy: conservative estimation with price collar + slippage buffer
                effective_price = ask_price * (Decimal("1") + self.collar_pct + self.slippage_buffer_pct)

            required_cash = intent.quantity * effective_price * (Decimal("1") + self.taker_fee)
            if required_cash > self.available_cash:
                return (
                    False,
                    f"INSUFFICIENT_CASH: required {required_cash:.4f} > available {self.available_cash:.4f}",
                    required_cash,
                )

            # Calculate total portfolio equity to check concentration
            total_equity = self._portfolio_equity()
            
            # Current value of existing position in this instrument
            cur_pos_qty = self.get_position_qty(intent.instrument_id)
            cur_pos_value = cur_pos_qty * bid_price
            
            # Post-trade value of this instrument
            new_pos_value = cur_pos_value + (intent.quantity * effective_price)
            
            if total_equity > 0:
                concentration_pct = new_pos_value / total_equity
                if concentration_pct > self.max_position_concentration_pct:
                    return (
                        False,
                        f"CONCENTRATION_LIMIT_BREACHED: new position {concentration_pct:.1%} > limit {self.max_position_concentration_pct:.1%}",
                        required_cash,
                    )

            return True, "APPROVED", required_cash

        return False, f"INVALID_SIDE: {intent.side}", Decimal("0")

    def dispatch(
        self,
        intent: OrderIntent,
        submit_fn: Callable[[Any], None],
        nautilus_order: Any,
    ) -> tuple[bool, str]:
        """Evaluate and atomically reserve cash/position in memory, then submit to Nautilus."""
        approved, reason, est_notional = self.evaluate_intent(intent)
        if not approved:
            logger.warning("Order intent %s rejected: %s", intent.client_order_id, reason)
            return False, reason

        # Atomically reserve before submission
        is_perp = parse_instrument_id(intent.instrument_id)[2]
        if is_perp:
            self.reserved_cash += est_notional  # initial margin + fee; 0 for reduce-only
        elif intent.side.upper() == "BUY":
            self.reserved_cash += est_notional
        elif intent.side.upper() == "SELL":
            cur_res = self.reserved_positions.get(intent.instrument_id, Decimal("0"))
            self.reserved_positions[intent.instrument_id] = cur_res + intent.quantity

        self.open_orders[intent.client_order_id] = {
            "intent": intent,
            "reserved_notional": est_notional,
            "side": intent.side.upper(),
            "quantity": intent.quantity,
            "instrument_id": intent.instrument_id,
            "instance_id": intent.instance_id,
            "is_perp": is_perp,
            "reduce_only": intent.reduce_only,
        }

        # Submit to Nautilus
        try:
            submit_fn(nautilus_order)
            return True, "SUBMITTED"
        except Exception as e:
            # Rollback reservation if submission immediately throws
            self.release_reservation(intent.client_order_id)
            logger.error("Failed submitting order to Nautilus: %s", e)
            return False, str(e)

    def release_reservation(self, client_order_id: str) -> None:
        """Release in-memory reservation when an order is rejected or canceled."""
        order_info = self.open_orders.pop(client_order_id, None)
        if not order_info:
            return

        if order_info.get("is_perp") or order_info["side"] == "BUY":
            self.reserved_cash = max(Decimal("0"), self.reserved_cash - order_info["reserved_notional"])
        elif order_info["side"] == "SELL":
            inst_id = order_info["instrument_id"]
            cur_res = self.reserved_positions.get(inst_id, Decimal("0"))
            self.reserved_positions[inst_id] = max(Decimal("0"), cur_res - order_info["quantity"])

    def handle_fill(
        self,
        client_order_id: str,
        fill_qty: Decimal,
        fill_price: Decimal,
        commission: Decimal,
        side: str | None = None,
        instrument_id: str | None = None,
    ) -> Decimal | None:
        """Process an execution fill in memory, reconciling cash and position balances.

        Returns the gross realized PnL of a perp fill's closing leg (None when it closed nothing);
        spot fills return None.
        """
        order_info = self.open_orders.pop(client_order_id, None)
        actual_side = (side or (order_info["side"] if order_info else "BUY")).upper()
        inst_id = instrument_id or (order_info["instrument_id"] if order_info else "UNKNOWN")

        if parse_instrument_id(inst_id)[2]:
            return self._handle_perp_fill(order_info, inst_id, actual_side, fill_qty, fill_price, commission)

        if actual_side == "BUY":
            cost = fill_qty * fill_price + commission
            self.cash_balance -= cost
            if order_info:
                self.reserved_cash = max(Decimal("0"), self.reserved_cash - order_info["reserved_notional"])
            cur_pos = self.positions.get(inst_id, Decimal("0"))
            self.positions[inst_id] = cur_pos + fill_qty
        elif actual_side == "SELL":
            proceeds = fill_qty * fill_price - commission
            self.cash_balance += proceeds
            if order_info:
                cur_res = self.reserved_positions.get(inst_id, Decimal("0"))
                self.reserved_positions[inst_id] = max(Decimal("0"), cur_res - order_info["quantity"])
            cur_pos = self.positions.get(inst_id, Decimal("0"))
            self.positions[inst_id] = max(Decimal("0"), cur_pos - fill_qty)
        return None

    def _handle_perp_fill(
        self,
        order_info: dict[str, Any] | None,
        instrument_id: str,
        side: str,
        fill_qty: Decimal,
        fill_price: Decimal,
        commission: Decimal,
    ) -> Decimal | None:
        """Signed-quantity perp accounting: open/add, reduce, close, or flip (close leg then open leg).

        Cash moves only by realized PnL and fees; initial margin moves from the order reservation
        into ``locked_margin`` and is released pro rata as the position shrinks.
        """
        if order_info:
            self.reserved_cash = max(Decimal("0"), self.reserved_cash - order_info["reserved_notional"])
        intent = order_info.get("intent") if order_info else None
        reduce_only = bool(order_info and order_info.get("reduce_only"))

        pos = self.perp_positions.get(instrument_id)
        leverage = (intent.leverage if intent and intent.leverage else None) or (pos.leverage if pos else 1)
        margin_mode = (intent.margin_mode if intent and intent.margin_mode else None) or (
            pos.margin_mode if pos else MarginMode.CROSS
        )
        fill_sign = Decimal("1") if side == "BUY" else Decimal("-1")
        realized = Decimal("0")
        closed_any = False
        open_qty = fill_qty

        if pos is not None and (pos.signed_qty > 0) != (fill_sign > 0):
            # Close leg: realize PnL on the overlapping quantity and release its margin pro rata
            held = abs(pos.signed_qty)
            closed = min(fill_qty, held)
            direction = Decimal("1") if pos.signed_qty > 0 else Decimal("-1")
            realized = direction * closed * (fill_price - pos.entry_price)
            closed_any = True
            released = pos.initial_margin * closed / held
            self.locked_margin -= released
            if closed == held:
                del self.perp_positions[instrument_id]
                pos = None
            else:
                pos.signed_qty += fill_sign * closed
                pos.initial_margin -= released
            open_qty = fill_qty - closed

        if reduce_only and open_qty > 0:
            # Never open exposure from a reduce-only fill, including when the position is already gone
            logger.error("Reduce-only fill %s exceeded position by %s; excess ignored", instrument_id, open_qty)
            open_qty = Decimal("0")

        if open_qty > 0:
            # Open/add leg: weighted average entry, lock initial margin at this fill
            im = open_qty * fill_price / Decimal(leverage)
            if pos is None:
                self.perp_positions[instrument_id] = PerpPositionState(
                    fill_sign * open_qty, fill_price, leverage, margin_mode, im,
                    (order_info or {}).get("instance_id") or "MANUAL_TRADE",
                )
            else:
                held = abs(pos.signed_qty)
                pos.entry_price = (held * pos.entry_price + open_qty * fill_price) / (held + open_qty)
                pos.signed_qty += fill_sign * open_qty
                pos.initial_margin += im
                pos.leverage, pos.margin_mode = leverage, margin_mode
            self.locked_margin += im

        self.cash_balance += realized - commission
        self.daily_realized_pnl += realized - commission
        return realized if closed_any else None

    def portfolio_equity(self) -> Decimal:
        """Cash + spot longs at bid + perpetual unrealized PnL at mark."""
        return self._portfolio_equity()

    def roll_trading_day(self, utc_day: Any, current_equity: Decimal) -> bool:
        """Start a new UTC trading day: re-base the drawdown high-water mark on current equity and
        clear the daily PnL and daily-loss latch. The kill switch is untouched (operator reset only).

        Returns True when a day boundary was crossed (False on the first call, which only sets the base).
        """
        if getattr(self, "trading_day", None) == utc_day:
            return False
        rolled = getattr(self, "trading_day", None) is not None
        self.trading_day = utc_day
        self.high_water_mark = current_equity
        if rolled:
            self.daily_realized_pnl = Decimal("0")
            self.daily_loss_latched = False
        return rolled

    def check_daily_drawdown(self, current_equity: Decimal, macro_atr_pct: Decimal | None = None) -> bool:
        """Trip the daily loss latch if current equity has breached allowable daily drawdown."""
        if getattr(self, "high_water_mark", None) is None:
            self.high_water_mark = current_equity
        else:
            self.high_water_mark = max(self.high_water_mark, current_equity)

        if self.high_water_mark <= Decimal("0"):
            return False
            
        drawdown_limit = macro_atr_pct if macro_atr_pct is not None else self.max_daily_loss_pct
        
        drawdown_pct = (self.high_water_mark - current_equity) / self.high_water_mark
        if drawdown_pct >= drawdown_limit:
            if not self.daily_loss_latched:
                send_alert(f"📉 *DAILY DRAWDOWN LIMIT BREACHED* 📉\nDrawdown: {float(drawdown_pct * 100):.2f}%")
                logger.critical("Daily loss limit breached: %.2f%% drawdown from HWM. Latching entries!", float(drawdown_pct * 100))
            self.daily_loss_latched = True
            return True
        return False
