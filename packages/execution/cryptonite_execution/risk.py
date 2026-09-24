"""Synchronous, node-thread-owned GuardedOrderDispatcher with zero async locks and zero DB I/O."""

import logging
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

from cryptonite_execution.derivatives import MarginMode
from cryptonite_execution.ingress import IngressHealthMonitor
from cryptonite_execution.kill_switch import LatchedKillSwitch, send_alert

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


class GuardedOrderDispatcher:
    """Authoritative synchronous risk guardrail and order dispatcher.
    
    Runs entirely within the dedicated LiveNode thread:
    - ZERO async locks
    - ZERO database I/O
    - Pre-submission cash/exposure reservation
    - Spot cash-only invariant (no short selling)
    - Dual freshness enforcement
    - Asymmetric price collars and conservative market buy fill estimation
    - Perpetuals (behind ``perp_trading_enabled``): leverage/margin-mode validation, reduce-only
      guard, 1% minimum order notional, 25% mark-notional concentration, initial-margin reservation
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
        perp_trading_enabled: bool = False,
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

        # Perpetuals. Off until perp fill accounting lands: handle_fill still books fills as spot.
        self.min_order_equity_pct = min_order_equity_pct
        self.max_perp_leverage = max_perp_leverage
        self.perp_trading_enabled = perp_trading_enabled
        self.perp_positions: dict[str, PerpPositionState] = {}
        self.mark_prices: dict[str, Decimal] = {}

    def set_node_cache(self, cache: Any) -> None:
        self.node_cache = cache

    @property
    def available_cash(self) -> Decimal:
        return max(Decimal("0"), self.cash_balance - self.reserved_cash)

    def get_position_qty(self, instrument_id: str) -> Decimal:
        return self.positions.get(instrument_id, Decimal("0"))

    def get_available_position_qty(self, instrument_id: str) -> Decimal:
        pos = self.positions.get(instrument_id, Decimal("0"))
        res = self.reserved_positions.get(instrument_id, Decimal("0"))
        return max(Decimal("0"), pos - res)

    def update_mark_price(self, instrument_id: str, mark_price: Decimal) -> None:
        self.mark_prices[instrument_id] = mark_price

    def set_perp_position(self, instrument_id: str, signed_qty: Decimal, entry_price: Decimal) -> None:
        if signed_qty == 0:
            self.perp_positions.pop(instrument_id, None)
        else:
            self.perp_positions[instrument_id] = PerpPositionState(signed_qty, entry_price)

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

        price_estimate = mark
        if intent.order_type.upper() == "LIMIT" and intent.price is not None:
            if side == "BUY" and intent.price > ask_price * (Decimal("1") + self.collar_pct):
                return False, f"PRICE_COLLAR_REJECT: buy limit {intent.price} exceeds allowable ask collar", Decimal("0")
            if side == "SELL" and intent.price < bid_price * (Decimal("1") - self.collar_pct):
                return False, f"PRICE_COLLAR_REJECT: sell limit {intent.price} is below allowable bid collar", Decimal("0")
            price_estimate = intent.price

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

        # 1. Kill switch latch check
        if self.kill_switch.is_latched and not intent.is_kill_switch_exit:
            return False, "KILL_SWITCH_ENGAGED", Decimal("0")

        # 2. Daily loss latch check
        if self.daily_loss_latched and not intent.is_kill_switch_exit:
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
    ) -> None:
        """Process an execution fill in memory, reconciling cash and position balances."""
        known = self.open_orders.get(client_order_id)
        if parse_instrument_id(instrument_id or (known["instrument_id"] if known else ""))[2]:
            # Spot accounting would book a short's proceeds as cash with no position
            raise NotImplementedError("perpetual fill accounting is not implemented yet (roadmap C9)")
        order_info = self.open_orders.pop(client_order_id, None)
        actual_side = (side or (order_info["side"] if order_info else "BUY")).upper()
        inst_id = instrument_id or (order_info["instrument_id"] if order_info else "UNKNOWN")

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
            self.daily_loss_latched = True
            logger.critical("Daily loss limit breached: %.2f%% drawdown from HWM. Latching entries!", float(drawdown_pct * 100))
            return True
        return False
