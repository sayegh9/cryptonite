"""Durable multi-asset execution reservation manager.

Implements PostgreSQL serializable transaction isolation with bounded jitter retry
on SQLSTATE 40001 (serialization failure), row-level consistency, and the race-free
available-balance invariant:
available_balance(asset) = free(asset) - sum(amount + fee_slippage_headroom)
for reservations in ('PRE_SEND', 'INFLIGHT', 'ACKED_UNREFLECTED').
"""

import asyncio
import logging
import random
import uuid
from decimal import Decimal
from typing import Any, Optional

import asyncpg

logger = logging.getLogger("cryptonite.execution.reservations")


class InsufficientAvailableBalanceError(Exception):
    """Raised when available balance (free - active reservations) is insufficient."""


class ReservationNotFoundError(Exception):
    """Raised when a specified reservation ID does not exist."""


class ReservationStateError(Exception):
    """Raised when a reservation state transition is invalid."""


class DurableReservationManager:
    """Manages durable execution reservations against PostgreSQL."""

    def __init__(self, pool: asyncpg.Pool, max_retries: int = 5, base_backoff_ms: float = 10.0):
        self._pool = pool
        self._max_retries = max_retries
        self._base_backoff_ms = base_backoff_ms

    async def _execute_with_retry(self, operation):
        """Execute a coroutine within serializable isolation, retrying on SQLSTATE 40001."""
        for attempt in range(1, self._max_retries + 1):
            async with self._pool.acquire() as conn:
                try:
                    async with conn.transaction(isolation="serializable"):
                        return await operation(conn)
                except asyncpg.SerializationError as e:
                    if attempt == self._max_retries:
                        logger.error(f"Serialization failure after {attempt} attempts: {e}")
                        raise
                    # Exponential backoff with random jitter (±50%)
                    backoff = (self._base_backoff_ms * (2 ** (attempt - 1))) / 1000.0
                    jitter = backoff * random.uniform(0.5, 1.5)
                    logger.warning(
                        f"Serialization failure (SQLSTATE 40001) on attempt {attempt}/{self._max_retries}. "
                        f"Retrying in {jitter:.4f}s..."
                    )
                    await asyncio.sleep(jitter)

    async def get_available_balance(
        self, environment: str, account_id: str, asset: str
    ) -> Decimal:
        """Compute available balance = free - sum(amount + headroom) of active reservations."""
        async def _query(conn: asyncpg.Connection) -> Decimal:
            bal_row = await conn.fetchrow(
                """
                SELECT free FROM trading_balances 
                WHERE environment = $1 AND account_id = $2 AND asset = $3;
                """,
                environment,
                account_id,
                asset,
            )
            free = Decimal(str(bal_row["free"])) if bal_row else Decimal("0")

            res_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(amount + fee_slippage_headroom), 0) AS reserved
                FROM execution_reservations
                WHERE environment = $1 AND account_id = $2 AND asset = $3
                  AND state IN ('PRE_SEND', 'INFLIGHT', 'ACKED_UNREFLECTED');
                """,
                environment,
                account_id,
                asset,
            )
            reserved = Decimal(str(res_row["reserved"])) if res_row else Decimal("0")
            return max(Decimal("0"), free - reserved)

        return await self._execute_with_retry(_query)

    async def acquire_reservation(
        self,
        environment: str,
        account_id: str,
        command_id: uuid.UUID,
        client_order_id: str,
        asset: str,
        amount: Decimal,
        fee_slippage_headroom: Decimal = Decimal("0"),
        strategy_id: str = "manual",
        instrument_id: str = "BTCUSDT.BINANCE",
        side: str = "BUY",
        order_type: str = "LIMIT",
        price: Optional[Decimal] = None,
        quantity: Optional[Decimal] = None,
    ) -> uuid.UUID:
        """Atomically check available balance and persist PRE_SEND reservation.
        
        Guarantees trading_orders record exists and creates reservation in state PRE_SEND
        with venue_reflected=False.
        """
        async def _tx(conn: asyncpg.Connection) -> uuid.UUID:
            # 1. Fetch free balance with row lock
            bal_row = await conn.fetchrow(
                """
                SELECT free FROM trading_balances 
                WHERE environment = $1 AND account_id = $2 AND asset = $3
                FOR UPDATE;
                """,
                environment,
                account_id,
                asset,
            )
            free = Decimal(str(bal_row["free"])) if bal_row else Decimal("0")

            # 2. Sum active reservations
            res_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(amount + fee_slippage_headroom), 0) AS reserved
                FROM execution_reservations
                WHERE environment = $1 AND account_id = $2 AND asset = $3
                  AND state IN ('PRE_SEND', 'INFLIGHT', 'ACKED_UNREFLECTED');
                """,
                environment,
                account_id,
                asset,
            )
            reserved = Decimal(str(res_row["reserved"])) if res_row else Decimal("0")
            available = free - reserved

            required = amount + fee_slippage_headroom
            if available < required:
                raise InsufficientAvailableBalanceError(
                    f"Insufficient available {asset}: available {available} < required {required} "
                    f"(free={free}, active_reservations={reserved})"
                )

            # 3. Ensure order exists in trading_orders
            order_qty = quantity if quantity is not None else amount
            await conn.execute(
                """
                INSERT INTO trading_orders (
                    environment, account_id, client_order_id, strategy_id, instrument_id,
                    side, order_type, quantity, price, status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'SUBMITTED')
                ON CONFLICT (environment, account_id, client_order_id) DO NOTHING;
                """,
                environment,
                account_id,
                client_order_id,
                strategy_id,
                instrument_id,
                side,
                order_type,
                order_qty,
                price,
            )

            # 4. Insert execution_reservation
            res_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO execution_reservations (
                    reservation_id, environment, account_id, command_id, client_order_id,
                    asset, amount, fee_slippage_headroom, state, venue_reflected
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'PRE_SEND', false);
                """,
                res_id,
                environment,
                account_id,
                command_id,
                client_order_id,
                asset,
                amount,
                fee_slippage_headroom,
            )
            return res_id

        return await self._execute_with_retry(_tx)

    async def mark_inflight(self, reservation_id: uuid.UUID) -> None:
        """Transition reservation PRE_SEND -> INFLIGHT."""
        async def _tx(conn: asyncpg.Connection) -> None:
            res = await conn.execute(
                """
                UPDATE execution_reservations
                SET state = 'INFLIGHT'
                WHERE reservation_id = $1 AND state = 'PRE_SEND';
                """,
                reservation_id,
            )
            if res == "UPDATE 0":
                row = await conn.fetchrow(
                    "SELECT state FROM execution_reservations WHERE reservation_id = $1;",
                    reservation_id,
                )
                if not row:
                    raise ReservationNotFoundError(f"Reservation {reservation_id} not found")
                raise ReservationStateError(
                    f"Cannot mark INFLIGHT: current state is {row['state']}"
                )

        await self._execute_with_retry(_tx)

    async def mark_acked(self, reservation_id: uuid.UUID) -> None:
        """Transition reservation INFLIGHT -> ACKED_UNREFLECTED."""
        async def _tx(conn: asyncpg.Connection) -> None:
            res = await conn.execute(
                """
                UPDATE execution_reservations
                SET state = 'ACKED_UNREFLECTED'
                WHERE reservation_id = $1 AND state = 'INFLIGHT';
                """,
                reservation_id,
            )
            if res == "UPDATE 0":
                row = await conn.fetchrow(
                    "SELECT state FROM execution_reservations WHERE reservation_id = $1;",
                    reservation_id,
                )
                if not row:
                    raise ReservationNotFoundError(f"Reservation {reservation_id} not found")
                raise ReservationStateError(
                    f"Cannot mark ACKED_UNREFLECTED: current state is {row['state']}"
                )

        await self._execute_with_retry(_tx)

    async def confirm_venue_reflection(
        self, reservation_id: uuid.UUID, snapshot_id: uuid.UUID
    ) -> None:
        """Transition reservation ACKED_UNREFLECTED -> VENUE_REFLECTED with confirmed snapshot ID."""
        async def _tx(conn: asyncpg.Connection) -> None:
            res = await conn.execute(
                """
                UPDATE execution_reservations
                SET state = 'VENUE_REFLECTED',
                    venue_reflected = true,
                    reflected_snapshot_id = $2,
                    reconciled_at = now()
                WHERE reservation_id = $1 AND state = 'ACKED_UNREFLECTED';
                """,
                reservation_id,
                snapshot_id,
            )
            if res == "UPDATE 0":
                row = await conn.fetchrow(
                    "SELECT state FROM execution_reservations WHERE reservation_id = $1;",
                    reservation_id,
                )
                if not row:
                    raise ReservationNotFoundError(f"Reservation {reservation_id} not found")
                raise ReservationStateError(
                    f"Cannot confirm VENUE_REFLECTED: current state is {row['state']}"
                )

        await self._execute_with_retry(_tx)

    async def mark_settlement_pending(self, reservation_id: uuid.UUID) -> None:
        """Transition reservation to SETTLEMENT_PENDING upon fill."""
        async def _tx(conn: asyncpg.Connection) -> None:
            res = await conn.execute(
                """
                UPDATE execution_reservations
                SET state = 'SETTLEMENT_PENDING',
                    venue_reflected = true
                WHERE reservation_id = $1 AND state IN ('INFLIGHT', 'ACKED_UNREFLECTED', 'VENUE_REFLECTED');
                """,
                reservation_id,
            )
            if res == "UPDATE 0":
                row = await conn.fetchrow(
                    "SELECT state FROM execution_reservations WHERE reservation_id = $1;",
                    reservation_id,
                )
                if not row:
                    raise ReservationNotFoundError(f"Reservation {reservation_id} not found")
                raise ReservationStateError(
                    f"Cannot transition to SETTLEMENT_PENDING: current state is {row['state']}"
                )

        await self._execute_with_retry(_tx)

    async def release_reservation(
        self,
        reservation_id: uuid.UUID,
        release_reason: str,
        settlement_snapshot_id: Optional[uuid.UUID] = None,
        venue_reflected: Optional[bool] = None,
    ) -> None:
        """Transition reservation to terminal RELEASED state with full provenance.
        
        Valid reasons:
        - VENUE_FILLED_SETTLED: requires settlement_snapshot_id, venue_reflected=True
        - VENUE_CANCELLED_SETTLED: requires settlement_snapshot_id, venue_reflected=True
        - PRE_SEND_REJECTED: settlement_snapshot_id=None, venue_reflected=False
        - CANCELLED_UNSENT: settlement_snapshot_id=None, venue_reflected=False
        - OPERATOR_ABORTED_UNSENT: settlement_snapshot_id=None, venue_reflected=False
        """
        valid_reasons = {
            "VENUE_FILLED_SETTLED",
            "VENUE_CANCELLED_SETTLED",
            "PRE_SEND_REJECTED",
            "CANCELLED_UNSENT",
            "OPERATOR_ABORTED_UNSENT",
        }
        if release_reason not in valid_reasons:
            raise ValueError(f"Invalid reservation release_reason: {release_reason}. Must be one of {valid_reasons}")

        if release_reason in ("VENUE_FILLED_SETTLED", "VENUE_CANCELLED_SETTLED"):
            if settlement_snapshot_id is None:
                raise ValueError(f"release_reason {release_reason} requires a non-null settlement_snapshot_id")
            resolved_venue_reflected = True
        else:
            if settlement_snapshot_id is not None:
                raise ValueError(f"release_reason {release_reason} requires settlement_snapshot_id to be None")
            resolved_venue_reflected = False

        if venue_reflected is not None and venue_reflected != resolved_venue_reflected:
            raise ValueError(
                f"Conflicting venue_reflected={venue_reflected} for release_reason={release_reason}"
            )

        async def _tx(conn: asyncpg.Connection) -> None:
            row = await conn.fetchrow(
                "SELECT state, venue_reflected FROM execution_reservations WHERE reservation_id = $1;",
                reservation_id,
            )
            if not row:
                raise ReservationNotFoundError(f"Reservation {reservation_id} not found")

            # Prevent releasing acknowledged or venue-reflected reservation with unsent reasons
            if (row["venue_reflected"] or row["state"] in ("ACKED_UNREFLECTED", "VENUE_REFLECTED", "SETTLEMENT_PENDING")) and release_reason in ("PRE_SEND_REJECTED", "CANCELLED_UNSENT", "OPERATOR_ABORTED_UNSENT"):
                raise ReservationStateError(
                    f"Cannot release reservation in state {row['state']} (venue_reflected={row['venue_reflected']}) with reason {release_reason}. Must settle via venue cancellation or fill."
                )

            res = await conn.execute(
                """
                UPDATE execution_reservations
                SET state = 'RELEASED',
                    release_reason = $2,
                    venue_reflected = $3,
                    settlement_snapshot_id = $4,
                    released_at = now()
                WHERE reservation_id = $1 AND state != 'RELEASED';
                """,
                reservation_id,
                release_reason,
                resolved_venue_reflected,
                settlement_snapshot_id,
            )
            if res == "UPDATE 0":
                curr = await conn.fetchval(
                    "SELECT state FROM execution_reservations WHERE reservation_id = $1;", reservation_id
                )
                if curr == "RELEASED":
                    return
                raise ReservationStateError(f"Failed to transition reservation {reservation_id} to RELEASED")

        await self._execute_with_retry(_tx)
