"""Spot OCO order lifecycle and protection manager for Binance Demo/Live.

Handles:
1. 15s protection timer initiated upon the FIRST partial fill of either leg.
2. Unfilled counterpart leg cancellation on timer expiry.
3. Dust checks (minQty and minNotional) on remaining balance.
4. Net base post-commission attribution to strategy ledger.
5. Safe pre-flattening cancellation of active OCO pairs before market exit.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("cryptonite.execution.oco")


@dataclass
class OcoLeg:
    client_order_id: str
    venue_order_id: Optional[str] = None
    order_type: str = "LIMIT_MAKER"  # or STOP_LOSS_LIMIT
    price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    quantity: Decimal = Decimal("0")
    filled_qty: Decimal = Decimal("0")
    status: str = "NEW"


@dataclass
class OcoState:
    order_list_id: int
    list_client_order_id: str
    instrument_id: str
    limit_leg: OcoLeg
    stop_leg: OcoLeg
    protection_status: str = "ACTIVE"  # ACTIVE, TIMER_RUNNING, EXPIRED, SETTLED
    first_fill_observed_at_ns: Optional[int] = None
    protection_timer_expires_at_ns: Optional[int] = None


@dataclass
class EntryOrderState:
    client_order_id: str
    instrument_id: str
    quantity: Decimal
    take_profit_price: Decimal
    stop_price: Decimal
    stop_limit_price: Optional[Decimal] = None
    filled_qty: Decimal = Decimal("0")
    commission_in_base: Decimal = Decimal("0")
    status: str = "NEW"  # NEW, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED
    first_fill_observed_at_ns: Optional[int] = None
    protection_timer_expires_at_ns: Optional[int] = None
    protective_oco_installed: bool = False
    oco_order_list_id: Optional[int] = None


class SpotOcoManager:
    """Manages Spot OCO order lifecycle, protection timers, and safe cancellation."""

    PROTECTION_TIMEOUT_SECS: float = 15.0

    def __init__(
        self,
        cancel_order_fn: Optional[Callable[[str], Any]] = None,
        submit_oco_fn: Optional[Callable[..., Any]] = None,
        min_qty: Decimal = Decimal("0.00001"),
        min_notional: Decimal = Decimal("10.0"),
    ):
        self._cancel_order_fn = cancel_order_fn
        self._submit_oco_fn = submit_oco_fn
        self._min_qty = min_qty
        self._min_notional = min_notional
        self._active_ocos: Dict[int, OcoState] = {}
        self._client_order_to_list_id: Dict[str, int] = {}
        self._active_entries: Dict[str, EntryOrderState] = {}
        self._timers: Dict[int, asyncio.Task] = {}
        self._entry_timers: Dict[str, asyncio.Task] = {}

    def register_entry_order(
        self,
        client_order_id: str,
        instrument_id: str,
        quantity: Decimal,
        take_profit_price: Decimal,
        stop_price: Decimal,
        stop_limit_price: Optional[Decimal] = None,
    ) -> EntryOrderState:
        """Register an active entry order awaiting fills and eventual OCO protection."""
        entry = EntryOrderState(
            client_order_id=client_order_id,
            instrument_id=instrument_id,
            quantity=quantity,
            take_profit_price=take_profit_price,
            stop_price=stop_price,
            stop_limit_price=stop_limit_price or stop_price,
        )
        self._active_entries[client_order_id] = entry
        return entry

    def on_entry_fill(
        self,
        client_order_id: str,
        fill_qty: Decimal,
        price: Decimal,
        commission: Decimal,
        commission_asset: str,
        base_asset: str,
        event_time_ns: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Process fill event on an entry order.

        Institutional OCO Contract:
        1. 15-second protection timer begins on the FIRST fill of the entry order.
        2. Tracks net base quantity post-commission.
        3. Schedules 15-second deadline for cancelling entry remainder and installing protective OCO.
        """
        now_ns = event_time_ns or time.time_ns()
        entry = self._active_entries.get(client_order_id)
        if not entry:
            commission_in_base = commission if commission_asset == base_asset else Decimal("0")
            net_base = fill_qty - commission_in_base
            return {"is_entry": False, "net_base": net_base, "timer_started": False}

        entry.filled_qty += fill_qty
        if commission_asset == base_asset:
            entry.commission_in_base += commission

        timer_started = False
        if entry.first_fill_observed_at_ns is None:
            entry.first_fill_observed_at_ns = now_ns
            entry.protection_timer_expires_at_ns = now_ns + int(self.PROTECTION_TIMEOUT_SECS * 1e9)
            entry.status = "PARTIALLY_FILLED"
            timer_started = True
            self._schedule_entry_protection_timer(client_order_id, last_price=price)

        net_base = fill_qty - (commission if commission_asset == base_asset else Decimal("0"))
        total_net_base = entry.filled_qty - entry.commission_in_base

        fully_filled = entry.filled_qty >= entry.quantity
        if fully_filled:
            entry.status = "FILLED"
            self._cancel_entry_timer(client_order_id)
            asyncio.create_task(self._install_protective_oco(entry, last_price=price))

        return {
            "is_entry": True,
            "client_order_id": client_order_id,
            "net_base": net_base,
            "total_net_base": total_net_base,
            "timer_started": timer_started,
            "status": entry.status,
            "fully_filled": fully_filled,
        }

    def _schedule_entry_protection_timer(self, client_order_id: str, last_price: Decimal) -> None:
        """Schedule asynchronous 15s entry protection timeout."""
        async def _entry_timer_coro():
            await asyncio.sleep(self.PROTECTION_TIMEOUT_SECS)
            await self._handle_entry_timeout(client_order_id, last_price)

        task = asyncio.create_task(_entry_timer_coro())
        self._entry_timers[client_order_id] = task

    def _cancel_entry_timer(self, client_order_id: str) -> None:
        """Cancel running entry timer if entry filled or cancelled."""
        task = self._entry_timers.pop(client_order_id, None)
        if task and not task.done():
            task.cancel()

    async def _handle_entry_timeout(self, client_order_id: str, last_price: Decimal) -> None:
        """Execute on 15s entry timer expiration: cancel remainder and install OCO."""
        entry = self._active_entries.get(client_order_id)
        if not entry or entry.status in ("FILLED", "EXPIRED", "CANCELLED"):
            return

        entry.status = "EXPIRED"
        logger.warning(
            f"Entry protection timer ({self.PROTECTION_TIMEOUT_SECS}s) expired for {client_order_id}. "
            f"Filled: {entry.filled_qty}/{entry.quantity}. Cancelling remainder."
        )

        if self._cancel_order_fn and entry.filled_qty < entry.quantity:
            try:
                res = self._cancel_order_fn(client_order_id)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.error(f"Failed to cancel entry remainder for {client_order_id}: {e}")

        # Install protective OCO for exact net acquired base
        await self._install_protective_oco(entry, last_price)

    async def _install_protective_oco(self, entry: EntryOrderState, last_price: Decimal) -> Optional[int]:
        """Installs protective OCO for the exact net acquired base quantity."""
        if entry.protective_oco_installed:
            return entry.oco_order_list_id

        net_base = entry.filled_qty - entry.commission_in_base
        if net_base <= Decimal("0") or self.is_dust(net_base, last_price):
            logger.warning(
                f"Cannot install OCO for entry {entry.client_order_id}: "
                f"net base {net_base} is dust or zero at price {last_price}."
            )
            return None

        entry.protective_oco_installed = True
        logger.info(
            f"Installing protective OCO for entry {entry.client_order_id}: "
            f"net_base={net_base}, TP={entry.take_profit_price}, SL={entry.stop_price}"
        )

        if self._submit_oco_fn:
            try:
                res = self._submit_oco_fn(
                    entry_order_id=entry.client_order_id,
                    instrument_id=entry.instrument_id,
                    quantity=net_base,
                    take_profit_price=entry.take_profit_price,
                    stop_price=entry.stop_price,
                    stop_limit_price=entry.stop_limit_price,
                )
                if asyncio.iscoroutine(res):
                    res = await res
                if isinstance(res, int):
                    entry.oco_order_list_id = res
                return entry.oco_order_list_id
            except Exception as e:
                logger.error(f"Failed to submit protective OCO for {entry.client_order_id}: {e}")
                return None
        return None

    def register_oco(
        self,
        order_list_id: int,
        list_client_order_id: str,
        instrument_id: str,
        limit_order_id: str,
        stop_order_id: str,
        quantity: Decimal,
        limit_price: Decimal,
        stop_price: Decimal,
    ) -> OcoState:
        """Register newly submitted OCO pair."""
        limit_leg = OcoLeg(
            client_order_id=limit_order_id,
            order_type="LIMIT_MAKER",
            price=limit_price,
            quantity=quantity,
        )
        stop_leg = OcoLeg(
            client_order_id=stop_order_id,
            order_type="STOP_LOSS_LIMIT",
            stop_price=stop_price,
            quantity=quantity,
        )
        oco = OcoState(
            order_list_id=order_list_id,
            list_client_order_id=list_client_order_id,
            instrument_id=instrument_id,
            limit_leg=limit_leg,
            stop_leg=stop_leg,
        )
        self._active_ocos[order_list_id] = oco
        self._client_order_to_list_id[limit_order_id] = order_list_id
        self._client_order_to_list_id[stop_order_id] = order_list_id
        return oco

    def on_fill(
        self,
        client_order_id: str,
        fill_qty: Decimal,
        price: Decimal,
        commission: Decimal,
        commission_asset: str,
        base_asset: str,
        event_time_ns: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Process fill event on an entry order or an OCO leg, starting 15s protection timer on first partial fill."""
        if client_order_id in self._active_entries:
            res = self.on_entry_fill(
                client_order_id=client_order_id,
                fill_qty=fill_qty,
                price=price,
                commission=commission,
                commission_asset=commission_asset,
                base_asset=base_asset,
                event_time_ns=event_time_ns,
            )
            res["is_oco"] = False
            return res

        now_ns = event_time_ns or time.time_ns()
        order_list_id = self._client_order_to_list_id.get(client_order_id)
        if not order_list_id or order_list_id not in self._active_ocos:
            # Not a tracked OCO order
            net_base = fill_qty - (commission if commission_asset == base_asset else Decimal("0"))
            return {"is_oco": False, "net_base": net_base, "timer_started": False}

        oco = self._active_ocos[order_list_id]
        leg = oco.limit_leg if oco.limit_leg.client_order_id == client_order_id else oco.stop_leg
        leg.filled_qty += fill_qty

        # Calculate exact net base post-commission
        commission_in_base = commission if commission_asset == base_asset else Decimal("0")
        net_base = fill_qty - commission_in_base

        timer_started = False
        if oco.first_fill_observed_at_ns is None:
            oco.first_fill_observed_at_ns = now_ns
            oco.protection_timer_expires_at_ns = now_ns + int(self.PROTECTION_TIMEOUT_SECS * 1e9)
            oco.protection_status = "TIMER_RUNNING"
            timer_started = True

            # Schedule protection timeout handler
            counterpart_leg = oco.stop_leg if leg is oco.limit_leg else oco.limit_leg
            self._schedule_protection_timer(order_list_id, counterpart_leg.client_order_id)

        # Check if fully settled
        total_filled = oco.limit_leg.filled_qty + oco.stop_leg.filled_qty
        if total_filled >= oco.limit_leg.quantity:
            oco.protection_status = "SETTLED"
            self._cancel_timer(order_list_id)

        return {
            "is_oco": True,
            "order_list_id": order_list_id,
            "net_base": net_base,
            "timer_started": timer_started,
            "protection_status": oco.protection_status,
        }

    def _schedule_protection_timer(self, order_list_id: int, counterpart_order_id: str) -> None:
        """Schedule asynchronous 15s protection timeout."""
        async def _timer_coro():
            await asyncio.sleep(self.PROTECTION_TIMEOUT_SECS)
            await self._handle_protection_timeout(order_list_id, counterpart_order_id)

        task = asyncio.create_task(_timer_coro())
        self._timers[order_list_id] = task

    def _cancel_timer(self, order_list_id: int) -> None:
        """Cancel running timer if OCO settled normally."""
        task = self._timers.pop(order_list_id, None)
        if task and not task.done():
            task.cancel()

    async def _handle_protection_timeout(self, order_list_id: int, counterpart_order_id: str) -> None:
        """Execute on 15s timer expiration: cancel unfilled counterpart and check dust."""
        oco = self._active_ocos.get(order_list_id)
        if not oco or oco.protection_status == "SETTLED":
            return

        oco.protection_status = "EXPIRED"
        logger.warning(
            f"OCO protection timer ({self.PROTECTION_TIMEOUT_SECS}s) expired for order_list_id {order_list_id}. "
            f"Cancelling counterpart order {counterpart_order_id}."
        )

        if self._cancel_order_fn:
            try:
                res = self._cancel_order_fn(counterpart_order_id)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.error(f"Failed to cancel OCO counterpart order {counterpart_order_id}: {e}")

    def is_dust(self, quantity: Decimal, price: Decimal) -> bool:
        """Check if quantity or notional falls below exchange minimum tradeable sizes."""
        if quantity < self._min_qty:
            return True
        notional = quantity * price
        if notional < self._min_notional:
            return True
        return False

    async def cancel_active_ocos_for_flatten(self) -> None:
        """Safe pre-flattening: cancel all open OCO legs and active entries before dispatching market flat order."""
        for entry_id, entry in list(self._active_entries.items()):
            if entry.status in ("NEW", "PARTIALLY_FILLED"):
                self._cancel_entry_timer(entry_id)
                entry.status = "CANCELLED"
                if self._cancel_order_fn:
                    try:
                        res = self._cancel_order_fn(entry_id)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as e:
                        logger.error(f"Error cancelling entry order {entry_id}: {e}")

        for order_list_id, oco in list(self._active_ocos.items()):
            if oco.protection_status in ("ACTIVE", "TIMER_RUNNING"):
                logger.info(f"Cancelling active OCO {order_list_id} prior to market flattening.")
                self._cancel_timer(order_list_id)
                for leg in (oco.limit_leg, oco.stop_leg):
                    if leg.filled_qty < leg.quantity and self._cancel_order_fn:
                        try:
                            res = self._cancel_order_fn(leg.client_order_id)
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception as e:
                            logger.error(f"Error cancelling OCO leg {leg.client_order_id}: {e}")
                oco.protection_status = "SETTLED"
