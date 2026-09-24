"""Cross-thread EventBridge, fail-closed supervisor, and async projection worker."""

import asyncio
import json
import logging
import queue
import threading
import time
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
from cryptonite_schemas.identifiers import parse_instrument_id

from cryptonite_execution.kill_switch import LatchedKillSwitch

logger = logging.getLogger("cryptonite.execution.bridge")


class EventBridge:
    """Thread-safe bounded channel bridging Nautilus node callbacks to the async DB committer.
    
    Guarantees:
    - Zero DB I/O inside the node callback thread.
    - 80% capacity watermark transitions executor to DEGRADED.
    - 100% capacity hard cap: trips kill switch, sets atomic INTEGRITY_FAILED flag,
      captures memory incident snapshot, and signals IntegritySupervisor.
    """

    def __init__(
        self,
        kill_switch: LatchedKillSwitch,
        capacity: int = 5000,
        degraded_watermark_pct: float = 0.80,
    ) -> None:
        self.capacity = capacity
        self.degraded_watermark = int(capacity * degraded_watermark_pct)
        self.kill_switch = kill_switch

        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=capacity)
        self.integrity_failed_event = threading.Event()
        self.supervisor_signal = threading.Event()
        
        self.is_degraded: bool = False
        self.incident_snapshot: dict[str, Any] | None = None
        self.dropped_events_count: int = 0
        self.total_enqueued: int = 0

    def enqueue_event(self, event: dict[str, Any]) -> bool:
        """Invoked directly from node thread callbacks (zero DB I/O, fast execution)."""
        try:
            self.queue.put_nowait(event)
            self.total_enqueued += 1
            current_size = self.queue.qsize()

            # Check 80% watermark
            if current_size >= self.degraded_watermark and not self.is_degraded:
                self.is_degraded = True
                logger.warning(
                    f"EventBridge reached 80% watermark ({current_size}/{self.capacity}). Status: DEGRADED."
                )

            return True
        except queue.Full:
            # 100% Capacity Overflow: FAIL CLOSED IMMEDIATELY
            current_size = self.queue.qsize()
            self.dropped_events_count += 1
            self.integrity_failed_event.set()
            self.kill_switch.trip(latched_by="EVENT_BRIDGE_OVERFLOW", reason="Bridge queue 100% full")
            
            if self.incident_snapshot is None:
                self.incident_snapshot = {
                    "reason": "QUEUE_OVERFLOW_FAIL_CLOSED",
                    "queue_size": current_size,
                    "dropped_event": event,
                    "occurred_at_ns": time.time_ns(),
                }
            # Signal supervisor outside the callback thread
            self.supervisor_signal.set()
            logger.critical(
                f"EventBridge queue FULL ({current_size}/{self.capacity}). Failed closed, tripped kill latch!"
            )
            return False


class IntegritySupervisor:
    """Asynchronous background supervisor that persists integrity incident records to Postgres."""

    def __init__(
        self,
        bridge: EventBridge,
        pg_pool: asyncpg.Pool | None,
        engine_run_id: str,
    ) -> None:
        self.bridge = bridge
        self.pool = pg_pool
        self.engine_run_id = engine_run_id
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        while self._running:
            try:
                # Check supervisor signal without busy spinning
                if self.bridge.supervisor_signal.is_set():
                    await self._record_incident()
                    # Keep waiting until restart
                    break
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in IntegritySupervisor loop: %s", e)
                await asyncio.sleep(1.0)

    async def _record_incident(self) -> None:
        snapshot = self.bridge.incident_snapshot or {}
        logger.critical("IntegritySupervisor recording critical incident to database: %s", snapshot)
        if not self.pool:
            return
        query = """
        INSERT INTO integrity_incidents (
            incident_id, engine_run_id, reason, last_durable_journal_id,
            queue_size, incident_payload, occurred_at
        ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, now());
        """
        try:
            await self.pool.execute(
                query,
                uuid.uuid4(),
                self.engine_run_id,
                snapshot.get("reason", "QUEUE_OVERFLOW"),
                None,
                snapshot.get("queue_size", self.bridge.capacity),
                json.dumps(snapshot, default=str),
            )
            logger.info("Integrity incident successfully persisted to integrity_incidents table.")
        except Exception as e:
            logger.exception("Failed to persist integrity incident: %s", e)


