import asyncio
import collections
import contextlib
import dataclasses
import datetime
import json
import logging
import os
import uuid
from decimal import Decimal
import time
from typing import Any

import asyncpg
import redis.asyncio as aioredis

from cryptonite_execution.engine import PaperTradingEngine
from cryptonite_execution.matching import PaperFill, match_order, match_working_orders
from cryptonite_execution.risk import OrderIntent
from pydantic import ValidationError
from cryptonite_strategies.registry import STRATEGY_REGISTRY
from cryptonite_execution.bridge import EventBridgeWorker
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_orchestrator.mandate_gate import command_mandate_id, gate_ai_order, is_ai_command
from cryptonite_orchestrator.mandate_monitor import mandate_monitor_once, sweep_approvals_once
from nautilus_trader.model import Bar, BarType
from nautilus_trader.model import Price, Quantity

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("cryptonite.orchestrator")

DATABASE_URL = os.getenv("ASYNC_DATABASE_URL", "postgresql+asyncpg://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
ENVIRONMENT = os.getenv("ENVIRONMENT", "PAPER").upper()
ACCOUNT_ID = os.getenv("ACCOUNT_ID", "PAPER-ACCOUNT-001")


def to_canonical_instrument(sym: str) -> str:
    clean = sym.strip().upper()
    return clean if clean.endswith(".BINANCE") else f"{clean}.BINANCE"


def to_nautilus_bar_type_str(instrument_id: str, timeframe: str) -> str:
    inst = to_canonical_instrument(instrument_id)
    tf = timeframe.strip().lower()
    if tf.count("-") >= 4:
        return tf.upper()
    clean_tf = tf.replace(inst.lower(), "").strip("-")
    if clean_tf.endswith("m"):
        mins = clean_tf[:-1] or "1"
        return f"{inst}-{mins}-MINUTE-LAST-INTERNAL"
    elif clean_tf.endswith("h"):
        hrs = clean_tf[:-1] or "1"
        return f"{inst}-{hrs}-HOUR-LAST-INTERNAL"
    elif clean_tf.endswith("d"):
        days = clean_tf[:-1] or "1"
        return f"{inst}-{days}-DAY-LAST-INTERNAL"
    elif clean_tf.isdigit():
        return f"{inst}-{clean_tf}-MINUTE-LAST-INTERNAL"
    return f"{inst}-1-MINUTE-LAST-INTERNAL"


class SimpleQuote:
    def __init__(self, instrument_id: str, bid_price: float, ask_price: float, bid_size: float = 1.0, ask_size: float = 1.0, ts_init: int | None = None):
        self.instrument_id = instrument_id
        self.bid_price = Decimal(str(bid_price))
        self.ask_price = Decimal(str(ask_price))
        self.bid_size = Decimal(str(bid_size))
        self.ask_size = Decimal(str(ask_size))
        self.ts_init = ts_init or time.time_ns()


class SimpleQuoteCache:
    def __init__(self):
        self._quotes: dict[str, SimpleQuote] = {}

    def set_quote(self, instrument_id: str, bid: float, ask: float, bid_size: float = 1.0, ask_size: float = 1.0, ts_init: int | None = None):
        canonical = to_canonical_instrument(instrument_id)
        q = SimpleQuote(canonical, bid, ask, bid_size, ask_size, ts_init)
        self._quotes[canonical] = q
        self._quotes[canonical.replace(".BINANCE", "")] = q

    def quote(self, instrument_id: Any) -> SimpleQuote | None:
        inst_str = str(instrument_id.value) if hasattr(instrument_id, "value") else str(instrument_id)
        if inst_str in self._quotes:
            return self._quotes[inst_str]
        canonical = to_canonical_instrument(inst_str)
        return self._quotes.get(canonical)


DEFAULT_FEE_RATE = Decimal("0.001")
MAX_MATCH_QUOTE_AGE_NS = 5_000_000_000  # a backlog of older quotes never fills working orders

UPSERT_FILLED_ORDER_SQL = """
    INSERT INTO paper_orders (
        client_order_id, instance_id, account_id, instrument_id, side,
        order_type, quantity, price, filled_qty, avg_fill_price, status,
        last_event_version, created_at, updated_at
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $7, $9, 'FILLED', 1, now(), now())
    ON CONFLICT (client_order_id) DO UPDATE SET
        status = 'FILLED',
        filled_qty = EXCLUDED.filled_qty,
        avg_fill_price = EXCLUDED.avg_fill_price,
        updated_at = now();
"""


@contextlib.asynccontextmanager
async def _savepoint(conn):
    """A nested transaction (savepoint) when ``conn`` supports it; a no-op otherwise (test doubles)."""
    tx = conn.transaction() if hasattr(conn, "transaction") else None
    if asyncio.iscoroutine(tx):
        tx = await tx
    if hasattr(tx, "__aenter__"):
        async with tx:
            yield
    else:
        yield


DISPATCHED_ID_MEMORY = 10_000


def remember_dispatched(engine, client_order_id: str, command_id) -> None:
    """Record which command placed an order id, independently of the database and the bridge."""
    seen = engine.__dict__.setdefault("_dispatched_order_ids", collections.OrderedDict())
    seen[client_order_id] = str(command_id)
    while len(seen) > DISPATCHED_ID_MEMORY:
        seen.popitem(last=False)


def forget_dispatched(engine, client_order_id: str) -> None:
    engine.__dict__.get("_dispatched_order_ids", {}).pop(client_order_id, None)


def dispatched_by(engine, client_order_id: str) -> str | None:
    return engine.__dict__.get("_dispatched_order_ids", {}).get(client_order_id)


async def apply_paper_fill(
    conn, engine, fill: PaperFill, instance_id: str, order_type: str, limit_price: Decimal | None
) -> None:
    """Book a paper fill.

    Order: dispatcher ledger (pops the working order), then the ORDER_FILLED bridge event (the bridge
    also creates the paper_orders row if missing), then a best-effort paper_orders upsert for
    immediate UI consistency. A failed upsert therefore never leaves memory ahead of the database.
    """
    fee_rate = getattr(engine.dispatcher, "taker_fee", None) or DEFAULT_FEE_RATE  # maker fills pay it too
    comm = fill.quantity * fill.price * fee_rate
    realized = engine.dispatcher.handle_fill(
        client_order_id=fill.client_order_id,
        fill_qty=fill.quantity,
        fill_price=fill.price,
        commission=comm,
        side=fill.side,
        instrument_id=fill.instrument_id,
    )
    acc_id = getattr(engine, "account_id", ACCOUNT_ID)
    engine.bridge.enqueue_event({
        "event_type": "ORDER_FILLED",
        "event_version": 1,
        "client_order_id": fill.client_order_id,
        "account_id": acc_id,
        "instance_id": instance_id,
        "instrument_id": fill.instrument_id,
        "side": fill.side,
        "order_type": order_type,
        "limit_price": str(limit_price) if limit_price is not None else None,
        "price": str(fill.price),
        "quantity": str(fill.quantity),
        "commission": str(comm),
        "filled_qty": str(fill.quantity),
        "avg_fill_price": str(fill.price),
        "status": "FILLED",
        "occurred_at_ns": time.time_ns(),
        **engine.dispatcher.fill_event_fields(fill.instrument_id, realized, fill.price),
    })
    try:
        # Savepoint: inside the poller's claim transaction a failed statement would otherwise abort
        # the whole transaction, roll back the claim, and let a retry fill the order a second time
        async with _savepoint(conn):
            await conn.execute(
                UPSERT_FILLED_ORDER_SQL,
                fill.client_order_id, instance_id, acc_id, fill.instrument_id,
                fill.side, order_type, fill.quantity, limit_price, fill.price,
            )
    except Exception as e:  # the bridge event above still records the fill
        logger.error("paper_orders upsert failed for %s (bridge will record it): %s", fill.client_order_id, e)
    logger.info("Order %s filled %s %s @ %s (%s)", fill.client_order_id, fill.side, fill.quantity, fill.price, fill.liquidity)


async def reconcile_working_orders(conn, engine) -> None:
    """Cancel paper_orders rows still ACCEPTED that have no working order in memory any more.

    PAUSE/STOP/CLOSE_POSITION, latches, liquidations and restarts release working orders in the
    dispatcher only. Rows whose order filled are fixed by the bridge's ORDER_FILLED (version 1 > 0).
    """
    await conn.execute(
        """
        UPDATE paper_orders SET status = 'CANCELED', updated_at = now()
        WHERE account_id = $1 AND status = 'ACCEPTED' AND NOT (client_order_id = ANY($2::text[]))
        """,
        getattr(engine, "account_id", ACCOUNT_ID), list(engine.dispatcher.open_orders),
    )


def _entries_blocked(dispatcher) -> bool:
    return bool(dispatcher.kill_switch.is_latched or dispatcher.daily_loss_latched)


def cancel_entries_if_latched(dispatcher) -> list[str]:
    """While the kill switch or the daily-loss latch is set, working entry orders must not fill."""
    if not _entries_blocked(dispatcher):
        return []
    to_cancel = [
        cid for cid, o in dispatcher.open_orders.items()
        if o.get("intent") is not None and dispatcher.is_entry_order(o)
    ]
    for cid in to_cancel:
        dispatcher.release_reservation(cid)
    if to_cancel:
        logger.warning("Latched: cancelled %d working entry orders", len(to_cancel))
    return to_cancel


async def match_resting_orders(pool, engine, instrument_id: str, bid: float, ask: float) -> None:
    """Fill the working orders on ``instrument_id`` that this quote reaches."""
    d = engine.dispatcher
    cancelled = cancel_entries_if_latched(d)
    fills = match_working_orders(d.open_orders, instrument_id, Decimal(str(bid)), Decimal(str(ask)))
    if not fills and not cancelled:
        return
    async with pool.acquire() as conn:
        for fill in fills:
            info = d.open_orders.get(fill.client_order_id)
            if info is None:  # filled or cancelled while we waited for the connection
                continue
            # State may have moved since matching (awaits above): re-check before booking
            if _entries_blocked(d) and d.is_entry_order(info):
                d.release_reservation(fill.client_order_id)
                cancelled.append(fill.client_order_id)
                continue
            qty = d.fillable_quantity(info)
            if qty <= 0:
                logger.warning("Working exit %s outlived its position; cancelled", fill.client_order_id)
                d.release_reservation(fill.client_order_id)
                cancelled.append(fill.client_order_id)
                continue
            if qty < fill.quantity:
                fill = dataclasses.replace(fill, quantity=qty)
            intent = info["intent"]
            await apply_paper_fill(conn, engine, fill, intent.instance_id, intent.order_type.upper(), intent.price)
        if cancelled:
            await reconcile_working_orders(conn, engine)


def _patch_strategy_for_paper_engine(strategy, engine, instance_id: str):
    """Monkey-patch Nautilus Strategy to route submit_order to PaperTradingEngine dispatcher."""
    def _submit_order(order):
        # Nautilus OrderType: MARKET=1, LIMIT=2. Anything else (stops, ...) has no paper matcher.
        type_value = getattr(getattr(order, "order_type", None), "value", None)
        if type_value not in (1, 2):
            logger.warning("Strategy %s order %s rejected: unsupported order type %s", instance_id, order.client_order_id, type_value)
            return
        intent = OrderIntent(
            client_order_id=order.client_order_id.value,
            instance_id=instance_id,
            instrument_id=to_canonical_instrument(str(order.instrument_id)),
            side="BUY" if order.side.value == 1 else "SELL",  # OrderSide.BUY=1
            order_type="MARKET" if type_value == 1 else "LIMIT",
            quantity=Decimal(str(order.quantity.as_double())),
            price=Decimal(str(order.price.as_double())) if getattr(order, "price", None) else None,
        )
        engine.dispatcher.dispatch(intent, lambda o: None, nautilus_order=order)

    strategy.submit_order = _submit_order

    def _buy_market(qty=None):
        qty_dec = Decimal(str(qty.as_double())) if qty else Decimal(str(strategy.trade_size))
        intent = OrderIntent(
            client_order_id=f"O-{uuid.uuid4().hex[:8]}",
            instance_id=instance_id,
            instrument_id=to_canonical_instrument(str(strategy.instrument_id)),
            side="BUY",
            order_type="MARKET",
            quantity=qty_dec,
        )
        engine.dispatcher.dispatch(intent, lambda o: None, nautilus_order=None)
        
    def _sell_market(qty=None):
        qty_dec = Decimal(str(qty.as_double())) if qty else Decimal(str(strategy.trade_size))
        intent = OrderIntent(
            client_order_id=f"O-{uuid.uuid4().hex[:8]}",
            instance_id=instance_id,
            instrument_id=to_canonical_instrument(str(strategy.instrument_id)),
            side="SELL",
            order_type="MARKET",
            quantity=qty_dec,
        )
        engine.dispatcher.dispatch(intent, lambda o: None, nautilus_order=None)

    strategy.buy_market = _buy_market
    strategy.sell_market = _sell_market

    # Paper fills never reach the Nautilus portfolio, so position queries read the dispatcher ledger
    # (account-level: strategies sharing an instrument see the same position).
    inst = to_canonical_instrument(str(strategy.instrument_id))
    d = engine.dispatcher
    strategy.is_long = lambda: d.get_position_qty(inst) > 0 or d.get_perp_position_qty(inst) > 0
    strategy.is_short = lambda: d.get_perp_position_qty(inst) < 0
    strategy.is_flat = lambda: d.get_position_qty(inst) == 0 and d.get_perp_position_qty(inst) == 0

    def _close_all(*_args, **_kwargs):
        if getattr(strategy, "is_in_warmup", None) and strategy.is_in_warmup() is True:
            return
        perp_qty = d.get_perp_position_qty(inst)
        if perp_qty != 0:  # perps close with an opposing reduce-only order
            side, qty, reduce_only = ("BUY" if perp_qty < 0 else "SELL"), abs(perp_qty), True
        else:
            side, qty, reduce_only = "SELL", d.get_available_position_qty(inst), False
        if qty <= 0:
            return
        intent = OrderIntent(
            client_order_id=f"O-{uuid.uuid4().hex[:8]}",
            instance_id=instance_id,
            instrument_id=inst,
            side=side,
            order_type="MARKET",
            quantity=qty,
            reduce_only=reduce_only,
        )
        d.dispatch(intent, lambda o: None, nautilus_order=None)

    strategy.close_all = _close_all
    strategy.close_all_positions = _close_all
    return strategy


async def consume_market_data(redis, engine, quote_cache, pool=None):
    """Consumes ui_quote_tick and ingress_health streams to maintain live cache and telemetry.

    With ``pool``, every quote also matches the working paper orders on its instrument.
    """
    quote_stream = "cryptonite:stream:ui_quote_tick"
    health_stream = "cryptonite:stream:ingress_health"

    # Pre-seed latest quotes from stream
    try:
        latest_quotes = await redis.xrevrange(quote_stream, "+", "-", count=20)
        for msg_id, fields in reversed(latest_quotes or []):
            raw_data = fields.get(b"data") or fields.get("data")
            if raw_data:
                data_str = raw_data.decode("utf-8") if isinstance(raw_data, bytes) else str(raw_data)
                data = json.loads(data_str)
                quote_cache.set_quote(
                    data["instrument_id"],
                    float(data["bid_price"]),
                    float(data["ask_price"]),
                    float(data.get("bid_size", 1.0)),
                    float(data.get("ask_size", 1.0)),
                    int(data.get("arrival_ns", time.time_ns())),
                )
        logger.info("Pre-seeded quote cache with %d entries", len(latest_quotes) if latest_quotes else 0)
    except Exception as e:
        logger.warning("Could not pre-seed quotes: %s", e)

    # Pre-seed latest health records
    try:
        latest_health = await redis.xrevrange(health_stream, "+", "-", count=20)
        for msg_id, fields in reversed(latest_health or []):
            raw_data = fields.get(b"data") or fields.get("data")
            if raw_data:
                data_str = raw_data.decode("utf-8") if isinstance(raw_data, bytes) else str(raw_data)
                data = json.loads(data_str)
                can_inst = to_canonical_instrument(data["instrument_id"])
                engine.ingress_health.update_health(
                    instrument_id=can_inst,
                    session_id=str(data.get("ingest_session_id", "default")),
                    seq=int(data.get("publish_sequence", 0)),
                    event_ts_ns=int(data.get("event_ts_ns", time.time_ns())),
                    arrival_ts_ns=int(data.get("arrival_ts_ns", time.time_ns())),
                )
        logger.info("Pre-seeded ingress health with %d entries", len(latest_health) if latest_health else 0)
    except Exception as e:
        logger.warning("Could not pre-seed ingress health: %s", e)

    async def _consume_quotes():
        last_id = "$"
        while True:
            try:
                res = await redis.xread({quote_stream: last_id}, count=100, block=200)
                if not res:
                    continue
                for _, msgs in res:
                    for msg_id, fields in msgs:
                        last_id = msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id)
                        raw_data = fields.get(b"data") or fields.get("data")
                        if not raw_data:
                            continue
                        data_str = raw_data.decode("utf-8") if isinstance(raw_data, bytes) else str(raw_data)
                        data = json.loads(data_str)
                        quote_cache.set_quote(
                            data["instrument_id"],
                            float(data["bid_price"]),
                            float(data["ask_price"]),
                            float(data.get("bid_size", 1.0)),
                            float(data.get("ask_size", 1.0)),
                            int(data.get("arrival_ns", time.time_ns())),
                        )
                        arrival_ns = int(data.get("arrival_ns", time.time_ns()))
                        if pool is not None and engine.dispatcher.open_orders and time.time_ns() - arrival_ns <= MAX_MATCH_QUOTE_AGE_NS:
                            try:
                                await match_resting_orders(
                                    pool, engine, to_canonical_instrument(data["instrument_id"]),
                                    float(data["bid_price"]), float(data["ask_price"]),
                                )
                            except Exception as e:
                                logger.error("Paper order matching failed for %s: %s", data["instrument_id"], e)
            except Exception as e:
                logger.debug("consume_quotes error: %s", e)
                await asyncio.sleep(0.5)

    async def _consume_health():
        last_id = "$"
        while True:
            try:
                res = await redis.xread({health_stream: last_id}, count=50, block=200)
                if not res:
                    continue
                for _, msgs in res:
                    for msg_id, fields in msgs:
                        last_id = msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id)
                        raw_data = fields.get(b"data") or fields.get("data")
                        if not raw_data:
                            continue
                        data_str = raw_data.decode("utf-8") if isinstance(raw_data, bytes) else str(raw_data)
                        data = json.loads(data_str)
                        can_inst = to_canonical_instrument(data["instrument_id"])
                        engine.ingress_health.update_health(
                            instrument_id=can_inst,
                            session_id=str(data.get("ingest_session_id", "default")),
                            seq=int(data.get("publish_sequence", 0)),
                            event_ts_ns=int(data.get("event_ts_ns", time.time_ns())),
                            arrival_ts_ns=int(data.get("arrival_ts_ns", time.time_ns())),
                        )
            except Exception as e:
                logger.debug("consume_health error: %s", e)
                await asyncio.sleep(0.5)

    await asyncio.gather(_consume_quotes(), _consume_health())


