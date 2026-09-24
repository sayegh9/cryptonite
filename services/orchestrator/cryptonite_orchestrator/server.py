import asyncio
import contextlib
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
from cryptonite_execution.risk import OrderIntent
from pydantic import ValidationError
from cryptonite_strategies.registry import STRATEGY_REGISTRY
from cryptonite_execution.bridge import EventBridgeWorker
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


def _patch_strategy_for_paper_engine(strategy, engine, instance_id: str):
    """Monkey-patch Nautilus Strategy to route submit_order to PaperTradingEngine dispatcher."""
    def _submit_order(order):
        intent = OrderIntent(
            client_order_id=order.client_order_id.value,
            instance_id=instance_id,
            instrument_id=to_canonical_instrument(str(order.instrument_id)),
            side="BUY" if order.side.value == 1 else "SELL",  # OrderSide.BUY=1
            order_type="MARKET" if getattr(order, "order_type", None) and order.order_type.value == 1 else "LIMIT",
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
    return strategy


async def consume_market_data(redis, engine, quote_cache):
    """Consumes ui_quote_tick and ingress_health streams to maintain live cache and telemetry."""
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
                              AND command_type IN ('DEPLOY', 'STOP', 'PAUSE', 'RESUME', 'CLOSE_POSITION', 'MANUAL_ORDER', 'SUBMIT_ORDER', 'CANCEL_ORDER')
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
                            pos_qty = engine.dispatcher.get_position_qty(canonical_inst)
                            perp_qty = engine.dispatcher.get_perp_position_qty(canonical_inst)
                            if pos_qty > Decimal("0") or perp_qty != 0:
                                # Spot long closes with a SELL; a perp closes with an opposing reduce-only order
                                close_side = "BUY" if perp_qty < 0 else "SELL"
                                close_qty = abs(perp_qty) if perp_qty != 0 else pos_qty
                                quote = quote_cache.quote(canonical_inst)
                                if quote:
                                    fill_px = Decimal(str(quote.ask_price if close_side == "BUY" else quote.bid_price))
                                else:
                                    fill_px = Decimal(str(payload.get("price") or "60000.00"))
                                comm = close_qty * fill_px * Decimal("0.001")
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
                                        fill_qty=pos_qty,
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
                        elif cmd_type in ("MANUAL_ORDER", "SUBMIT_ORDER"):
                            inst_raw = payload.get("instrument_id") or payload.get("symbol", "BTCUSDT")
                            canonical_inst = to_canonical_instrument(inst_raw)
                            
                            # Determine side
                            raw_side = payload.get("side")
                            if not raw_side:
                                rec = payload.get("recommendation", "").upper()
                                raw_side = "BUY" if rec in ("BUY", "OVERWEIGHT") else "SELL"
                            side = raw_side.upper()
                            
                            # Determine quantity
                            qty_val = payload.get("quantity")
                            if not qty_val:
                                alloc_pct = Decimal(str(payload.get("allocation_pct", 5.0)))
                                quote = quote_cache.quote(canonical_inst)
                                mark_px = Decimal(str(quote.ask_price if side == "BUY" else quote.bid_price)) if quote else Decimal("60000.00")
                                notional = engine.dispatcher.cash_balance * (alloc_pct / Decimal("100.0"))
                                qty_val = max(Decimal("0.0001"), notional / mark_px)
                            qty_dec = Decimal(str(qty_val))
                            
                            price_dec = Decimal(str(payload["price"])) if payload.get("price") else None
                            order_type = payload.get("order_type", "MARKET").upper()
                            client_oid = payload.get("client_order_id", f"{'MANUAL' if cmd_type == 'MANUAL_ORDER' else 'AI'}-{uuid.uuid4().hex[:8].upper()}")
                            instance_id = target or ("MANUAL_TRADE" if cmd_type == "MANUAL_ORDER" else "AI_DEBATE_WORKER")
                            
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
                                quote = quote_cache.quote(canonical_inst)
                                if side == "BUY":
                                    fill_px = Decimal(str(quote.ask_price)) if quote else (price_dec or Decimal("60000.00"))
                                else:
                                    fill_px = Decimal(str(quote.bid_price)) if quote else (price_dec or Decimal("60000.00"))
                                
                                comm = qty_dec * fill_px * Decimal("0.001")
                                
                                # Process in dispatcher in-memory ledger
                                realized = engine.dispatcher.handle_fill(
                                    client_order_id=client_oid,
                                    fill_qty=qty_dec,
                                    fill_price=fill_px,
                                    commission=comm,
                                    side=side,
                                    instrument_id=canonical_inst,
                                )
                                
                                # Insert order into paper_orders
                                await conn.execute(
                                    """
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
                                    """,
                                    client_oid, instance_id, acc_id, canonical_inst,
                                    side, order_type, qty_dec, price_dec, fill_px
                                )
                                
                                # Enqueue ORDER_FILLED event into bridge
                                engine.bridge.enqueue_event({
                                    "event_type": "ORDER_FILLED",
                                    "event_version": 1,
                                    "client_order_id": client_oid,
                                    "account_id": acc_id,
                                    "instance_id": instance_id,
                                    "instrument_id": canonical_inst,
                                    "side": side,
                                    "price": str(fill_px),
                                    "quantity": str(qty_dec),
                                    "commission": str(comm),
                                    "filled_qty": str(qty_dec),
                                    "avg_fill_price": str(fill_px),
                                    "status": "FILLED",
                                    "occurred_at_ns": time.time_ns(),
                                    **engine.dispatcher.fill_event_fields(canonical_inst, realized, fill_px),
                                })
                                
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
                                logger.info("Order %s filled successfully at %s", client_oid, fill_px)
                        
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
    asyncio.create_task(consume_market_data(redis, engine, quote_cache))
    asyncio.create_task(consume_perp_marks(redis, engine))
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
