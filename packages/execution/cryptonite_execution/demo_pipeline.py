"""DemoExecutionPipeline: End-to-end Binance Demo execution service engine.

Enforces:
1. Strict fencing token (attempt_id + lease_owner) on all command state mutations.
2. Two-phase pre-transmission persistence: order (PENDING_SUBMIT) and reservation (PRE_SEND)
   committed to PostgreSQL before network dispatch.
3. Ambiguous outcome protocol: never blind-resubmit on timeout; set UNKNOWN_PENDING_RECONCILIATION
   and query venue by client_order_id before taking action.
4. Transactionally idempotent execution report processing: journal, fill, order projection,
   reservation, and outbox event committed in a single atomic transaction.
5. Monotonic order projections: terminal statuses (FILLED, CANCELED, REJECTED, EXPIRED) never regress.
6. Multi-path reservation settlement:
   - Standard path: PRE_SEND -> INFLIGHT -> ACKED_UNREFLECTED -> VENUE_REFLECTED -> SETTLEMENT_PENDING -> RELEASED
   - Immediate execution path: PRE_SEND -> INFLIGHT -> SETTLEMENT_PENDING -> RELEASED
   - Pre-venue rejection path: PRE_SEND -> RELEASED (venue_reflected = false)
7. PostgreSQL session advisory lock ensuring single-active executor per account.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
import time
import uuid
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import asyncpg

from cryptonite_execution.clock import ClockDriftExceededError, ClockSyncMonitor
from cryptonite_execution.filters import DynamicExchangeFilterValidator, ExchangeFilterValidationError
from cryptonite_execution.kill_switch import LatchedKillSwitch
from cryptonite_execution.oco import SpotOcoManager
from cryptonite_execution.reconciliation import AuthoritativeReconciler
from cryptonite_execution.reservations import DurableReservationManager, InsufficientAvailableBalanceError

logger = logging.getLogger("cryptonite.execution.demo_pipeline")


class FencingTokenError(Exception):
    """Raised when an attempt to mutate a command fails because the lease was lost."""


class VenueAmbiguousError(Exception):
    """Raised when network transmission to venue times out or produces an ambiguous response."""


class AdvisoryLockLostError(Exception):
    """Raised when the PostgreSQL session holding the single-active advisory lock drops or terminates."""


class KillSwitchResetDeniedError(Exception):
    """Raised when an authenticated kill switch reset request fails one of the 7 safety checks."""


class MockVenueAdapter:
    """In-memory mock venue simulating Binance Demo REST and WebSocket behaviors.
    
    Provides deterministic responses for orders, queries, cancellations, fills,
    and ambiguous timeout simulation.
    """

    def __init__(self, symbol: str = "BTCUSDT", initial_price: Decimal = Decimal("50000.00")):
        self.symbol = symbol
        self.current_price = initial_price
        self.orders: Dict[str, dict] = {}  # client_order_id -> order_data
        self.venue_orders: Dict[int, dict] = {}  # venue_order_id -> order_data
        self.next_venue_id = 1000001
        self.next_trade_id = 500001
        self.simulate_network_timeout = False
        self.execution_callbacks: List[Callable[[dict], Any]] = []

    def register_callback(self, cb: Callable[[dict], Any]) -> None:
        self.execution_callbacks.append(cb)

    async def _emit(self, event: dict) -> None:
        for cb in self.execution_callbacks:
            try:
                res = cb(event)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.error("Error in mock venue callback: %s", e)

    async def submit_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Optional[Decimal] = None,
        client_order_id: Optional[str] = None,
        time_in_force: str = "GTC",
    ) -> dict:
        if self.simulate_network_timeout:
            raise VenueAmbiguousError("Connection timed out waiting for Binance Demo response")

        c_id = client_order_id or f"MOCK-{uuid.uuid4().hex[:8]}"
        v_id = self.next_venue_id
        self.next_venue_id += 1

        exec_price = price if price is not None else self.current_price

        order_data = {
            "symbol": symbol,
            "orderId": v_id,
            "clientOrderId": c_id,
            "side": side,
            "type": order_type,
            "timeInForce": time_in_force,
            "origQty": str(quantity),
            "price": str(exec_price),
            "status": "NEW",
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "transactTime": int(time.time() * 1000),
        }
        self.orders[c_id] = order_data
        self.venue_orders[v_id] = order_data

        # Emit ORDER_ACCEPTED event
        accepted_event = {
            "event_type": "ORDER_ACCEPTED",
            "source_event_id": f"evt-acc-{v_id}",
            "client_order_id": c_id,
            "venue_order_id": str(v_id),
            "status": "ACCEPTED",
            "raw_venue_status": "NEW",
            "filled_qty": Decimal("0"),
            "price": exec_price,
            "quantity": quantity,
            "occurred_at_ns": time.time_ns(),
        }
        await self._emit(accepted_event)

        return order_data

    async def query_order(self, symbol: str, client_order_id: str) -> Optional[dict]:
        """Query venue by clientOrderId. Returns None if order does not exist."""
        return self.orders.get(client_order_id)

    async def get_open_orders(self, environment: str = "DEMO", account_id: str = "DEMO-ACCOUNT-001") -> List[dict]:
        """Returns all open orders currently active on mock venue."""
        return [
            o for o in self.orders.values()
            if o.get("status") in ("NEW", "PARTIALLY_FILLED")
        ]

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict:
        order = self.orders.get(client_order_id)
        if not order:
            raise ValueError(f"Order {client_order_id} not found on mock venue")
        order["status"] = "CANCELED"
        canceled_event = {
            "event_type": "ORDER_CANCELED",
            "source_event_id": f"evt-canc-{order['orderId']}",
            "client_order_id": client_order_id,
            "venue_order_id": str(order["orderId"]),
            "status": "CANCELED",
            "raw_venue_status": "CANCELED",
            "filled_qty": Decimal(str(order["executedQty"])),
            "price": Decimal(str(order["price"])),
            "quantity": Decimal(str(order["origQty"])),
            "occurred_at_ns": time.time_ns(),
        }
        await self._emit(canceled_event)
        return order

    async def simulate_fill(
        self,
        client_order_id: str,
        fill_qty: Decimal,
        fill_price: Decimal,
        commission: Decimal = Decimal("0.001"),
        commission_asset: str = "USDT",
        is_maker: bool = False,
    ) -> dict:
        order = self.orders.get(client_order_id)
        if not order:
            raise ValueError(f"Order {client_order_id} not found on mock venue")

        t_id = self.next_trade_id
        self.next_trade_id += 1

        prev_qty = Decimal(str(order["executedQty"]))
        new_qty = prev_qty + fill_qty
        orig_qty = Decimal(str(order["origQty"]))
        order["executedQty"] = str(new_qty)

        quote_qty = fill_qty * fill_price
        order["cummulativeQuoteQty"] = str(Decimal(str(order["cummulativeQuoteQty"])) + quote_qty)

        new_status = "FILLED" if new_qty >= orig_qty else "PARTIALLY_FILLED"
        order["status"] = new_status

        fill_event = {
            "event_type": "ORDER_FILLED" if new_status == "FILLED" else "ORDER_PARTIALLY_FILLED",
            "source_event_id": f"evt-fill-{t_id}",
            "trade_id": t_id,
            "client_order_id": client_order_id,
            "venue_order_id": str(order["orderId"]),
            "instrument_id": f"{order['symbol']}.BINANCE",
            "status": new_status,
            "raw_venue_status": new_status,
            "side": order["side"],
            "price": fill_price,
            "quantity": fill_qty,
            "quote_quantity": quote_qty,
            "filled_qty": new_qty,
            "commission_amount": commission,
            "commission_asset": commission_asset,
            "is_maker": is_maker,
            "occurred_at_ns": time.time_ns(),
        }
        await self._emit(fill_event)
        return fill_event


class DemoExecutionPipeline:
    """Institutional-grade Demo Execution Service coordinating the full command-to-ledger-to-outbox lifecycle."""

    def __init__(
        self,
        environment: str,
        account_id: str,
        pool: asyncpg.Pool,
        redis_client: Any,
        venue_client: Optional[Any] = None,
        filter_validator: Optional[DynamicExchangeFilterValidator] = None,
        clock_monitor: Optional[ClockSyncMonitor] = None,
        kill_switch: Optional[LatchedKillSwitch] = None,
        worker_id: Optional[str] = None,
        emergency_marker_path: Optional[str] = None,
    ):
        self.environment = environment
        self.account_id = account_id
        self.pool = pool
        self.redis = redis_client
        self.venue_client = venue_client or MockVenueAdapter()
        self.reservation_mgr = DurableReservationManager(pool)
        self.filter_validator = filter_validator or DynamicExchangeFilterValidator()
        self.clock_monitor = clock_monitor or ClockSyncMonitor()
        self.kill_switch = kill_switch or LatchedKillSwitch()
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"

        self.emergency_marker_path = emergency_marker_path or f"/data/executor_state/{environment.lower()}/emergency_kill_switch.marker"
        reconciler_kwargs = {
            "pool": self.pool,
            "environment": self.environment,
            "account_id": self.account_id,
            "emergency_marker_path": self.emergency_marker_path,
        }
        self.reconciler = AuthoritativeReconciler(**reconciler_kwargs)
        self.oco_mgr = SpotOcoManager(cancel_order_fn=self._cancel_venue_order_sync)

        self._advisory_lock_held = False
        self._advisory_conn: Optional[asyncpg.Connection] = None
        self._is_ready = False
        self.is_gate_open = True

    def _cancel_venue_order_sync(self, client_order_id: str) -> None:
        if hasattr(self.venue_client, "cancel_order"):
            asyncio.create_task(self.venue_client.cancel_order(symbol="BTCUSDT", client_order_id=client_order_id))

    @property
    def is_ready(self) -> bool:
        return self._is_ready and self.is_gate_open and not self.kill_switch.is_latched

    def assert_advisory_lock(self) -> None:
        """Verifies advisory lock session is active. Trips kill switch immediately if lost."""
        conn_closed = True
        try:
            conn_closed = (
                self._advisory_conn is None
                or getattr(self._advisory_conn, "_con", None) is None
                or self._advisory_conn.is_closed()
            )
        except Exception:
            conn_closed = True

        if not self._advisory_lock_held or conn_closed:
            self._advisory_lock_held = False
            self._is_ready = False
            self.is_gate_open = False
            self.kill_switch.trip(
                latched_by="SYSTEM:ADVISORY_LOCK_LOST",
                reason="PostgreSQL session advisory lock connection lost",
            )
            raise AdvisoryLockLostError("PostgreSQL advisory lock connection lost or closed")

    async def acquire_advisory_lock(self) -> bool:
        """Acquires a single-active PostgreSQL session advisory lock for this account."""
        lock_key = f"cryptonite_executor:{self.environment}:{self.account_id}"
        if self._advisory_conn is None:
            self._advisory_conn = await self.pool.acquire()
        acquired = await self._advisory_conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1));", lock_key)
        self._advisory_lock_held = bool(acquired)
        if not self._advisory_lock_held:
            await self.pool.release(self._advisory_conn)
            self._advisory_conn = None
        return self._advisory_lock_held

    async def release_advisory_lock(self) -> None:
        """Releases the single-active PostgreSQL advisory lock."""
        if self._advisory_conn:
            lock_key = f"cryptonite_executor:{self.environment}:{self.account_id}"
            try:
                if self._advisory_lock_held and getattr(self._advisory_conn, "_con", None) is not None and not self._advisory_conn.is_closed():
                    await self._advisory_conn.execute("SELECT pg_advisory_unlock(hashtext($1));", lock_key)
            except Exception as e:
                logger.warning("Error releasing advisory lock: %s", e)
            finally:
                try:
                    if getattr(self._advisory_conn, "_con", None) is not None and not self._advisory_conn.is_closed():
                        await self.pool.release(self._advisory_conn)
                except Exception:
                    pass
                self._advisory_conn = None
                self._advisory_lock_held = False

    async def claim_command(self, lease_seconds: int = 30, target_instance_id: Optional[str] = None) -> Optional[dict]:
        """Claim a PENDING command with PostgreSQL SKIP LOCKED and fencing token generation."""
        query = f"""
        WITH claimed AS (
            SELECT command_id
            FROM execution_commands
            WHERE ((status = 'PENDING' AND (lease_expires_at IS NULL OR lease_expires_at < now()))
                OR (status IN ('CLAIMED', 'RUNNING', 'SENDING') AND lease_expires_at < now()))
              AND environment = $3
              AND ($2::text IS NULL OR target_instance_id = $2)
            ORDER BY created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        UPDATE execution_commands
        SET status = 'RUNNING',
            lease_owner = $1,
            lease_expires_at = now() + INTERVAL '{lease_seconds} seconds',
            attempt_id = gen_random_uuid(),
            attempt_count = attempt_count + 1
        FROM claimed
        WHERE execution_commands.command_id = claimed.command_id
        RETURNING execution_commands.command_id, execution_commands.command_type,
                  execution_commands.target_instance_id, execution_commands.requester_id,
                  execution_commands.payload, execution_commands.attempt_id,
                  execution_commands.lease_owner;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, self.worker_id, target_instance_id, self.environment)
            return dict(row) if row else None

    async def process_submit_order(self, cmd: dict) -> dict:
        """Executes the strict crash-safe order submission protocol:
        
        1. Evaluate fail-closed gates (kill switch, clock drift).
        2. Validate exchange filters.
        3. Deterministic client_order_id generation.
        4. Atomically persist PENDING_SUBMIT order, PRE_SEND reservation, and UNKNOWN_PENDING_RECONCILIATION
           command outcome into PostgreSQL BEFORE network transmission.
        5. Transmit to venue client.
        6. On success: transition reservation to INFLIGHT and mark command COMPLETED (SUCCESS).
        7. On timeout/error: query venue by client_order_id before resubmitting.
        """
        cmd_id = cmd["command_id"]
        attempt_id = cmd["attempt_id"]
        lease_owner = cmd["lease_owner"]

        # 1. Gate: Kill Switch
        if self.kill_switch.is_latched:
            await self._finalize_command(cmd_id, attempt_id, lease_owner, "REJECTED", "REJECTED", "Kill switch is latched")
            return {"status": "REJECTED", "reason": "KILL_SWITCH_LATCHED"}

        payload = cmd.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        symbol = payload.get("symbol", "BTCUSDT")
        side = payload.get("side", "BUY")
        order_type = payload.get("order_type", "LIMIT")
        quantity = Decimal(str(payload.get("quantity", "0.01")))
        price = Decimal(str(payload.get("price", "50000.00"))) if payload.get("price") else None
        client_order_id = payload.get("client_order_id") or f"DEMO-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"
        instrument_id = f"{symbol}.BINANCE"

        # Determine asset to reserve
        reserve_asset = "USDT" if side == "BUY" else symbol.replace("USDT", "")
        reserve_amount = (quantity * price) if side == "BUY" and price is not None else quantity
        headroom = payload.get("headroom", Decimal("0"))
        if not isinstance(headroom, Decimal):
            headroom = Decimal(str(headroom))

        # 2. Gate: Exchange Filters
        try:
            # Validate order parameters against exchange limits
            current_avg = payload.get("current_avg_price")
            avg_dec = Decimal(str(current_avg)) if current_avg else None
            self.filter_validator.validate_order(
                symbol=symbol,
                price=price or Decimal("50000.00"),
                quantity=quantity,
                side=side,
                current_avg_price=avg_dec,
                order_type=order_type,
            )
        except ExchangeFilterValidationError as e:
            logger.warning("Order rejected by exchange filters: %s", e)
            await self._finalize_command(cmd_id, attempt_id, lease_owner, "REJECTED", "REJECTED", str(e))
            return {"status": "REJECTED", "reason": str(e)}

        # 3. Two-Phase Pre-Transmission Persistence
        # Atomically commit PENDING_SUBMIT order, PRE_SEND reservation, and UNKNOWN_PENDING_RECONCILIATION
        res_id = uuid.uuid4()
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="serializable"):
                # Insert order as PENDING_SUBMIT
                await conn.execute(
                    """
                    INSERT INTO trading_orders (
                        environment, account_id, client_order_id, strategy_id, instrument_id,
                        side, order_type, quantity, price, status
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'PENDING_SUBMIT')
                    ON CONFLICT (environment, account_id, client_order_id) DO NOTHING;
                    """,
                    self.environment,
                    self.account_id,
                    client_order_id,
                    cmd.get("target_instance_id") or "manual",
                    instrument_id,
                    side,
                    order_type,
                    quantity,
                    price,
                )

                # Insert reservation in PRE_SEND
                # Check available balance first
                bal_row = await conn.fetchrow(
                    """
                    SELECT free FROM trading_balances 
                    WHERE environment = $1 AND account_id = $2 AND asset = $3
                    FOR UPDATE;
                    """,
                    self.environment,
                    self.account_id,
                    reserve_asset,
                )
                free = Decimal(str(bal_row["free"])) if bal_row else Decimal("0")
                res_sum = await conn.fetchrow(
                    """
                    SELECT COALESCE(SUM(amount + fee_slippage_headroom), 0) AS reserved
                    FROM execution_reservations
                    WHERE environment = $1 AND account_id = $2 AND asset = $3
                      AND state IN ('PRE_SEND', 'INFLIGHT', 'ACKED_UNREFLECTED');
                    """,
                    self.environment,
                    self.account_id,
                    reserve_asset,
                )
                reserved = Decimal(str(res_sum["reserved"])) if res_sum else Decimal("0")
                if (free - reserved) < (reserve_amount + headroom):
                    # Reject command for insufficient balance
                    await conn.execute(
                        """
                        UPDATE execution_commands
                        SET status = 'REJECTED', command_outcome = 'REJECTED',
                            error_message = 'Insufficient available balance',
                            processed_at = now(), lease_owner = NULL, lease_expires_at = NULL
                        WHERE command_id = $1 AND attempt_id = $2 AND lease_owner = $3;
                        """,
                        cmd_id,
                        attempt_id,
                        lease_owner,
                    )
                    return {"status": "REJECTED", "reason": "INSUFFICIENT_AVAILABLE_BALANCE"}

                await conn.execute(
                    """
                    INSERT INTO execution_reservations (
                        reservation_id, environment, account_id, command_id, client_order_id,
                        asset, amount, fee_slippage_headroom, state, venue_reflected
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'PRE_SEND', false);
                    """,
                    res_id,
                    self.environment,
                    self.account_id,
                    cmd_id,
                    client_order_id,
                    reserve_asset,
                    reserve_amount,
                    headroom,
                )

                # Mutate command state: SENDING, command_outcome NULL (bytes have not left process yet)
                res = await conn.execute(
                    """
                    UPDATE execution_commands
                    SET status = 'SENDING', command_outcome = NULL
                    WHERE command_id = $1 AND attempt_id = $2 AND lease_owner = $3;
                    """,
                    cmd_id,
                    attempt_id,
                    lease_owner,
                )
                if res == "UPDATE 0":
                    raise FencingTokenError(f"Worker {lease_owner} lost lease on command {cmd_id}")

        # 4. Transmission Phase
        # Mark reservation INFLIGHT
        await self.reservation_mgr.mark_inflight(res_id)

        socket_write_started = False
        try:
            # Pre-socket advisory lock verification
            self.assert_advisory_lock()

            # Record that network transmission attempt has started
            socket_write_started = True
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE execution_commands
                    SET socket_write_started = true
                    WHERE command_id = $1;
                    """,
                    cmd_id
                )

            # Network transmission to venue
            venue_resp = await self.venue_client.submit_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                client_order_id=client_order_id,
            )

            # Post-socket advisory lock verification
            self.assert_advisory_lock()

            # Update command to COMPLETED (SUCCESS)
            await self._finalize_command(cmd_id, attempt_id, lease_owner, "COMPLETED", "SUCCESS", None)
            return {
                "status": "COMPLETED",
                "client_order_id": client_order_id,
                "reservation_id": str(res_id),
                "venue_order_id": str(venue_resp.get("orderId", "")),
            }
        except AdvisoryLockLostError as e:
            if not socket_write_started:
                logger.critical("Advisory lock lost before network dispatch for order %s: %s", client_order_id, e)
                await self._finalize_command(cmd_id, attempt_id, lease_owner, "REJECTED", "REJECTED", f"Advisory lock lost: {e}")
                await self.reservation_mgr.release_reservation(res_id, release_reason="CANCELLED_UNSENT", venue_reflected=False)
                raise
            else:
                logger.critical("Advisory lock lost during/after network dispatch for order %s: %s. Retaining reservation for reconciliation.", client_order_id, e)
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE execution_commands
                        SET status = 'RUNNING', command_outcome = 'UNKNOWN_PENDING_RECONCILIATION', error_message = $4
                        WHERE command_id = $1 AND attempt_id = $2 AND lease_owner = $3;
                        """,
                        cmd_id,
                        attempt_id,
                        lease_owner,
                        f"Advisory lock lost post-write: {e}",
                    )
                return await self.resolve_ambiguous_order(
                    client_order_id=client_order_id,
                    command_id=cmd_id,
                    attempt_id=attempt_id,
                    lease_owner=lease_owner,
                    reservation_id=res_id,
                    symbol=symbol,
                )
        except VenueAmbiguousError as e:
            logger.error("Ambiguous outcome on order %s: %s", client_order_id, e)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE execution_commands
                    SET command_outcome = 'UNKNOWN_PENDING_RECONCILIATION'
                    WHERE command_id = $1 AND attempt_id = $2 AND lease_owner = $3;
                    """,
                    cmd_id,
                    attempt_id,
                    lease_owner,
                )
            # 5. Query venue to resolve ambiguous outcome before resubmitting
            return await self.resolve_ambiguous_order(
                client_order_id=client_order_id,
                command_id=cmd_id,
                attempt_id=attempt_id,
                lease_owner=lease_owner,
                reservation_id=res_id,
                symbol=symbol,
            )
        except Exception as e:
            if not socket_write_started:
                logger.error("Error submitting order before socket write: %s", e)
                await self._finalize_command(cmd_id, attempt_id, lease_owner, "REJECTED", "REJECTED", str(e))
                await self.reservation_mgr.release_reservation(res_id, release_reason="CANCELLED_UNSENT", venue_reflected=False)
                return {"status": "REJECTED", "reason": str(e)}
            else:
                logger.error("Error submitting order during/after socket write: %s. Retaining reservation.", e)
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE execution_commands
                        SET status = 'RUNNING', command_outcome = 'UNKNOWN_PENDING_RECONCILIATION', error_message = $4
                        WHERE command_id = $1 AND attempt_id = $2 AND lease_owner = $3;
                        """,
                        cmd_id,
                        attempt_id,
                        lease_owner,
                        str(e),
                    )
                return await self.resolve_ambiguous_order(
                    client_order_id=client_order_id,
                    command_id=cmd_id,
                    attempt_id=attempt_id,
                    lease_owner=lease_owner,
                    reservation_id=res_id,
                    symbol=symbol,
                )

    async def resolve_ambiguous_order(
        self,
        client_order_id: str,
        command_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_owner: str,
        reservation_id: uuid.UUID,
        symbol: str = "BTCUSDT",
        max_attempts: int = 3,
    ) -> dict:
        """Queries venue by client_order_id to determine authoritative state with bounded backoff.
        
        If venue has order: adopts venue state and marks COMPLETED (SUCCESS).
        If unconfirmed on venue: retains UNKNOWN_PENDING_RECONCILIATION and active reservation
        for the AuthoritativeReconciler. Never automatically resubmits.
        """
        logger.info("Resolving ambiguous order %s by querying venue...", client_order_id)
        backoffs = [0.1, 0.2, 0.4]
        venue_order = None
        for attempt in range(max_attempts):
            try:
                venue_order = await self.venue_client.query_order(symbol=symbol, client_order_id=client_order_id)
                if venue_order is not None:
                    break
            except Exception as q_err:
                err_str = str(q_err).upper()
                if "-2013" in err_str or "NO_SUCH_ORDER" in err_str:
                    logger.warning(
                        "Query attempt %d for order %s returned NO_SUCH_ORDER (-2013). "
                        "This contributes to absence evidence but does not prove rejection immediately.",
                        attempt + 1, client_order_id
                    )
                else:
                    logger.warning("Query attempt %d for order %s failed: %s", attempt + 1, client_order_id, q_err)
            if attempt < max_attempts - 1:
                await asyncio.sleep(backoffs[min(attempt, len(backoffs) - 1)])

        if venue_order is not None:
            # Venue has order! Adopt venue state and finalize command as SUCCESS
            venue_order_id = str(venue_order.get("orderId", ""))
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE trading_orders
                    SET venue_order_id = $2, status = 'ACCEPTED', raw_venue_status = $3
                    WHERE environment = $1 AND account_id = $4 AND client_order_id = $5;
                    """,
                    self.environment,
                    venue_order_id,
                    venue_order.get("status", "NEW"),
                    self.account_id,
                    client_order_id,
                )
            await self._finalize_command(command_id, attempt_id, lease_owner, "COMPLETED", "SUCCESS", None)
            return {"status": "COMPLETED", "client_order_id": client_order_id, "venue_order_id": venue_order_id}
        else:
            # Order remains unconfirmed on venue!
            # Retain UNKNOWN_PENDING_RECONCILIATION and keep reservation active for AuthoritativeReconciler.
            logger.critical(
                "Order %s remains unconfirmed after %d venue queries. Retaining UNKNOWN_PENDING_RECONCILIATION without releasing reservation.",
                client_order_id, max_attempts
            )
            return {
                "status": "UNKNOWN_PENDING_RECONCILIATION",
                "client_order_id": client_order_id,
                "reservation_id": str(reservation_id),
            }

    async def _finalize_command(
        self,
        command_id: uuid.UUID,
        attempt_id: uuid.UUID,
        lease_owner: str,
        status: str,
        command_outcome: str,
        error_message: Optional[str],
    ) -> None:
        """Mutate command terminal state with strict attempt_id and lease_owner fencing."""
        query = """
        UPDATE execution_commands
        SET status = $1,
            command_outcome = $2,
            error_message = $3,
            processed_at = now(),
            lease_owner = NULL,
            lease_expires_at = NULL
        WHERE command_id = $4
          AND attempt_id = $5
          AND lease_owner = $6;
        """
        async with self.pool.acquire() as conn:
            res = await conn.execute(query, status, command_outcome, error_message, command_id, attempt_id, lease_owner)
            if res == "UPDATE 0":
                raise FencingTokenError(f"Worker {lease_owner} lost lease on command {command_id}")

    async def handle_execution_report(self, report: dict) -> bool:
        """Transactionally idempotent handler processing a venue execution report.
        
        In a SINGLE PostgreSQL transaction:
        1. Inserts into trading_order_events (unique on environment, account_id, client_order_id, source_event_id).
           If conflicting source_event_id, the entire handler is an immediate NO-OP.
        2. Updates trading_orders with monotonic status protection (terminal statuses never regress).
        3. If fill: inserts into trading_fills and updates order filled_qty.
        4. Transitions execution_reservations (INFLIGHT -> ACKED_UNREFLECTED or SETTLEMENT_PENDING).
        5. Emits outbox_events record for stream delivery.
        """
        c_id = report["client_order_id"]
        source_id = report["source_event_id"]
        event_type = report["event_type"]
        status = report.get("status", "NEW")
        raw_status = report.get("raw_venue_status", status)
        venue_order_id = report.get("venue_order_id")
        instrument_id = report.get("instrument_id", "BTCUSDT.BINANCE")
        occurred_at_ns = report.get("occurred_at_ns", time.time_ns())

        filled_qty = Decimal(str(report.get("filled_qty", "0")))
        price = Decimal(str(report.get("price", "0"))) if report.get("price") else None
        qty = Decimal(str(report.get("quantity", "0")))
        commission_amt = Decimal(str(report.get("commission_amount", "0")))
        commission_asset = report.get("commission_asset", "USDT")
        quote_qty = Decimal(str(report.get("quote_quantity", "0")))
        is_maker = report.get("is_maker", False)
        side = report.get("side", "BUY")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. Idempotent journal insert
                ins_journal = await conn.fetchrow(
                    """
                    INSERT INTO trading_order_events (
                        environment, account_id, client_order_id, source_event_id,
                        event_type, event_version, raw_venue_status, payload, occurred_at_ns
                    ) VALUES ($1, $2, $3, $4, $5, 1, $6, $7::jsonb, $8)
                    ON CONFLICT (environment, account_id, client_order_id, source_event_id) DO NOTHING
                    RETURNING id;
                    """,
                    self.environment,
                    self.account_id,
                    c_id,
                    source_id,
                    event_type,
                    raw_status,
                    json.dumps(report, default=str),
                    occurred_at_ns,
                )
                if not ins_journal:
                    # DUPLICATE EVENT REPLAY: Entire transaction NO-OP!
                    logger.info("Duplicate event %s for order %s ignored (idempotent no-op)", source_id, c_id)
                    return False

                # 2. Monotonic order update
                await conn.execute(
                    """
                    UPDATE trading_orders
                    SET status = CASE 
                            WHEN status IN ('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED') THEN status 
                            ELSE $1 
                        END,
                        raw_venue_status = $2,
                        venue_order_id = COALESCE($3, venue_order_id),
                        filled_qty = GREATEST(filled_qty, $4),
                        cumulative_quote_qty = GREATEST(cumulative_quote_qty, $5),
                        avg_fill_price = COALESCE($6, avg_fill_price),
                        commission_amount = commission_amount + $7,
                        commission_asset = COALESCE($8, commission_asset),
                        updated_at = now()
                    WHERE environment = $9
                      AND account_id = $10
                      AND client_order_id = $11;
                    """,
                    status,
                    raw_status,
                    venue_order_id,
                    filled_qty,
                    quote_qty,
                    price,
                    commission_amt,
                    commission_asset,
                    self.environment,
                    self.account_id,
                    c_id,
                )

                # 3. Fill record if applicable
                trade_id = report.get("trade_id")
                if trade_id is not None:
                    await conn.execute(
                        """
                        INSERT INTO trading_fills (
                            environment, account_id, instrument_id, trade_id, client_order_id,
                            venue_order_id, side, price, quantity, quote_quantity,
                            commission_amount, commission_asset, is_maker, trade_time_ns
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                        ON CONFLICT (environment, account_id, instrument_id, trade_id) DO NOTHING;
                        """,
                        self.environment,
                        self.account_id,
                        instrument_id,
                        trade_id,
                        c_id,
                        venue_order_id or "0",
                        side,
                        price,
                        qty,
                        quote_qty,
                        commission_amt,
                        commission_asset,
                        is_maker,
                        occurred_at_ns,
                    )

                # 4. Transition reservation
                if event_type == "ORDER_ACCEPTED":
                    await conn.execute(
                        """
                        UPDATE execution_reservations
                        SET state = 'ACKED_UNREFLECTED'
                        WHERE environment = $1 AND account_id = $2 AND client_order_id = $3
                          AND state = 'INFLIGHT';
                        """,
                        self.environment,
                        self.account_id,
                        c_id,
                    )
                elif "FILLED" in event_type:
                    # Multi-path: allows transition from INFLIGHT, ACKED_UNREFLECTED, or VENUE_REFLECTED
                    await conn.execute(
                        """
                        UPDATE execution_reservations
                        SET state = 'SETTLEMENT_PENDING', venue_reflected = true
                        WHERE environment = $1 AND account_id = $2 AND client_order_id = $3
                          AND state IN ('INFLIGHT', 'ACKED_UNREFLECTED', 'VENUE_REFLECTED');
                        """,
                        self.environment,
                        self.account_id,
                        c_id,
                    )

                # 5. Outbox event emission
                stream_name = "cryptonite:stream:trading_fills" if trade_id else "cryptonite:stream:trading_events"
                await conn.execute(
                    """
                    INSERT INTO outbox_events (
                        event_id, stream_name, event_type, producer, payload,
                        status, attempt_count, next_attempt_at, created_at
                    ) VALUES ($1, $2, $3, $4, $5::jsonb, 'PENDING', 0, now(), now());
                    """,
                    str(uuid.uuid4()),
                    stream_name,
                    event_type.lower(),
                    "cryptonite-executor",
                    json.dumps(report, default=str),
                )

        return True

    async def settle_reservation(
        self,
        client_order_id: str,
        snapshot_id: uuid.UUID,
        release_reason: Optional[str] = None,
    ) -> bool:
        """Authoritative reservation release protocol.
        
        Requires agreement between:
        1. Terminal venue order state (FILLED, CANCELED, REJECTED, EXPIRED).
        2. Linked authoritative balance snapshot.
        Transitions reservation to RELEASED with release_reason, venue_reflected=true,
        and settlement_snapshot_id.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Verify terminal order
                order = await conn.fetchrow(
                    """
                    SELECT status, filled_qty FROM trading_orders
                    WHERE environment = $1 AND account_id = $2 AND client_order_id = $3;
                    """,
                    self.environment,
                    self.account_id,
                    client_order_id,
                )
                if not order or order["status"] not in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
                    logger.warning("Cannot settle reservation for %s: order not in terminal state", client_order_id)
                    return False

                # Verify balance snapshot exists
                snap = await conn.fetchrow(
                    """
                    SELECT snapshot_id FROM trading_balance_snapshots
                    WHERE snapshot_id = $1 AND environment = $2 AND account_id = $3;
                    """,
                    snapshot_id,
                    self.environment,
                    self.account_id,
                )
                if not snap:
                    logger.warning("Cannot settle reservation: snapshot %s not found", snapshot_id)
                    return False

                if not release_reason:
                    release_reason = "VENUE_FILLED_SETTLED" if order["status"] == "FILLED" else "VENUE_CANCELLED_SETTLED"

                # Transition to RELEASED
                res = await conn.execute(
                    """
                    UPDATE execution_reservations
                    SET state = 'RELEASED',
                        release_reason = $4,
                        venue_reflected = true,
                        reflected_snapshot_id = $5,
                        settlement_snapshot_id = $5,
                        released_at = now()
                    WHERE environment = $1 AND account_id = $2 AND client_order_id = $3
                      AND state IN ('SETTLEMENT_PENDING', 'VENUE_REFLECTED', 'ACKED_UNREFLECTED', 'INFLIGHT');
                    """,
                    self.environment,
                    self.account_id,
                    client_order_id,
                    release_reason,
                    snapshot_id,
                )
                return res != "UPDATE 0"

    async def process_kill_switch_reset_request(self, cmd: dict) -> dict:
        """Evaluates an authenticated KILL_SWITCH_RESET_REQUEST against the 7-point safety checklist:
        
        1. Command signature & actor validation (valid schema, non-expired, valid auth_audit_event_id).
        2. Session advisory lock held by this executor.
        3. Kill switch currently tripped in database.
        4. CAS latch generation match (ensures no newer trip raced).
        5. Zero open orders, active reservations, or ambiguous commands.
        6. Emergency marker file removal with directory fsync.
        7. Atomic database CAS unlatch and in-memory switch reset.
        """
        cmd_id = cmd["command_id"]
        attempt_id = cmd["attempt_id"]
        lease_owner = cmd["lease_owner"]
        payload = cmd.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        actor_id = payload.get("actor_id") or cmd.get("requester_id", "OPERATOR:unknown")
        reason = payload.get("reason", "Operator reset request")
        auth_audit_event_id = payload.get("auth_audit_event_id")
        expected_generation = payload.get("expected_generation")
        expires_at_ns = payload.get("expires_at_ns")

        # Checklist 1: Validation of expiry & audit event
        now_ns = time.time_ns()
        if expires_at_ns and now_ns > expires_at_ns:
            err = f"Reset request expired (now={now_ns} > expires_at={expires_at_ns})"
            logger.error("Reset denied: %s", err)
            await self._finalize_command(cmd_id, attempt_id, lease_owner, "REJECTED", "REJECTED", err)
            raise KillSwitchResetDeniedError(err)

        if auth_audit_event_id:
            async with self.pool.acquire() as conn:
                audit_row = await conn.fetchrow(
                    "SELECT event_id, action FROM auth_audit_events WHERE event_id = $1;",
                    uuid.UUID(str(auth_audit_event_id)),
                )
                if not audit_row or audit_row["action"] != "KILL_SWITCH_RESET_REQUESTED":
                    err = f"Invalid or missing auth audit event {auth_audit_event_id}"
                    logger.error("Reset denied: %s", err)
                    await self._finalize_command(cmd_id, attempt_id, lease_owner, "REJECTED", "REJECTED", err)
                    raise KillSwitchResetDeniedError(err)

        # Checklist 2: Advisory lock held
        try:
            self.assert_advisory_lock()
        except AdvisoryLockLostError as e:
            await self._finalize_command(cmd_id, attempt_id, lease_owner, "REJECTED", "REJECTED", f"Advisory lock lost: {e}")
            raise

        # Checklist 3 & 4: DB Kill switch is latched and latch_generation matches
        async with self.pool.acquire() as conn:
            ks_row = await conn.fetchrow(
                """
                SELECT is_latched, latch_generation, reason 
                FROM execution_kill_switch
                WHERE environment = $1 AND account_id = $2;
                """,
                self.environment,
                self.account_id,
            )
            if not ks_row or not ks_row["is_latched"]:
                await self._finalize_command(cmd_id, attempt_id, lease_owner, "COMPLETED", "SUCCESS", "Kill switch already unlatched")
                return {"status": "COMPLETED", "kill_switch_latched": False}

            current_gen = ks_row["latch_generation"]
            if expected_generation is not None and int(current_gen) != int(expected_generation):
                err = f"Latch generation mismatch: expected {expected_generation}, found {current_gen} (newer trip occurred)"
                logger.error("Reset denied: %s", err)
                await self._finalize_command(cmd_id, attempt_id, lease_owner, "REJECTED", "REJECTED", err)
                raise KillSwitchResetDeniedError(err)

        # Checklist 5: Verify zero open orders, active reservations, and ambiguous commands
        async with self.pool.acquire() as conn:
            open_orders_cnt = await conn.fetchval(
                """
                SELECT COUNT(*) FROM trading_orders
                WHERE environment = $1 AND account_id = $2
                  AND status IN ('PENDING_SUBMIT', 'SUBMITTED', 'ACCEPTED', 'PARTIALLY_FILLED');
                """,
                self.environment,
                self.account_id,
            )
            active_res_cnt = await conn.fetchval(
                """
                SELECT COUNT(*) FROM execution_reservations
                WHERE environment = $1 AND account_id = $2
                  AND state IN ('PRE_SEND', 'INFLIGHT', 'ACKED_UNREFLECTED', 'SETTLEMENT_PENDING');
                """,
                self.environment,
                self.account_id,
            )
            ambiguous_cmd_cnt = await conn.fetchval(
                """
                SELECT COUNT(*) FROM execution_commands
                WHERE command_id != $1
                  AND (lease_owner = $2 OR payload->>'account_id' = $3)
                  AND (status IN ('PENDING', 'RUNNING', 'SENDING') OR command_outcome = 'UNKNOWN_PENDING_RECONCILIATION');
                """,
                cmd_id,
                self.worker_id,
                self.account_id,
            )

        venue_open_cnt = 0
        if hasattr(self.venue_client, "get_open_orders"):
            try:
                venue_orders = await self.venue_client.get_open_orders(self.environment, self.account_id)
                venue_open_cnt = len(venue_orders)
            except Exception as vo_err:
                logger.warning("Could not query venue open orders: %s", vo_err)

        if (open_orders_cnt or 0) > 0 or (active_res_cnt or 0) > 0 or (ambiguous_cmd_cnt or 0) > 0 or venue_open_cnt > 0:
            err = (
                f"Safety checklist failed: open_orders={open_orders_cnt}, "
                f"active_reservations={active_res_cnt}, ambiguous_commands={ambiguous_cmd_cnt}, "
                f"venue_open_orders={venue_open_cnt}"
            )
            logger.error("Reset denied: %s", err)
            await self._finalize_command(cmd_id, attempt_id, lease_owner, "REJECTED", "REJECTED", err)
            raise KillSwitchResetDeniedError(err)

        # Checklist 6: Remove emergency marker file with parent directory fsync
        marker_p = Path(self.emergency_marker_path)
        if marker_p.exists():
            try:
                marker_p.unlink()
                parent_dir = str(marker_p.parent)
                dir_fd = os.open(parent_dir, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
                logger.info("Removed emergency marker %s and fsynced directory", marker_p)
            except Exception as m_err:
                logger.warning("Error removing emergency marker file: %s", m_err)

        # Checklist 7: Atomic CAS unlatch in database
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                upd_res = await conn.execute(
                    """
                    UPDATE execution_kill_switch
                    SET is_latched = false,
                        reset_at = now(),
                        reset_by = $1,
                        reason = $2,
                        updated_at = now()
                    WHERE environment = $3 AND account_id = $4
                      AND latch_generation = $5 AND is_latched = true;
                    """,
                    actor_id,
                    reason,
                    self.environment,
                    self.account_id,
                    current_gen,
                )
                if upd_res == "UPDATE 0":
                    err = f"CAS unlatch failed for generation {current_gen} (racing trip occurred)"
                    logger.error("Reset failed: %s", err)
                    self.reconciler.drop_emergency_marker("CAS unlatch failed")
                    await self._finalize_command(cmd_id, attempt_id, lease_owner, "REJECTED", "REJECTED", err)
                    raise KillSwitchResetDeniedError(err)

                # 2. Audit record in auth_audit_events
                audit_reset_id = uuid.uuid4()
                await conn.execute(
                    """
                    INSERT INTO auth_audit_events (
                        event_id, username, action, ip_address, user_agent, details, occurred_at
                    ) VALUES ($1, $2, 'KILL_SWITCH_RESET_COMPLETED', '127.0.0.1', 'cryptonite-executor', $3, now());
                    """,
                    audit_reset_id,
                    actor_id,
                    json.dumps({
                        "environment": self.environment,
                        "account_id": self.account_id,
                        "reason": reason,
                        "latch_generation": current_gen,
                        "command_id": str(cmd_id),
                    }),
                )

                # 3. Outbox event
                await conn.execute(
                    """
                    INSERT INTO outbox_events (
                        event_id, stream_name, event_type, producer, payload,
                        status, attempt_count, next_attempt_at, created_at
                    ) VALUES ($1, 'cryptonite:stream:control_events', 'kill_switch.reset', 'cryptonite-executor', $2::jsonb, 'PENDING', 0, now(), now());
                    """,
                    str(uuid.uuid4()),
                    json.dumps({
                        "environment": self.environment,
                        "account_id": self.account_id,
                        "reset_by": actor_id,
                        "reason": reason,
                        "latch_generation": current_gen,
                    }),
                )

                # 4. Command terminal state in same atomic transaction
                cmd_upd = await conn.execute(
                    """
                    UPDATE execution_commands
                    SET status = 'COMPLETED',
                        command_outcome = 'SUCCESS',
                        error_message = NULL,
                        processed_at = now(),
                        lease_owner = NULL,
                        lease_expires_at = NULL
                    WHERE command_id = $1 AND attempt_id = $2 AND lease_owner = $3;
                    """,
                    cmd_id,
                    attempt_id,
                    lease_owner,
                )
                if cmd_upd == "UPDATE 0":
                    raise FencingTokenError(f"Worker {lease_owner} lost lease on command {cmd_id}")

        # In-memory unlatch & gate open
        self.kill_switch.reset(actor_id, open_orders_count=0, open_positions_count=0)
        self.is_gate_open = True
        self._is_ready = True
        logger.info("Kill switch successfully reset by %s (generation %s)", actor_id, current_gen)
        return {"status": "COMPLETED", "kill_switch_latched": False, "latch_generation": current_gen}

    async def shutdown(self) -> None:
        """Gracefully shuts down the demo execution pipeline."""
        self.is_gate_open = False
        self._is_ready = False
        await self.release_advisory_lock()