PERP_MARK_CHANNEL = "channel:perp_mark_ticks"  # published by cryptonite_scanner_service.derivatives_feed
PERP_MARK_KEY_PATTERN = "perp:mark:*"


def apply_perp_mark_tick(engine, raw) -> dict[str, Any] | None:
    """Feed one perp.mark tick (JSON from the derivatives feed) into the dispatcher; return it parsed."""
    data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    if data.get("type") != "perp.mark":
        return None
    tick = {
        "instrument_id": data["instrument_id"],
        "mark_price": Decimal(str(data["mark_price"])),
        "funding_rate": Decimal(str(data["funding_rate"])),
        "next_funding_time_ns": int(data["next_funding_time_ns"]),
    }
    engine.dispatcher.update_mark_price(tick["instrument_id"], tick["mark_price"], int(data["ts_event_ns"]))
    return tick


async def settle_funding_on_rollover(engine, funding_state: dict[str, dict[str, Any]], tick: dict[str, Any] | None):
    """Settle funding when a symbol's next funding time rolls forward.

    The settlement is booked at the previous funding time with the rate and mark from the last
    tick before the rollover. Only one settlement is booked per rollover, so settlements missed
    while disconnected are not reconstructed.
    """
    if tick is None:
        return
    prev = funding_state.get(tick["instrument_id"])
    if prev is not None and tick["next_funding_time_ns"] < prev["next_funding_time_ns"]:
        return  # out-of-order tick; keep the newer state
    funding_state[tick["instrument_id"]] = tick
    if prev is None or tick["next_funding_time_ns"] == prev["next_funding_time_ns"]:
        return
    payments, liquidations = await engine.process_funding_settlement(
        prev["instrument_id"], prev["mark_price"], prev["funding_rate"], prev["next_funding_time_ns"]
    )
    for p in payments:
        logger.info(
            "Funding settled %s @ %s: rate %s, cash flow %s (cumulative %s)",
            p.instrument_id, p.funding_time_ns, p.funding_rate, p.cash_flow, p.cumulative_funding,
        )
    for liq in liquidations:
        logger.critical("Funding-triggered liquidation %s: %s %s", liq.client_order_id, liq.instrument_id, liq.side)