class EventBridgeWorker:
    """Async worker that consumes micro-batches from EventBridge and commits them atomically to PostgreSQL."""

    def __init__(
        self,
        bridge: EventBridge,
        pg_pool: asyncpg.Pool,
        engine_run_id: str,
        batch_size: int = 50,
        drain_interval_s: float = 0.05,
    ) -> None:
        self.bridge = bridge
        self.pool = pg_pool
        self.engine_run_id = engine_run_id
        self.batch_size = batch_size
        self.drain_interval_s = drain_interval_s
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self.committed_events_count = 0

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def drain_and_commit_batch(self) -> int:
        """Drain up to batch_size events and commit in a single DB transaction."""
        batch: list[dict[str, Any]] = []
        for _ in range(self.batch_size):
            try:
                evt = self.bridge.queue.get_nowait()
                batch.append(evt)
            except queue.Empty:
                break

        if not batch:
            return 0

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for evt in batch:
                    # 1. Insert into paper_execution_events
                    # Invariants: UNIQUE (client_order_id, event_version), UNIQUE (engine_run_id, native_event_id)
                    event_id = evt.get("event_id", uuid.uuid4())
                    native_id = evt.get("native_event_id", str(uuid.uuid4()))
                    client_order_id = evt.get("client_order_id", "UNKNOWN")
                    event_version = evt.get("event_version", 1)
                    idempotency_key = f"{self.engine_run_id}:{native_id}"

                    await conn.execute(
                        """
                        INSERT INTO paper_execution_events (
                            engine_run_id, native_event_id, idempotency_key, event_id,
                            client_order_id, venue_order_id, event_type, event_version,
                            payload, occurred_at_ns, created_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, now())
                        ON CONFLICT (idempotency_key) DO NOTHING;
                        """,
                        self.engine_run_id,
                        native_id,
                        idempotency_key,
                        event_id,
                        client_order_id,
                        evt.get("venue_order_id"),
                        evt.get("event_type", "UNKNOWN"),
                        event_version,
                        json.dumps(evt.get("payload", {}), default=str),
                        evt.get("occurred_at_ns", time.time_ns()),
                    )

                    # 2. Update paper_orders if event_version > last_event_version
                    if evt.get("event_type") in ("ORDER_ACCEPTED", "ORDER_FILLED", "ORDER_CANCELED", "ORDER_REJECTED"):
                        await conn.execute(
                            """
                            UPDATE paper_orders
                            SET status = $2,
                                venue_order_id = COALESCE($3, venue_order_id),
                                filled_qty = COALESCE($4, filled_qty),
                                avg_fill_price = COALESCE($5, avg_fill_price),
                                last_event_version = $6,
                                updated_at = now()
                            WHERE client_order_id = $1
                              AND last_event_version < $6;
                            """,
                            client_order_id,
                            evt.get("status", evt.get("event_type")),
                            evt.get("venue_order_id"),
                            Decimal(str(evt.get("filled_qty", 0))),
                            Decimal(str(evt.get("avg_fill_price", 0))) if evt.get("avg_fill_price") else None,
                            event_version,
                        )

                    # 3. If fill, record trade, position, and account settlement
                    if evt.get("event_type") == "ORDER_FILLED":
                        trade_id = f"TRADE-{uuid.uuid4().hex[:12].upper()}"
                        trade_qty = Decimal(str(evt["quantity"]))
                        trade_px = Decimal(str(evt["price"]))
                        trade_comm = Decimal(str(evt.get("commission", 0)))
                        side = str(evt["side"]).upper()
                        account_id = str(evt["account_id"])
                        instance_id = str(evt["instance_id"])
                        instrument_id = str(evt["instrument_id"])

                        # 3a. Record trade
                        await conn.execute(
                            """
                            INSERT INTO paper_trades (
                                trade_id, account_id, client_order_id, instance_id,
                                instrument_id, side, price, quantity, commission, executed_at
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now());
                            """,
                            trade_id,
                            account_id,
                            client_order_id,
                            instance_id,
                            instrument_id,
                            side,
                            trade_px,
                            trade_qty,
                            trade_comm,
                        )

                        if parse_instrument_id(instrument_id)[2]:
                            cash_delta, realized_delta = await self._persist_perp_fill(
                                conn, evt, account_id, instance_id, instrument_id, trade_px, trade_comm
                            )
                        else:
                            # 3b. Position state machine: fetch current open position
                            pos_row = await conn.fetchrow(
                                """
                                SELECT position_id, entry_price, quantity, realized_pnl
                                FROM paper_positions
                                WHERE account_id = $1 AND instance_id = $2 AND instrument_id = $3 AND status = 'OPEN'
                                FOR UPDATE;
                                """,
                                account_id, instance_id, instrument_id
                            )

                            slice_pnl = Decimal("0")
                            if side == "BUY":
                                if not pos_row:
                                    # Open new position
                                    await conn.execute(
                                        """
                                        INSERT INTO paper_positions (
                                            position_id, account_id, instance_id, instrument_id, side,
                                            entry_price, quantity, current_price, unrealized_pnl, realized_pnl,
                                            status, opened_at
                                        ) VALUES ($1, $2, $3, $4, 'LONG', $5, $6, $5, 0, 0, 'OPEN', now());
                                        """,
                                        str(uuid.uuid4()), account_id, instance_id, instrument_id, trade_px, trade_qty
                                    )
                                else:
                                    old_qty = Decimal(str(pos_row["quantity"]))
                                    old_entry_px = Decimal(str(pos_row["entry_price"]))
                                    new_qty = old_qty + trade_qty
                                    new_entry_px = ((old_qty * old_entry_px) + (trade_qty * trade_px)) / new_qty
                                    await conn.execute(
                                        """
                                        UPDATE paper_positions
                                        SET quantity = $2, entry_price = $3, current_price = $4
                                        WHERE position_id = $1;
                                        """,
                                        pos_row["position_id"], new_qty, new_entry_px, trade_px
                                    )
                            elif side == "SELL":
                                if pos_row:
                                    old_qty = Decimal(str(pos_row["quantity"]))
                                    old_entry_px = Decimal(str(pos_row["entry_price"]))
                                    fill_qty = min(trade_qty, old_qty)
                                    slice_pnl = (trade_px - old_entry_px) * fill_qty - trade_comm
                                    new_qty = old_qty - fill_qty
                                    if new_qty == Decimal("0"):
                                        # Closed position
                                        await conn.execute(
                                            """
                                            UPDATE paper_positions
                                            SET quantity = 0, side = 'FLAT', status = 'CLOSED', closed_at = now(),
                                                realized_pnl = realized_pnl + $2, current_price = $3
                                            WHERE position_id = $1;
                                            """,
                                            pos_row["position_id"], slice_pnl, trade_px
                                        )
                                    else:
                                        await conn.execute(
                                            """
                                            UPDATE paper_positions
                                            SET quantity = $2, realized_pnl = realized_pnl + $3, current_price = $4
                                            WHERE position_id = $1;
                                            """,
                                            pos_row["position_id"], new_qty, slice_pnl, trade_px
                                        )

                            # 3c. Double-entry settlement on paper_accounts
                            if side == "BUY":
                                cash_delta = -(trade_qty * trade_px + trade_comm)
                                realized_delta = Decimal("0")
                            else:
                                cash_delta = +(trade_qty * trade_px - trade_comm)
                                realized_delta = slice_pnl

                        await conn.execute(
                            """
                            UPDATE paper_accounts
                            SET cash_balance = cash_balance + $2,
                                realized_pnl = realized_pnl + $3,
                                daily_realized_pnl = daily_realized_pnl + $3,
                                updated_at = now()
                            WHERE account_id = $1;
                            """,
                            account_id, cash_delta, realized_delta
                        )

                        # Recalculate total_equity = cash_balance + spot value + perp unrealized PnL
                        equity_row = await conn.fetchrow(
                            """
                            SELECT COALESCE(SUM(CASE
                                WHEN instrument_id LIKE '%-PERP.%'
                                THEN (CASE WHEN side = 'SHORT' THEN -1 ELSE 1 END) * quantity * (current_price - entry_price)
                                ELSE quantity * current_price
                            END), 0) as pos_val
                            FROM paper_positions
                            WHERE account_id = $1 AND status = 'OPEN';
                            """,
                            account_id
                        )
                        open_val = Decimal(str(equity_row["pos_val"])) if equity_row else Decimal("0")
                        await conn.execute(
                            """
                            UPDATE paper_accounts
                            SET total_equity = cash_balance + $2,
                                updated_at = now()
                            WHERE account_id = $1;
                            """,
                            account_id, open_val
                        )

                        # Emit outbox event for trade execution
                        await conn.execute(
                            """
                            INSERT INTO outbox_events (
                                event_id, stream_name, event_type, producer, payload,
                                status, attempt_count, next_attempt_at, created_at
                            ) VALUES ($1, $2, $3, $4, $5::jsonb, 'PENDING', 0, now(), now());
                            """,
                            str(uuid.uuid4()),
                            "cryptonite:stream:paper_trades",
                            "paper_trade_executed",
                            "cryptonite-executor",
                            json.dumps({
                                "trade_id": trade_id,
                                "account_id": evt["account_id"],
                                "client_order_id": client_order_id,
                                "instrument_id": evt["instrument_id"],
                                "side": evt["side"],
                                "price": str(evt["price"]),
                                "quantity": str(evt["quantity"]),
                            }),
                        )

        self.committed_events_count += len(batch)
        return len(batch)

    async def _persist_perp_fill(
        self,
        conn: asyncpg.Connection,
        evt: dict[str, Any],
        account_id: str,
        instance_id: str,
        instrument_id: str,
        fill_px: Decimal,
        commission: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Write the dispatcher's post-fill perp position snapshot to paper_positions.

        A close (size 0) or a flip closes the open row (FLAT/CLOSED, realized PnL attached);
        a flip then opens a new row on the other side. Returns (cash_delta, realized_delta):
        perp cash moves only by realized PnL and fees, never notional.
        """
        gross = evt.get("realized_pnl")
        closed_realized = Decimal(str(gross)) - commission if gross is not None else Decimal("0")
        cash_delta = (Decimal(str(gross)) if gross is not None else Decimal("0")) - commission

        snap = evt.get("position")
        if snap is None:
            logger.error("Perp fill %s has no position snapshot; paper_positions not updated", evt.get("client_order_id"))
            return cash_delta, cash_delta

        signed_qty = Decimal(str(snap["signed_qty"]))
        new_side = "LONG" if signed_qty > 0 else "SHORT" if signed_qty < 0 else "FLAT"
        pos_row = await conn.fetchrow(
            """
            SELECT position_id, side FROM paper_positions
            WHERE account_id = $1 AND instance_id = $2 AND instrument_id = $3 AND status = 'OPEN'
            FOR UPDATE;
            """,
            account_id, instance_id, instrument_id,
        )
        pending_realized = closed_realized  # net of this fill's fee; attached to the row it closes

        if pos_row and (signed_qty == 0 or pos_row["side"] != new_side):
            await conn.execute(
                """
                UPDATE paper_positions
                SET quantity = 0, side = 'FLAT', status = 'CLOSED', closed_at = now(), current_price = $2,
                    unrealized_pnl = 0, realized_pnl = realized_pnl + $3, isolated_margin = 0,
                    liquidation_price = NULL, maintenance_margin = NULL
                WHERE position_id = $1;
                """,
                pos_row["position_id"], fill_px, pending_realized,
            )
            pending_realized, pos_row = Decimal("0"), None

        if signed_qty != 0:
            qty = abs(signed_qty)
            entry = Decimal(str(snap["entry_price"]))
            unrealized = (Decimal("1") if signed_qty > 0 else Decimal("-1")) * qty * (fill_px - entry)
            liq = Decimal(str(snap["liquidation_price"])) if snap.get("liquidation_price") is not None else None
            mm = Decimal(str(snap["maintenance_margin"])) if snap.get("maintenance_margin") is not None else None
            fields = (
                new_side, entry, qty, fill_px, unrealized, pending_realized, int(snap["leverage"]),
                str(snap["margin_mode"]), Decimal(str(snap["isolated_margin"])), liq, mm,
            )
            if pos_row:
                await conn.execute(
                    """
                    UPDATE paper_positions
                    SET side = $2, entry_price = $3, quantity = $4, current_price = $5, unrealized_pnl = $6,
                        realized_pnl = realized_pnl + $7, leverage = $8, margin_mode = $9,
                        isolated_margin = $10, liquidation_price = $11, maintenance_margin = $12
                    WHERE position_id = $1;
                    """,
                    pos_row["position_id"], *fields,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO paper_positions (
                        position_id, account_id, instance_id, instrument_id, side, entry_price, quantity,
                        current_price, unrealized_pnl, realized_pnl, leverage, margin_mode, isolated_margin,
                        liquidation_price, maintenance_margin, status, opened_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, 'OPEN', now());
                    """,
                    str(uuid.uuid4()), account_id, instance_id, instrument_id, *fields,
                )
        elif pending_realized:
            logger.warning("Perp close %s had no open paper_positions row to carry realized PnL", instrument_id)

        return cash_delta, cash_delta

    async def _run_loop(self) -> None:
        while self._running:
            try:
                drained = await self.drain_and_commit_batch()
                if drained == 0:
                    await asyncio.sleep(self.drain_interval_s)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in EventBridgeWorker batch commit: %s", e)
                await asyncio.sleep(0.5)


