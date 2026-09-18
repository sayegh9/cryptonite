import asyncio
import json
import logging
import os
import uuid
from decimal import Decimal
import time

import asyncpg
import redis.asyncio as aioredis

from cryptonite_execution.engine import PaperTradingEngine
from cryptonite_execution.risk import OrderIntent
from cryptonite_strategies.registry import STRATEGY_REGISTRY
from nautilus_trader.model import Bar, BarType
from nautilus_trader.model import Price, Quantity

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("cryptonite.orchestrator")

DATABASE_URL = os.getenv("ASYNC_DATABASE_URL", "postgresql+asyncpg://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
ENVIRONMENT = os.getenv("ENVIRONMENT", "DEMO").upper()
ACCOUNT_ID = os.getenv("ACCOUNT_ID", "DEMO-ACCOUNT-001")


def _patch_strategy_for_paper_engine(strategy, engine, instance_id: str):
    """Monkey-patch Nautilus Strategy to route submit_order to PaperTradingEngine dispatcher."""
    def _submit_order(order):
        intent = OrderIntent(
            client_order_id=order.client_order_id.value,
            instance_id=instance_id,
            instrument_id=str(order.instrument_id),
            side="BUY" if order.side.value == 1 else "SELL", # OrderSide.BUY=1
            order_type="MARKET" if getattr(order, "order_type", None) and order.order_type.value == 1 else "LIMIT",
            quantity=Decimal(str(order.quantity.as_double())),
            price=Decimal(str(order.price.as_double())) if getattr(order, "price", None) else None,
        )
        def _on_submit(naut_order):
            pass  # The dispatcher handles the fill internally in PaperTradingEngine
            
        engine.dispatcher.dispatch(intent, _on_submit, nautilus_order=order)

    strategy.submit_order = _submit_order
    # Also patch buy_market / sell_market to avoid crashing without a node
    def _buy_market(qty=None):
        qty_dec = Decimal(str(qty.as_double())) if qty else Decimal(str(strategy.trade_size))
        intent = OrderIntent(
            client_order_id=f"O-{uuid.uuid4().hex[:8]}",
            instance_id=instance_id,
            instrument_id=str(strategy.instrument_id),
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
            instrument_id=str(strategy.instrument_id),
            side="SELL",
            order_type="MARKET",
            quantity=qty_dec,
        )
        engine.dispatcher.dispatch(intent, lambda o: None, nautilus_order=None)

    strategy.buy_market = _buy_market
    strategy.sell_market = _sell_market
    return strategy


async def command_poller(pool, engine):
    """Poll execution_commands for DEPLOY, STOP, PAUSE, RESUME, CLOSE_POSITION, MANUAL_ORDER."""
    while True:
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow("""
                        WITH claimed AS (
                            SELECT command_id FROM execution_commands
                            WHERE status = 'PENDING'
                              AND command_type IN ('DEPLOY', 'STOP', 'PAUSE', 'RESUME', 'CLOSE_POSITION', 'MANUAL_ORDER')
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
                        
                        logger.info("Orchestrator processing %s for %s", cmd_type, target)
                        
                        if cmd_type == "DEPLOY":
                            # Insert into paper_strategy_instances
                            await conn.execute("""
                                INSERT INTO paper_strategy_instances (
                                    instance_id, account_id, strategy_name, strategy_version_id,
                                    strategy_source_hash, risk_policy_hash, last_processed_bar_ns,
                                    instrument_id, timeframe, resolved_parameters, allocation_usd,
                                    status, created_at, updated_at
                                ) VALUES ($1, $2, $3, $4::uuid, 'dummy_hash', 'dummy_hash', 0, $5, $6, $7::jsonb, $8, 'RUNNING', now(), now())
                                ON CONFLICT (instance_id) DO UPDATE SET status = 'RUNNING', updated_at = now();
                            """,
                                target, payload["account_id"], payload["strategy_name"],
                                payload["strategy_version_id"], payload["instrument_id"],
                                payload["timeframe"], json.dumps(payload.get("parameters", {})),
                                payload.get("allocation_usd", Decimal("0"))
                            )
                        elif cmd_type in ("STOP", "PAUSE"):
                            await conn.execute("UPDATE paper_strategy_instances SET status = $2, updated_at = now() WHERE instance_id = $1", target, cmd_type)
                            engine.mailbox.push({"command_type": cmd_type, "target_instance_id": target})
                            engine.process_mailbox_commands()
                        elif cmd_type == "RESUME":
                            await conn.execute("UPDATE paper_strategy_instances SET status = 'RUNNING', updated_at = now() WHERE instance_id = $1", target)
                        elif cmd_type == "CLOSE_POSITION":
                            engine.mailbox.push({"command_type": cmd_type, "target_instance_id": target, "payload": payload})
                            engine.process_mailbox_commands()
                        elif cmd_type == "MANUAL_ORDER":
                            intent = OrderIntent(
                                client_order_id=payload.get("client_order_id", f"MANUAL-{uuid.uuid4().hex[:8]}"),
                                instance_id="MANUAL_TRADE",
                                instrument_id=payload["instrument_id"],
                                side=payload["side"].upper(),
                                order_type=payload.get("order_type", "MARKET").upper(),
                                quantity=Decimal(str(payload["quantity"])),
                                price=Decimal(str(payload["price"])) if payload.get("price") else None,
                            )
                            engine.dispatcher.dispatch(intent, lambda o: None, nautilus_order=None)
                            
                        # Mark command COMPLETED
                        await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', processed_at = now() WHERE command_id = $1", row["command_id"])
                        
        except Exception as e:
            logger.error("Error in command poller: %s", e)
        await asyncio.sleep(0.1)


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

    # Track active strategies
    active_instances = {}

    # EventBridge worker for async DB persistence
    from cryptonite_execution.bridge import EventBridgeWorker
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
                            cfg = reg_entry["config_cls"](
                                instrument_id=row["instrument_id"],
                                bar_type=row["timeframe"],
                                **(json.loads(row["resolved_parameters"]) if isinstance(row["resolved_parameters"], str) else row["resolved_parameters"])
                            )
                            strategy = reg_entry["strategy_cls"](cfg)
                            _patch_strategy_for_paper_engine(strategy, engine, iid)
                            engine.register_strategy(strategy)
                            active_instances[iid] = strategy
                            logger.info("Successfully deployed %s", iid)
                            
                await asyncio.sleep(5)
            except Exception as e:
                logger.error("Error syncing strategies: %s", e)
                await asyncio.sleep(5)

    asyncio.create_task(sync_strategies())
    asyncio.create_task(command_poller(pool, engine))

    async def publish_positions():
        """Poll Nautilus portfolio and publish positions to Redis"""
        while True:
            try:
                if not engine._running:
                    await asyncio.sleep(1)
                    continue
                    
                pos_list = []
                for p in engine.node.portfolio.positions():
                    if p.is_closed:
                        continue
                        
                    # Calculate unrealized pnl by calling calculate_pnl() with latest price if available
                    # Actually, we can just use the last mark price if we have it, or let the frontend do it
                    # But we'll just send entry price and the frontend can compute it with live ticker.
                    
                    pos_dict = {
                        "position_id": str(p.id.value) if hasattr(p.id, "value") else str(p.id),
                        "account_id": str(p.account_id.value) if hasattr(p.account_id, "value") else str(p.account_id),
                        "instance_id": str(p.strategy_id.value) if hasattr(p.strategy_id, "value") else str(p.strategy_id),
                        "instrument_id": str(p.instrument_id.value) if hasattr(p.instrument_id, "value") else str(p.instrument_id),
                        "side": str(p.side.name) if hasattr(p.side, "name") else str(p.side),
                        "quantity": float(p.quantity.as_double()) if hasattr(p.quantity, "as_double") else float(p.quantity),
                        "entry_price": float(p.avg_px_open.as_double()) if hasattr(p.avg_px_open, "as_double") else float(p.avg_px_open),
                        "opened_at_ns": int(p.ts_opened),
                    }
                    pos_list.append(pos_dict)
                    
                # Publish the full snapshot to Redis
                await redis.publish("CHANNEL_POSITIONS", json.dumps({"type": "positions_snapshot", "positions": pos_list}))
            except Exception as e:
                logger.error(f"Error publishing positions: {e}")
            await asyncio.sleep(1.0)
            
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
            # Read from stream
            messages = await redis.xreadgroup(
                group_name, consumer_name, {stream_name: ">"}, count=10, block=2000
            )
            for stream, msgs in messages:
                for msg_id, msg_data in msgs:
                    # Deserialize Bar
                    if b"type" in msg_data and msg_data[b"type"] == b"Bar":
                        payload_json = msg_data[b"payload"].decode("utf-8")
                        try:
                            # Use nautilus Bar.from_json or custom parse if unavailable
                            bar_obj = Bar.from_json_bytes(payload_json.encode('utf-8'))
                            engine.on_live_bar(bar_obj)
                        except Exception:
                            # Fallback if Bar.from_json_bytes fails due to nautilus version weirdness
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

                    # Acknowledge message
                    await redis.xack(stream_name, group_name, msg_id)
                    
        except Exception as e:
            logger.error("Error in stream reader loop: %s", e)
            await asyncio.sleep(1)




if __name__ == "__main__":
    asyncio.run(main())