async def consume_perp_marks(
    redis,
    engine,
    channel: str = PERP_MARK_CHANNEL,
    key_pattern: str = PERP_MARK_KEY_PATTERN,
    liquidation_interval_s: float = 1.0,
):
    """Keep dispatcher mark prices current and run liquidation checks on fresh marks.

    Seeds from the cached ``perp:mark:*`` keys, then follows the pub/sub channel. A tick whose
    next funding time rolled forward settles funding (``settle_funding_on_rollover``). Every
    ``liquidation_interval_s`` it calls ``engine.process_liquidations`` with the marks that are
    still fresh, so a stalled feed never liquidates on stale prices.
    """
    funding_state: dict[str, dict[str, Any]] = {}
    while True:
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(channel)
            async for key in redis.scan_iter(match=key_pattern):
                cached = await redis.get(key)
                if cached:
                    await settle_funding_on_rollover(engine, funding_state, apply_perp_mark_tick(engine, cached))
            last_check = 0.0
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=liquidation_interval_s)
                if msg and msg.get("type") == "message":
                    await settle_funding_on_rollover(engine, funding_state, apply_perp_mark_tick(engine, msg["data"]))
                now = time.monotonic()
                if now - last_check >= liquidation_interval_s:
                    last_check = now
                    fresh = engine.dispatcher.fresh_mark_prices()
                    if fresh and engine.dispatcher.perp_positions:
                        for liq in await engine.process_liquidations(fresh):
                            logger.critical(
                                "Synthetic liquidation %s: %s %s %s @ %s (mark %s)",
                                liq.client_order_id, liq.instrument_id, liq.side, liq.quantity,
                                liq.fill_price, liq.mark_price,
                            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Perp mark consumer error, resubscribing: %s", e)
            await asyncio.sleep(1.0)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()


SYSTEM_ALERTS_CHANNEL = "channel:system_alerts"
DAILY_DRAWDOWN_REASON = "DAILY_DRAWDOWN_LIMIT_BREACHED"


async def risk_monitor_once(engine, pool, redis, now: datetime.datetime | None = None) -> None:
    """One risk-monitor pass: UTC day rollover, DB kill-switch sync, daily drawdown breaker."""
    d = engine.dispatcher
    now = now or datetime.datetime.now(datetime.UTC)
    equity = d.portfolio_equity()

    # 1. UTC midnight: re-base the drawdown high-water mark and clear daily PnL / daily-loss latch
    if d.roll_trading_day(now.date(), equity):
        logger.info("UTC trading day rolled to %s; drawdown base equity %s", now.date(), equity)
        await pool.execute(
            "UPDATE paper_accounts SET daily_realized_pnl = 0, daily_loss_latched = false, utc_trading_day = $2, updated_at = now() WHERE account_id = $1",
            engine.account_id, now.date(),
        )

    # 2. A PAPER latch written straight to the DB (e.g. /control/kill-switch/trip) must stop the engine too
    row = await pool.fetchrow(
        "SELECT is_latched, latched_by, reason FROM execution_kill_switch WHERE environment = 'PAPER' AND account_id = $1",
        engine.account_id,
    )
    if row and row["is_latched"] and not engine.kill_switch.is_latched:
        engine.kill_switch.trip(latched_by=row["latched_by"] or "database", reason=row["reason"] or "Latched in database")
        logger.critical("Paper kill switch latched from database (%s)", row["reason"])

    # 3. Daily drawdown circuit breaker: latch entries, trip + persist the kill switch, alert
    was_latched = d.daily_loss_latched
    if d.check_daily_drawdown(equity) and not was_latched:
        hwm = d.high_water_mark
        drawdown = (hwm - equity) / hwm if hwm else Decimal("0")
        if not engine.kill_switch.is_latched:
            engine.kill_switch.trip(latched_by="risk-monitor", reason=DAILY_DRAWDOWN_REASON)
        await pool.fetchval(
            "SELECT trip_execution_kill_switch($1, $2, $3, $4)", "PAPER", engine.account_id, DAILY_DRAWDOWN_REASON, "risk-monitor"
        )
        await pool.execute("UPDATE paper_accounts SET daily_loss_latched = true, updated_at = now() WHERE account_id = $1", engine.account_id)
        detail = {"equity": str(equity), "high_water_mark": str(hwm), "drawdown_pct": str(drawdown), "limit_pct": str(d.max_daily_loss_pct)}
        await pool.execute(
            """INSERT INTO paper_risk_events (event_id, account_id, rule_name, severity, message, blocked_action, occurred_at)
               VALUES (gen_random_uuid(), $1, $2, 'CRITICAL', $3, $4::jsonb, now())""",
            engine.account_id, DAILY_DRAWDOWN_REASON,
            f"Daily drawdown {drawdown:.2%} >= {d.max_daily_loss_pct:.0%}: kill switch tripped", json.dumps(detail),
        )
        if redis is not None:
            await redis.publish(SYSTEM_ALERTS_CHANNEL, json.dumps({
                "type": "system_alert", "v": 1, "kind": DAILY_DRAWDOWN_REASON, "account_id": engine.account_id, **detail,
            }))
        logger.critical("DAILY DRAWDOWN BREACH %.2f%%: kill switch tripped", float(drawdown * 100))


async def risk_monitor(engine, pool, redis, interval_s: float = 1.0, reconcile_every: int = 5):
    tick = 0
    while True:
        try:
            await risk_monitor_once(engine, pool, redis)
            tick += 1
            if tick % reconcile_every == 0:
                cancel_entries_if_latched(engine.dispatcher)
                async with pool.acquire() as conn:
                    await reconcile_working_orders(conn, engine)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Risk monitor error: %s", e)
        if tick % reconcile_every == 0:
            # Mandate hard stops and the account backstop (A5.2); isolated so a failure here never
            # stops the daily breaker above
            try:
                await mandate_monitor_once(engine, pool, redis)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Mandate monitor error: %s", e)
            try:
                await sweep_approvals_once(pool)  # D8 approval timeouts (A5.3)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Approval sweeper error: %s", e)
        await asyncio.sleep(interval_s)


async def command_poller(pool, engine, quote_cache=None):
    """Poll execution_commands for PAPER environment commands."""
    if quote_cache is None:
        quote_cache = SimpleQuoteCache()
    while True:
        try:
            async with pool.acquire() as conn:
                tx = conn.transaction() if hasattr(conn, "transaction") else None
                if asyncio.iscoroutine(tx):
                    tx = await tx
                cm = tx if hasattr(tx, "__aenter__") else contextlib.nullcontext()
                async with cm:
                    row = await conn.fetchrow("""
                        WITH claimed AS (
                            SELECT command_id FROM execution_commands
                            WHERE status = 'PENDING'
                              AND environment = 'PAPER'
                              AND command_type IN ('DEPLOY', 'STOP', 'PAUSE', 'RESUME', 'CLOSE_POSITION', 'MANUAL_ORDER', 'SUBMIT_ORDER', 'CANCEL_ORDER', 'TRIP_KILL_SWITCH', 'RESET_SWITCH', 'KILL_SWITCH_RESET_REQUEST')
                              AND (lease_expires_at IS NULL OR lease_expires_at < now())
                            ORDER BY created_at ASC LIMIT 1
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE execution_commands
                        SET status = 'RUNNING',
                            lease_owner = 'cryptonite-orchestrator',
                            lease_expires_at = now() + INTERVAL '30 seconds',
                            attempt_id = gen_random_uuid(),
                            attempt_count = attempt_count + 1
                        FROM claimed
                        WHERE execution_commands.command_id = claimed.command_id
                        RETURNING execution_commands.*
                    """)
                    if row:
                        cmd_type = row["command_type"]
                        target = row["target_instance_id"]
                        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                        acc_id = getattr(engine, "account_id", ACCOUNT_ID)
                        
                        logger.info("Orchestrator processing %s for %s", cmd_type, target)
                        
                        if cmd_type == "DEPLOY":
                            await conn.execute("""
                                INSERT INTO paper_strategy_instances (
                                    instance_id, account_id, strategy_name, strategy_version_id,
                                    strategy_source_hash, risk_policy_hash, last_processed_bar_ns,
                                    instrument_id, timeframe, resolved_parameters, allocation_usd,
                                    status, created_at, updated_at
                                ) VALUES ($1, $2, $3, $4::uuid, 'dummy_hash', 'dummy_hash', 0, $5, $6, $7::jsonb, $8, 'RUNNING', now(), now())
                                ON CONFLICT (instance_id) DO UPDATE SET status = 'RUNNING', updated_at = now();
                            """,
                                target, payload.get("account_id") or acc_id, payload["strategy_name"],
                                payload["strategy_version_id"], payload["instrument_id"],
                                payload["timeframe"], json.dumps(payload.get("parameters", {})),
                                payload.get("allocation_usd", Decimal("0"))
                            )
                            await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type in ("STOP", "PAUSE"):
                            await conn.execute("UPDATE paper_strategy_instances SET status = $2, updated_at = now() WHERE instance_id = $1", target, cmd_type)
                            engine.mailbox.push({"command_type": cmd_type, "target_instance_id": target})
                            engine.process_mailbox_commands()
                            await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type == "RESUME":
                            await conn.execute("UPDATE paper_strategy_instances SET status = 'RUNNING', updated_at = now() WHERE instance_id = $1", target)
                            await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type == "CLOSE_POSITION":
                            inst_raw = payload.get("instrument_id", "")
                            canonical_inst = to_canonical_instrument(inst_raw) if inst_raw else ""
                            def release_instrument_orders():
                                # Working orders on the instrument would outlive the position being closed
                                for cid in [c for c, o in engine.dispatcher.open_orders.items() if o.get("instrument_id") == canonical_inst]:
                                    engine.dispatcher.release_reservation(cid)

                            pos_qty = engine.dispatcher.get_position_qty(canonical_inst)
                            perp_qty = engine.dispatcher.get_perp_position_qty(canonical_inst)
                            if pos_qty > Decimal("0") or perp_qty != 0:
                                # Spot long closes with a SELL; a perp closes with an opposing reduce-only order
                                close_side = "BUY" if perp_qty < 0 else "SELL"
                                close_qty = abs(perp_qty) if perp_qty != 0 else pos_qty
                                if perp_qty == 0 and payload.get("quantity"):
                                    # a mandate stop closes only the mandate's own quantity (A5.2)
                                    close_qty = min(pos_qty, Decimal(str(payload["quantity"])))
                                quote = quote_cache.quote(canonical_inst)
                                if quote:
                                    fill_px = Decimal(str(quote.ask_price if close_side == "BUY" else quote.bid_price))
                                else:
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                        row["command_id"], "NO_LIVE_QUOTE",
                                    )
                                    continue
                                release_instrument_orders()
                                comm = close_qty * fill_px * (getattr(engine.dispatcher, "taker_fee", None) or DEFAULT_FEE_RATE)
                                client_oid = f"CLOSE-{uuid.uuid4().hex[:8].upper()}"
                                instance_id = target or "MANUAL_TRADE"

                                realized = None
                                if perp_qty != 0:
                                    _, _, realized = engine.dispatcher.flatten_perp_position(
                                        canonical_inst, fill_px, comm, client_oid
                                    )
                                else:
                                    engine.dispatcher.handle_fill(
                                        client_order_id=client_oid,
                                        fill_qty=close_qty,
                                        fill_price=fill_px,
                                        commission=comm,
                                        side="SELL",
                                        instrument_id=canonical_inst,
                                    )
                                await conn.execute(
                                    """
                                    INSERT INTO paper_orders (
                                        client_order_id, instance_id, account_id, instrument_id, side,
                                        order_type, quantity, price, filled_qty, avg_fill_price, status,
                                        last_event_version, created_at, updated_at
                                    ) VALUES ($1, $2, $3, $4, $7, 'MARKET', $5, $6, $5, $6, 'FILLED', 1, now(), now());
                                    """,
                                    client_oid, instance_id, acc_id, canonical_inst, close_qty, fill_px, close_side
                                )
                                engine.bridge.enqueue_event({
                                    "event_type": "ORDER_FILLED",
                                    "event_version": 1,
                                    "client_order_id": client_oid,
                                    "account_id": acc_id,
                                    "instance_id": instance_id,
                                    "instrument_id": canonical_inst,
                                    "side": close_side,
                                    "price": str(fill_px),
                                    "quantity": str(close_qty),
                                    "commission": str(comm),
                                    "filled_qty": str(close_qty),
                                    "avg_fill_price": str(fill_px),
                                    "status": "FILLED",
                                    "occurred_at_ns": time.time_ns(),
                                    **engine.dispatcher.fill_event_fields(canonical_inst, realized, fill_px),
                                })
                            else:
                                release_instrument_orders()
                            await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type == "CANCEL_ORDER":
                            client_oid = payload.get("client_order_id")
                            if client_oid:
                                await conn.execute(
                                    "UPDATE paper_orders SET status = 'CANCELED', updated_at = now() WHERE client_order_id = $1 AND status IN ('PENDING', 'SUBMITTED', 'ACCEPTED')",
                                    client_oid
                                )
                                if hasattr(engine.dispatcher, "release_reservation"):
                                    engine.dispatcher.release_reservation(client_oid)
                                await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                            else:
                                await conn.execute("UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = 'Missing client_order_id', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type == "TRIP_KILL_SWITCH":
                            # Latch the in-memory switch the dispatcher checks, then persist it (row + audit)
                            reason = payload.get("reason") or "Manual UI Kill Switch"
                            actor = payload.get("operator") or row.get("requester_id") or "web-ui"
                            engine.mailbox.push({"command_type": "TRIP_KILL_SWITCH", "requester_id": actor, "payload": {"reason": reason}})
                            engine.process_mailbox_commands()
                            await conn.fetchval("SELECT trip_execution_kill_switch($1, $2, $3, $4)", "PAPER", acc_id, reason, actor)
                            await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type in ("RESET_SWITCH", "KILL_SWITCH_RESET_REQUEST"):
                            # RESET_SWITCH: /paper/kill-switch/reset. KILL_SWITCH_RESET_REQUEST: /control reset
                            # (step-up authenticated), which carries an expiry and the latch generation it saw.
                            # Either way the engine refuses while any order or position is open.
                            actor = payload.get("operator") or payload.get("actor_id") or row.get("requester_id") or "web-ui"
                            stale = ""
                            if cmd_type == "KILL_SWITCH_RESET_REQUEST":
                                if payload.get("expires_at_ns") and time.time_ns() > int(payload["expires_at_ns"]):
                                    stale = "RESET_REJECTED: request expired"
                                elif payload.get("expected_generation") is not None:
                                    gen = await conn.fetchval(
                                        "SELECT latch_generation FROM execution_kill_switch WHERE environment = 'PAPER' AND account_id = $1",
                                        acc_id,
                                    )
                                    if gen is not None and int(gen) != int(payload["expected_generation"]):
                                        stale = f"RESET_REJECTED: latch generation changed ({payload['expected_generation']} -> {gen})"
                            if not stale:
                                engine.mailbox.push({"command_type": "RESET_SWITCH", "requester_id": actor})
                                engine.process_mailbox_commands()
                            if stale or engine.kill_switch.is_latched:
                                reason = stale or (
                                    f"RESET_REJECTED: open orders={len(engine.dispatcher.open_orders)}, "
                                    f"open positions={engine.dispatcher.open_position_count()}"
                                )
                                await conn.execute("UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1", row["command_id"], reason)
                            else:
                                await conn.execute(
                                    "UPDATE execution_kill_switch SET is_latched = false, reset_at = now(), reset_by = $3, updated_at = now() WHERE environment = $1 AND account_id = $2",
                                    "PAPER", acc_id, actor,
                                )
                                await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type in ("MANUAL_ORDER", "SUBMIT_ORDER"):
                            inst_raw = payload.get("instrument_id") or payload.get("symbol", "BTCUSDT")
                            canonical_inst = to_canonical_instrument(inst_raw)
                            
                            # Determine side
                            raw_side = payload.get("side")
                            if not raw_side:
                                rec = payload.get("recommendation", "").upper()
                                raw_side = "BUY" if rec in ("BUY", "OVERWEIGHT") else "SELL"
                            side = raw_side.upper()
                            
                            # AI orders are checked against their mandate before the dispatcher (A5.1) and
                            # are booked under the mandate's own paper instance
                            ai_order = is_ai_command(cmd_type, target, row.get("requester_id"))
                            mandate = None
                            if ai_order and (mandate_ref := command_mandate_id(row, payload)):
                                mandate = await load_mandate(conn, mandate_ref)

                            # Determine quantity
                            qty_val = payload.get("quantity")
                            if not qty_val:
                                alloc_pct = Decimal(str(payload.get("allocation_pct", 5.0)))
                                quote = quote_cache.quote(canonical_inst)
                                if not quote:
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                        row["command_id"], "NO_LIVE_QUOTE_FOR_SIZING",
                                    )
                                    continue
                                mark_px = Decimal(str(quote.ask_price if side == "BUY" else quote.bid_price))
                                # a mandate order's allocation is a share of the mandate's budget ("6% of mandate")
                                base = mandate.capital_budget_usd if mandate is not None else engine.dispatcher.cash_balance
                                notional = base * (alloc_pct / Decimal("100.0"))
                                qty_val = max(Decimal("0.0001"), notional / mark_px)
                            qty_dec = Decimal(str(qty_val))
                            
                            price_dec = Decimal(str(payload["price"])) if payload.get("price") else None
                            order_type = payload.get("order_type", "MARKET").upper()
                            # Derived from the command id when not supplied, so a retried claim (after a
                            # rolled-back transaction) recognises its own order instead of placing a second one
                            client_oid = payload.get("client_order_id") or f"{'MANUAL' if cmd_type == 'MANUAL_ORDER' else 'AI'}-{str(row['command_id']).replace('-', '')[:12].upper()}"
                            instance_id = target or ("MANUAL_TRADE" if cmd_type == "MANUAL_ORDER" else "AI_DEBATE_WORKER")
                            if mandate is not None:
                                instance_id = mandate.instance_id
                            
                            try:
                                intent = OrderIntent(
                                    client_order_id=client_oid,
                                    instance_id=instance_id,
                                    instrument_id=canonical_inst,
                                    side=side,
                                    order_type=order_type,
                                    quantity=qty_dec,
                                    price=price_dec,
                                    # Perp attributes; the dispatcher defaults leverage to 1 and CROSS,
                                    # and rejects them on spot instruments
                                    leverage=payload.get("leverage"),
                                    margin_mode=payload.get("margin_mode"),
                                    reduce_only=payload.get("reduce_only") or False,
                                )
                            except ValidationError as e:
                                await conn.execute(
                                    "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                    row["command_id"], f"INVALID_ORDER_PARAMETERS: {e.errors()[0]['loc']} {e.errors()[0]['msg']}",
                                )
                                continue
                            
                            placed_by = dispatched_by(engine, client_oid)
                            already = (
                                placed_by is not None
                                or client_oid in engine.dispatcher.open_orders
                                or await conn.fetchval("SELECT 1 FROM paper_orders WHERE client_order_id = $1", client_oid) == 1
                            )
                            if already:
                                derived = not payload.get("client_order_id")
                                if placed_by == str(row["command_id"]) or derived:
                                    # A retried claim of this same command: its order is already placed
                                    logger.warning("Command %s retried; order %s already placed", row["command_id"], client_oid)
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1",
                                        row["command_id"],
                                    )
                                else:
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                        row["command_id"], "DUPLICATE_CLIENT_ORDER_ID",
                                    )
                                continue

                            if ai_order:
                                mandate_reject = await gate_ai_order(
                                    conn, engine, row, payload, intent, mandate, quote_cache.quote(canonical_inst),
                                )
                                if mandate_reject:
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                        row["command_id"], mandate_reject,
                                    )
                                    continue

                            # Dispatch intent to dispatcher (evaluates risk + atomically reserves)
                            approved = False
                            reason = "NO_DISPATCHER"
                            if hasattr(engine.dispatcher, "dispatch") and callable(engine.dispatcher.dispatch):
                                res = engine.dispatcher.dispatch(intent, lambda o: None, nautilus_order=None)
                                if isinstance(res, tuple):
                                    approved, reason = res[0], res[1]
                                else:
                                    approved, reason = True, "APPROVED"
                            else:
                                approved, reason, _ = engine.dispatcher.evaluate_intent(intent)

                            if not approved:
                                logger.warning("Order rejected by dispatcher for %s: %s", client_oid, reason)
                                await conn.execute(
                                    """
                                    UPDATE execution_commands
                                    SET status = 'REJECTED',
                                        command_outcome = 'REJECTED',
                                        error_message = $2,
                                        processed_at = now()
                                    WHERE command_id = $1;
                                    """,
                                    row["command_id"], reason
                                )
                            else:
                                remember_dispatched(engine, client_oid, row["command_id"])
                                try:
                                    quote = quote_cache.quote(canonical_inst)
                                    order_info = engine.dispatcher.open_orders.get(client_oid)
                                    fill = None
                                    if quote and order_info is not None:
                                        fill = match_order(
                                            client_oid, order_info,
                                            Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price)),
                                        )
                                    if fill:
                                        await apply_paper_fill(conn, engine, fill, instance_id, order_type, price_dec)
                                    else:
                                        # Works until a quote reaches it: a non-marketable limit rests at its
                                        # limit; an order without a quote yet takes the first quote.
                                        if quote and order_info is not None:
                                            order_info["rested"] = True
                                        await conn.execute(
                                            """
                                            INSERT INTO paper_orders (
                                                client_order_id, instance_id, account_id, instrument_id, side,
                                                order_type, quantity, price, filled_qty, status,
                                                last_event_version, created_at, updated_at
                                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0, 'ACCEPTED', 0, now(), now())
                                            ON CONFLICT (client_order_id) DO NOTHING;
                                            """,
                                            client_oid, instance_id, acc_id, canonical_inst,
                                            side, order_type, qty_dec, price_dec,
                                        )
                                        logger.info("Order %s accepted and working: %s %s %s @ %s", client_oid, order_type, side, qty_dec, price_dec)

                                    await conn.execute(
                                        """
                                        UPDATE execution_commands
                                        SET status = 'COMPLETED',
                                            command_outcome = 'SUCCESS',
                                            processed_at = now()
                                        WHERE command_id = $1;
                                        """,
                                        row["command_id"]
                                    )
                                except BaseException:
                                    # The claim transaction rolls back and the command is retried: a still-working
                                    # order from this attempt must not survive in memory (it would fill later)
                                    if hasattr(engine.dispatcher, "release_reservation") and client_oid in engine.dispatcher.open_orders:
                                        engine.dispatcher.release_reservation(client_oid)
                                        # Nothing was placed after all: let the retry place it again
                                        forget_dispatched(engine, client_oid)
                                    raise

                        if cmd_type in ("STOP", "PAUSE", "CLOSE_POSITION", "TRIP_KILL_SWITCH"):
                            await reconcile_working_orders(conn, engine)

        except Exception as e:
            logger.error("Error in command poller: %s", e)
        await asyncio.sleep(0.1)


