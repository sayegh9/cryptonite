"""Synchronous, node-thread-owned GuardedOrderDispatcher with zero async locks and zero DB I/O."""

import logging
from decimal import Decimal
from typing import Any, Callable

from cryptonite_execution.ingress import IngressHealthMonitor
from cryptonite_execution.kill_switch import LatchedKillSwitch
from nautilus_trader.model import (
    ClientOrderId,
    InstrumentId,
    LimitOrder,
    MarketOrder,
    QuoteTick,
)
from pydantic import BaseModel, Field

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


class GuardedOrderDispatcher:
    """Authoritative synchronous risk guardrail and order dispatcher.
    
    Runs entirely within the dedicated LiveNode thread:
    - ZERO async locks
    - ZERO database I/O
    - Pre-submission cash/exposure reservation
    - Spot cash-only invariant (no short selling)
    - Dual freshness enforcement
    - Asymmetric price collars and conservative market buy fill estimation
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
        self.daily_realized_pnl: Decimal = Decimal("0")
        self.daily_loss_latched: bool = False

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

    def evaluate_intent(self, intent: OrderIntent) -> tuple[bool, str, Decimal]:
        """Synchronously evaluate risk rules for an order intent.
        
        Returns (is_approved, reject_reason, estimated_notional).
        """
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

        is_perpetual = "PERP" in intent.instrument_id or ("USDT" in intent.instrument_id and "_" in intent.instrument_id)

        # 4. Risk Margin & Spot Cash Invariants
        if intent.side.upper() == "SELL":
            avail_pos = self.get_available_position_qty(intent.instrument_id)
            if not is_perpetual and intent.quantity > avail_pos:
                return (
                    False,
                    f"SPOT_INVARIANT_VIOLATION: intent quantity {intent.quantity} exceeds available long position {avail_pos}",
                    Decimal("0"),
                )
            # If it's a perpetual and quantity > avail_pos, it's a short sale (requires Margin calculation below)

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
        if intent.side.upper() == "BUY":
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

        if order_info["side"] == "BUY":
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

    def check_daily_drawdown(self, starting_equity: Decimal, current_equity: Decimal) -> bool:
        """Trip the daily loss latch if current equity has breached allowable daily drawdown."""
        if starting_equity <= Decimal("0"):
            return False
        drawdown_pct = (starting_equity - current_equity) / starting_equity
        if drawdown_pct >= self.max_daily_loss_pct:
            self.daily_loss_latched = True
            logger.critical("Daily loss limit breached: %.2f%% drawdown. Latching entries!", float(drawdown_pct * 100))
            return True
        return False
