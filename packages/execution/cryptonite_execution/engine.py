"""PaperTradingEngine: Single-LiveNode runtime with lossless watermark handoff and command mailbox."""

import asyncio
import json
import logging
import threading
import time
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
from cryptonite_schemas.profile import underlying_spot
from nautilus_trader.live import LiveNode
from nautilus_trader.model import (
    Bar,
    InstrumentId,
    TraderId,
)

from cryptonite_execution.bridge import EventBridge, IntegritySupervisor
from cryptonite_execution.derivatives import FundingPaymentEvent, LiquidationEvent
from cryptonite_execution.ingress import IngressHealthMonitor
from cryptonite_execution.kill_switch import LatchedKillSwitch
from cryptonite_execution.mailbox import NodeCommandMailbox
from cryptonite_execution.risk import GuardedOrderDispatcher
from cryptonite_execution.router import GateOwnedBarRouter
from cryptonite_execution.version_lock import assert_version_locked

logger = logging.getLogger("cryptonite.execution.engine")

LIQUIDATION_CHANNEL = "channel:perp_liquidations"
FUNDING_CHANNEL = "channel:perp_funding_events"


class PaperTradingEngine:
    """Orchestrates single-node paper trading execution with strict lifecycle guarantees."""

    def __init__(
        self,
        account_id: str,
        trader_id: str,
        engine_run_id: str,
        pg_pool: asyncpg.Pool,
        redis_client: Any,
        starting_cash: Decimal = Decimal("10000.0"),
    ) -> None:
        # Enforce Nautilus version lock contract
        assert_version_locked()

        self.account_id = account_id
        self.trader_id = TraderId(trader_id)
        self.engine_run_id = engine_run_id
        self.pool = pg_pool
        self.redis = redis_client
        self.starting_cash = starting_cash

        # Core components
        self.kill_switch = LatchedKillSwitch()
        self.mailbox = NodeCommandMailbox()
        self.router = GateOwnedBarRouter()
        self.ingress_health = IngressHealthMonitor()
        self.bridge = EventBridge(self.kill_switch)
        self.supervisor = IntegritySupervisor(self.bridge, self.pool, self.engine_run_id)

        self.dispatcher = GuardedOrderDispatcher(
            account_id=self.account_id,
            starting_cash=self.starting_cash,
            kill_switch=self.kill_switch,
            ingress_health=self.ingress_health,
        )

        # Lifecycle state
        self._live_gate_open: bool = False
        self._buffered_bars: list[Bar] = []
        self._buffered_bars_lock = threading.Lock()
        self._strategies: list[Any] = []
        self._node_thread: threading.Thread | None = None
        self._running = False
        self.node: LiveNode | None = None

    @property
    def is_gate_open(self) -> bool:
        return self._live_gate_open

    @property
    def is_ready(self) -> bool:
        """Readiness predicate for HTTP /ready endpoint."""
        if self.bridge.integrity_failed_event.is_set():
            return False
        if self.kill_switch.is_latched:
            return False
        return self._running and self._live_gate_open

    def register_strategy(self, strategy: Any) -> None:
        self._strategies.append(strategy)

    def on_live_bar(self, bar: Bar) -> None:
        """Invoked when a live bar arrives from Redis message bus."""
        if not self._live_gate_open:
            with self._buffered_bars_lock:
                self._buffered_bars.append(bar)
            return

        # Route through gate-owned deduplicating router
        self.router.route_bar(bar, self._strategies)

    async def execute_startup_handoff(
        self,
        symbol: str,
        bar_type_str: str,
        catalog_watermark_c_ns: int,
        historical_warmup_bars: list[Bar],
    ) -> bool:
        """8-step lossless post-subscription handoff protocol."""
        logger.info("Starting post-subscription handoff protocol for %s...", symbol)
        
        # 1. Gate closed
        self._live_gate_open = False

        # 2. Rewarm historical catalog bars up through watermark C with dispatcher disabled
        logger.info("Rewarming %d historical bars through watermark C=%d", len(historical_warmup_bars), catalog_watermark_c_ns)
        for b in historical_warmup_bars:
            if b.ts_event <= catalog_watermark_c_ns:
                self.router.route_bar(b, self._strategies)

        # 3. Discard buffered live bars <= C, process buffered live bars > C
        with self._buffered_bars_lock:
            buffered = list(self._buffered_bars)
            self._buffered_bars.clear()

        routed_buffered = 0
        for b in buffered:
            if b.ts_event > catalog_watermark_c_ns:
                if self.router.route_bar(b, self._strategies):
                    routed_buffered += 1

        logger.info("Routed %d buffered live bars past watermark C", routed_buffered)

        # 4. Checkpoint persistence: persist actual max_processed_bar_ns to DB
        max_routed_ns = max(self.router.max_processed_bar_ns, catalog_watermark_c_ns)
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE paper_strategy_instances
                    SET last_processed_bar_ns = $2,
                        updated_at = now()
                    WHERE account_id = $1;
                    """,
                    self.account_id,
                    max_routed_ns,
                )
            logger.info("Persisted high-water mark last_processed_bar_ns=%d", max_routed_ns)
        except Exception as e:
            logger.error("Failed to persist high-water mark to database: %s. Live gate remains CLOSED!", e)
            return False

        # 5. Assert dual freshness before opening gate
        # Wait up to 5 seconds for telemetry and cache quote
        inst_str = f"{symbol.upper()}.BINANCE"
        is_fresh = False
        for _ in range(50):
            node_cache = self.node.cache if self.node else self.dispatcher.node_cache
            if self.ingress_health.is_fresh(inst_str, node_cache):
                is_fresh = True
                break
            await asyncio.sleep(0.1)

        if not is_fresh:
            logger.warning("Ingress dual freshness check failed for %s. Gate remains CLOSED.", inst_str)
            return False

        # 6. Open gate
        self._live_gate_open = True
        logger.info("Live gate OPEN for %s. Trading enabled.", inst_str)
        return True

    async def process_liquidations(self, mark_prices: dict[str, Decimal]) -> list[LiquidationEvent]:
        """Run the dispatcher's liquidation check, persist each forced close, and notify the UI.

        Each liquidation becomes an ORDER_FILLED event (``liquidation: True``) on the EventBridge,
        which closes the paper_positions row, and is published on ``channel:perp_liquidations``.
        """
        liquidations = self.dispatcher.check_liquidations(mark_prices)
        for liq in liquidations:
            close_side = "SELL" if liq.side == "LONG" else "BUY"
            self.bridge.enqueue_event({
                "event_type": "ORDER_FILLED",
                "event_version": 1,
                "client_order_id": liq.client_order_id,
                "account_id": liq.account_id,
                "instance_id": liq.instance_id,
                "instrument_id": liq.instrument_id,
                "side": close_side,
                "price": str(liq.fill_price),
                "quantity": str(liq.quantity),
                "commission": str(liq.trading_fee + liq.clearance_fee),
                "filled_qty": str(liq.quantity),
                "avg_fill_price": str(liq.fill_price),
                "status": "FILLED",
                "occurred_at_ns": time.time_ns(),
                "liquidation": True,
                "shortfall_covered": str(liq.shortfall_covered),
                **self.dispatcher.fill_event_fields(liq.instrument_id, liq.realized_pnl, liq.fill_price),
            })
            if self.redis is not None:
                try:
                    await self.redis.publish(
                        LIQUIDATION_CHANNEL,
                        json.dumps({"type": "perp.liquidation", "v": 1, **liq.model_dump(mode="json")}),
                    )
                except Exception as e:  # noqa: BLE001 - notification must not block the close
                    logger.error("Failed to publish liquidation %s: %s", liq.client_order_id, e)
        return liquidations

    async def process_funding_settlement(
        self, instrument_id: str, mark_price: Decimal, funding_rate: Decimal, funding_time_ns: int
    ) -> tuple[list[FundingPaymentEvent], list[LiquidationEvent]]:
        """Apply a funding settlement, persist and publish it, then run a liquidation sweep.

        Each payment becomes a FUNDING_SETTLED event. Its journal idempotency key
        (account, instrument, funding time) makes the DB application exactly-once across restarts.
        """
        payments = self.dispatcher.apply_funding_settlement(instrument_id, mark_price, funding_rate, funding_time_ns)
        for p in payments:
            event_key = f"FUND-{p.instrument_id}-{p.funding_time_ns}"
            self.bridge.enqueue_event({
                "event_type": "FUNDING_SETTLED",
                "event_version": 1,
                "client_order_id": event_key,
                "native_event_id": event_key,
                "idempotency_key": f"FUNDING:{p.account_id}:{p.instrument_id}:{p.funding_time_ns}",
                "account_id": p.account_id,
                "instance_id": p.instance_id,
                "instrument_id": p.instrument_id,
                "cash_flow": str(p.cash_flow),
                "occurred_at_ns": p.funding_time_ns,
                "payload": p.model_dump(mode="json"),
                "position": self.dispatcher.perp_position_snapshot(p.instrument_id, p.mark_price),
            })
            if self.redis is not None:
                try:
                    await self.redis.publish(
                        FUNDING_CHANNEL, json.dumps({"type": "perp.funding", "v": 1, **p.model_dump(mode="json")})
                    )
                except Exception as e:  # noqa: BLE001 - notification must not block settlement
                    logger.error("Failed to publish funding payment for %s: %s", p.instrument_id, e)
        liquidations = await self.process_liquidations(self.dispatcher.fresh_mark_prices()) if payments else []
        return payments, liquidations

    def process_mailbox_commands(self) -> None:
        """Drained synchronously inside node worker thread."""
        commands = self.mailbox.drain()
        for cmd in commands:
            cmd_type = cmd.get("command_type")
            target_id = cmd.get("target_instance_id")
            logger.info("Processing mailbox command: %s for %s", cmd_type, target_id)

            if cmd_type == "PAUSE":
                # Cancels resting entry orders (spot BUYs; perp orders of either side unless
                # reduce-only), preserves open positions and their exit orders
                to_cancel = [
                    cid for cid, o in self.dispatcher.open_orders.items()
                    if self.dispatcher.is_entry_order(o) and (not target_id or o.get("instance_id") == target_id)
                ]
                for cid in to_cancel:
                    self.dispatcher.release_reservation(cid)
                logger.info("Paused strategy %s: cancelled %d resting entry orders", target_id, len(to_cancel))

            elif cmd_type == "STOP":
                # Cancels all open orders for strategy
                to_cancel = [
                    cid for cid, o in self.dispatcher.open_orders.items()
                    if not target_id or o.get("instance_id") == target_id
                ]
                for cid in to_cancel:
                    self.dispatcher.release_reservation(cid)
                logger.info("Stopped strategy %s: cancelled %d orders", target_id, len(to_cancel))

            elif cmd_type == "CLOSE_POSITION":
                # Cancels open orders and flattens: spot long via sell, perp via opposing reduce-only order
                to_cancel = list(self.dispatcher.open_orders.keys())
                for cid in to_cancel:
                    self.dispatcher.release_reservation(cid)
                payload = cmd.get("payload", {})
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                inst_id = cmd.get("instrument_id") or payload.get("instrument_id", "")
                pos_qty = self.dispatcher.get_position_qty(inst_id)
                if pos_qty > Decimal("0"):
                    price = payload.get("price") or cmd.get("price", Decimal("50000"))
                    logger.info("Flattening position: selling %s of %s at %s", pos_qty, inst_id, price)
                    self.dispatcher.handle_fill(
                        client_order_id="FLATTEN-EMERGENCY",
                        fill_qty=pos_qty,
                        fill_price=Decimal(str(price)),
                        commission=Decimal("0"),
                        side="SELL",
                        instrument_id=inst_id,
                    )
                elif self.dispatcher.get_perp_position_qty(inst_id) != 0:
                    price = payload.get("price") or cmd.get("price", Decimal("50000"))
                    closed = self.dispatcher.flatten_perp_position(
                        inst_id, Decimal(str(price)), Decimal("0"), "FLATTEN-EMERGENCY"
                    )
                    logger.info("Flattened perp position %s: reduce-only (side, qty, pnl)=%s at %s", inst_id, closed, price)
                elif self.dispatcher.get_margin_position_qty(inst_id) != 0:
                    price = payload.get("price") or cmd.get("price")
                    if price is None and self.dispatcher.node_cache:
                        account = self.dispatcher.margin_accounts[inst_id]
                        quote = self.dispatcher.node_cache.quote(InstrumentId.from_str(underlying_spot(inst_id)))
                        price = quote.bid_price if account.side == "LONG" else quote.ask_price
                    if price is not None:
                        closed = self.dispatcher.flatten_margin_position(
                            inst_id, Decimal(str(price)), Decimal("0"), "FLATTEN-EMERGENCY"
                        )
                        logger.info("Flattened margin position %s: reduce-only (side, qty, pnl)=%s at %s",
                                    inst_id, closed, price)

            elif cmd_type == "TRIP_KILL_SWITCH":
                payload = cmd.get("payload", {})
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                latched_by = cmd.get("requester_id") or payload.get("operator", "operator")
                reason = payload.get("reason", "Operator manual trip")
                self.kill_switch.trip(latched_by=latched_by, reason=reason)
                # Working entry orders die with the trip; reduce-only exits keep working.
                to_cancel = [cid for cid, o in self.dispatcher.open_orders.items() if self.dispatcher.is_entry_order(o)]
                for cid in to_cancel:
                    self.dispatcher.release_reservation(cid)
                for inst_id, account in list(self.dispatcher.margin_accounts.items()):
                    try:
                        quote = self.dispatcher.node_cache.quote(InstrumentId.from_str(underlying_spot(inst_id))) \
                            if self.dispatcher.node_cache else None
                        if quote is None:
                            continue
                        price = Decimal(str(quote.bid_price if account.side == "LONG" else quote.ask_price))
                        qty = abs(account.position_qty)
                        fee = qty * price * self.dispatcher.taker_fee
                        self.dispatcher.flatten_margin_position(inst_id, price, fee, f"KILL-MARGIN-{uuid.uuid4().hex[:8]}")
                    except Exception as exc:
                        logger.error("Could not flatten isolated margin position %s after kill switch: %s", inst_id, exc)
                logger.critical("Kill switch latched by %s: %s (cancelled %d working entry orders)", latched_by, reason, len(to_cancel))

            elif cmd_type == "RESET_SWITCH":
                payload = cmd.get("payload", {})
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                reset_by = cmd.get("requester_id") or payload.get("operator", "operator")
                open_orders_cnt = len(self.dispatcher.open_orders)
                open_pos_cnt = self.dispatcher.open_position_count()
                success = self.kill_switch.reset(reset_by, open_orders_cnt, open_pos_cnt)
                if success:
                    logger.info("Latched kill switch reset by %s", reset_by)
                else:
                    logger.warning("Kill switch reset REJECTED: open orders=%d, open pos=%d", open_orders_cnt, open_pos_cnt)


    def stop(self) -> None:
        """Gracefully stops the paper trading engine."""
        self._running = False
        self._live_gate_open = False
        if self.node:
            try:
                self.node.stop()
            except Exception as e:
                logger.warning("Error stopping Nautilus LiveNode: %s", e)