async def rehydrate_dispatcher_state(pool, engine):
    """Load cash, spot positions, and perp positions from Postgres into the dispatcher."""
    try:
        acc_row = await pool.fetchrow("SELECT cash_balance FROM paper_accounts WHERE account_id = $1", engine.account_id)
        if acc_row:
            engine.dispatcher.cash_balance = Decimal(str(acc_row["cash_balance"]))
            logger.info("Rehydrated cash balance: %s", engine.dispatcher.cash_balance)
    
        pos_rows = await pool.fetch(
            "SELECT instrument_id, quantity FROM paper_positions WHERE account_id = $1 AND status = 'OPEN' "
            "AND instrument_id NOT LIKE '%-PERP.%'",
            engine.account_id,
        )
        for pr in pos_rows:
            engine.dispatcher.positions[pr["instrument_id"]] = Decimal(str(pr["quantity"]))
        logger.info("Rehydrated %d open positions from database", len(pos_rows))
    except Exception as e:
        logger.warning("Error rehydrating state: %s", e)

    # Perp positions (needs migration 020 columns): signed quantity, locked margin, owning instance
    try:
        perp_rows = await pool.fetch(
            "SELECT instrument_id, instance_id, side, quantity, entry_price, leverage, margin_mode "
            "FROM paper_positions WHERE account_id = $1 AND status = 'OPEN' AND instrument_id LIKE '%-PERP.%'",
            engine.account_id,
        )
        for pr in perp_rows:
            qty = Decimal(str(pr["quantity"]))
            engine.dispatcher.set_perp_position(
                pr["instrument_id"], -qty if pr["side"] == "SHORT" else qty, Decimal(str(pr["entry_price"])),
                leverage=pr["leverage"], margin_mode=pr["margin_mode"] or "CROSS", instance_id=pr["instance_id"],
            )
        logger.info("Rehydrated %d open perp positions from database", len(perp_rows))
    except Exception as e:
        logger.warning("Error rehydrating perp positions: %s", e)

    # Fail closed across restarts: a latched PAPER kill switch stays latched
    try:
        ks = await pool.fetchrow(
            "SELECT is_latched, latched_by, reason FROM execution_kill_switch WHERE environment = 'PAPER' AND account_id = $1",
            engine.account_id,
        )
        if ks and ks["is_latched"]:
            engine.kill_switch.trip(latched_by=ks["latched_by"] or "database", reason=ks["reason"] or "Latched before restart")
            logger.critical("Paper kill switch restored LATCHED from database (%s)", ks["reason"])
    except Exception as e:
        logger.warning("Error rehydrating kill switch state: %s", e)