class CoalescedMarkToMarketProjector:
    """Updates paper_positions and paper_accounts mark-to-market valuation throttled to 250ms."""

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        get_latest_price_fn: Any,
        interval_s: float = 0.25,
    ) -> None:
        self.pool = pg_pool
        self.get_latest_price_fn = get_latest_price_fn
        self.interval_s = interval_s
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def project_once(self) -> None:
        """Update open positions mark-to-market."""
        async with self.pool.acquire() as conn:
            positions = await conn.fetch(
                "SELECT position_id, instrument_id, side, entry_price, quantity FROM paper_positions WHERE status = 'OPEN';"
            )
            for pos in positions:
                cur_price = self.get_latest_price_fn(pos["instrument_id"])
                if cur_price is not None and cur_price > 0:
                    direction = -1 if pos.get("side") == "SHORT" else 1
                    unrealized_pnl = direction * (cur_price - pos["entry_price"]) * pos["quantity"]
                    await conn.execute(
                        """
                        UPDATE paper_positions
                        SET current_price = $2,
                            unrealized_pnl = $3
                        WHERE position_id = $1;
                        """,
                        pos["position_id"],
                        cur_price,
                        unrealized_pnl,
                    )

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.project_once()
                await asyncio.sleep(self.interval_s)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Error in mark-to-market projection: %s", e)
                await asyncio.sleep(1.0)