async def main():
    pool = await asyncpg.create_pool(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    redis = await aioredis.from_url(REDIS_URL)

    engine_run_id = f"RUN-{uuid.uuid4().hex[:8]}"
    
    # Initialize the paper trading engine for strategy execution
    engine = PaperTradingEngine(
        account_id=ACCOUNT_ID,
        trader_id="ORCHESTRATOR-001",
        engine_run_id=engine_run_id,
        pg_pool=pool,
        redis_client=redis,
        starting_cash=Decimal("100000.00"),
    )
    
    engine._live_gate_open = True
    engine._running = True

    quote_cache = SimpleQuoteCache()
    engine.dispatcher.set_node_cache(quote_cache)

    # Startup sweep: reset any orphaned RUNNING commands for PAPER environment
    try:
        await pool.execute(
            """
            UPDATE execution_commands
            SET status = 'PENDING',
                lease_owner = NULL,
                lease_expires_at = NULL
            WHERE environment = 'PAPER' AND status = 'RUNNING';
            """
        )
    except Exception as e:
        logger.warning("Error running startup sweep: %s", e)

    await rehydrate_dispatcher_state(pool, engine)
    # Working orders live in memory only; the ones from before this restart are gone.
    async with pool.acquire() as conn:
        await reconcile_working_orders(conn, engine)

    # Track active strategies
    active_instances = {}

    # EventBridge worker for async DB persistence
    worker = EventBridgeWorker(engine.bridge, pool, engine_run_id, batch_size=10)
    await worker.start()

    logger.info("Live Strategy Orchestrator initialized.")

    async def sync_strategies():
        """Poll the database for ACTIVE strategy deployments."""
        while True:
            try:
                rows = await pool.fetch("SELECT * FROM paper_strategy_instances WHERE status = 'RUNNING'")
                current_ids = {r["instance_id"] for r in rows}
                
                # Load new ones
                for row in rows:
                    iid = row["instance_id"]
                    if iid not in active_instances:
                        logger.info("Deploying new active strategy: %s", iid)
                        reg_entry = STRATEGY_REGISTRY.get(row["strategy_name"])
                        if reg_entry:
                            try:
                                bt_str = to_nautilus_bar_type_str(row["instrument_id"], row["timeframe"])
                                raw_params = json.loads(row["resolved_parameters"]) if isinstance(row["resolved_parameters"], str) else (row["resolved_parameters"] or {})
                                alias_map = {
                                    "oversold": "oversold_threshold",
                                    "exit_level": "exit_threshold",
                                }
                                normalized_params = {}
                                for k, v in raw_params.items():
                                    normalized_params[alias_map.get(k, k)] = v

                                cfg_cls = reg_entry["config_cls"]
                                valid_params = {"instrument_id": row["instrument_id"], "bar_type": bt_str}
                                import inspect
                                sig = inspect.signature(cfg_cls.__init__)
                                for param_name in sig.parameters:
                                    if param_name in normalized_params:
                                        valid_params[param_name] = normalized_params[param_name]

                                cfg = cfg_cls(**valid_params)
                                strategy = reg_entry["strategy_cls"](cfg)
                                _patch_strategy_for_paper_engine(strategy, engine, iid)
                                engine.register_strategy(strategy)
                                active_instances[iid] = strategy
                                logger.info("Successfully deployed %s", iid)
                            except Exception as e:
                                logger.error("Failed to deploy strategy instance %s: %s", iid, e)
                                active_instances[iid] = None
                        else:
                            logger.warning("Strategy '%s' not registered in STRATEGY_REGISTRY for instance %s", row["strategy_name"], iid)
                            active_instances[iid] = None
                            
                await asyncio.sleep(5)
            except Exception as e:
                logger.error("Error syncing strategies: %s", e)
                await asyncio.sleep(5)

    async def publish_positions():
        """Poll PostgreSQL paper_positions and publish positions snapshot to Redis."""
        while True:
            try:
                if not engine._running:
                    await asyncio.sleep(1)
                    continue
                    
                pos_list = []
                account_data = None
                async with pool.acquire() as conn:
                    acc_row = await conn.fetchrow(
                        """
                        SELECT total_equity, cash_balance, realized_pnl, daily_realized_pnl, currency
                        FROM paper_accounts
                        WHERE account_id = $1;
                        """,
                        engine.account_id
                    )
                    if acc_row:
                        account_data = {
                            "total_equity": float(acc_row["total_equity"]),
                            "cash_balance": float(acc_row["cash_balance"]),
                            "realized_pnl": float(acc_row["realized_pnl"]),
                            "daily_realized_pnl": float(acc_row["daily_realized_pnl"]),
                            "currency": str(acc_row["currency"])
                        }

                    rows = await conn.fetch(
                        """
                        SELECT position_id, account_id, instance_id, instrument_id,
                               side, entry_price, quantity, current_price, unrealized_pnl,
                               realized_pnl, opened_at
                        FROM paper_positions
                        WHERE account_id = $1 AND status = 'OPEN';
                        """,
                        engine.account_id
                    )
                    for r in rows:
                        quote = quote_cache.quote(r["instrument_id"])
                        curr_px = float(quote.bid_price) if quote else float(r["current_price"])
                        entry_px = float(r["entry_price"])
                        qty = float(r["quantity"])
                        unrealized = (-1 if r["side"] == "SHORT" else 1) * (curr_px - entry_px) * qty

                        pos_dict = {
                            "position_id": str(r["position_id"]),
                            "account_id": str(r["account_id"]),
                            "instance_id": str(r["instance_id"]),
                            "instrument_id": str(r["instrument_id"]),
                            "side": str(r["side"]),
                            "quantity": qty,
                            "entry_price": entry_px,
                            "current_price": curr_px,
                            "unrealized_pnl": unrealized,
                            "realized_pnl": float(r["realized_pnl"]),
                            "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None,
                            "status": "OPEN"
                        }
                        pos_list.append(pos_dict)
                    
                payload_dict = {"type": "positions_snapshot", "positions": pos_list}
                if account_data:
                    payload_dict["account"] = account_data
                payload_json = json.dumps(payload_dict)
                await redis.publish("CHANNEL_POSITIONS", payload_json)
                await redis.publish("channel:portfolio:positions", payload_json)
            except Exception as e:
                logger.error("Error publishing positions: %s", e)
            await asyncio.sleep(1.0)

    # Launch background tasks
    asyncio.create_task(sync_strategies())
    asyncio.create_task(command_poller(pool, engine, quote_cache))
    asyncio.create_task(consume_market_data(redis, engine, quote_cache, pool))
    asyncio.create_task(consume_perp_marks(redis, engine))
    asyncio.create_task(risk_monitor(engine, pool, redis))
    asyncio.create_task(publish_positions())

    # Stream consumer for nautilus:stream:bus
    stream_name = "nautilus:stream:bus"
    group_name = "orchestrator_group"
    consumer_name = f"orch_{uuid.uuid4().hex[:6]}"
    
    try:
        await redis.xgroup_create(stream_name, group_name, mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            logger.warning("Group create warning: %s", e)

    logger.info("Listening for live bars on %s...", stream_name)

    while True:
        try:
            messages = await redis.xreadgroup(
                group_name, consumer_name, {stream_name: ">"}, count=10, block=2000
            )
            for stream, msgs in messages:
                for msg_id, msg_data in msgs:
                    if b"type" in msg_data and msg_data[b"type"] == b"Bar":
                        payload_json = msg_data[b"payload"].decode("utf-8")
                        try:
                            bar_obj = Bar.from_json_bytes(payload_json.encode('utf-8'))
                            engine.on_live_bar(bar_obj)
                        except Exception:
                            payload_dict = json.loads(payload_json)
                            bt = BarType.from_str(payload_dict["bar_type"])
                            b = Bar(
                                bar_type=bt,
                                open=Price(Decimal(str(payload_dict["open"])), 2),
                                high=Price(Decimal(str(payload_dict["high"])), 2),
                                low=Price(Decimal(str(payload_dict["low"])), 2),
                                close=Price(Decimal(str(payload_dict["close"])), 2),
                                volume=Quantity(Decimal(str(payload_dict["volume"])), 4),
                                ts_event=int(payload_dict["ts_event"]),
                                ts_init=int(payload_dict["ts_init"])
                            )
                            engine.on_live_bar(b)

                    await redis.xack(stream_name, group_name, msg_id)
                    
        except Exception as e:
            logger.error("Error in stream reader loop: %s", e)
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
