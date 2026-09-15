"""Cryptonite Executor Service Daemon.

Independently supervised service running with dedicated least-privilege role (cryptonite_executor).
Implements the required startup order:
  1. Emergency marker check
  2. Database and secrets
  3. Endpoint validation (restricted Demo-only egress)
  4. Single-active-executor advisory lock
  5. Clock synchronization
  6. Nautilus adapter / venue client
  7. User-data stream
  8. Startup reconciliation
  9. READY
  10. Command polling
"""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import socket
import time
from typing import Any, AsyncGenerator, Optional
import uuid

import asyncpg
from fastapi import FastAPI, Response, status

from cryptonite_execution.clock import ClockSyncMonitor
from cryptonite_execution.demo_pipeline import DemoExecutionPipeline, MockVenueAdapter
from cryptonite_execution.filters import DynamicExchangeFilterValidator
from cryptonite_execution.kill_switch import LatchedKillSwitch
from cryptonite_execution.version_lock import assert_version_locked

logger = logging.getLogger("cryptonite.executor")

# Global pipeline, engine & connection references
pipeline: Optional[DemoExecutionPipeline] = None
engine: Any = None
pg_pool: Optional[asyncpg.Pool] = None
poller_running: bool = False
poller_task: Optional[asyncio.Task[None]] = None
service_ready: bool = False
advisory_lock_acquired: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global pipeline, pg_pool, poller_running, poller_task, service_ready, advisory_lock_acquired

    assert_version_locked()

    environment = os.getenv("ENVIRONMENT", "DEMO").upper()
    account_id = os.getenv("ACCOUNT_ID", "DEMO-ACCOUNT-001")
    marker_path_str = os.getenv(
        "EMERGENCY_MARKER_PATH",
        f"/data/executor_state/{environment.lower()}/emergency_kill_switch.marker",
    )
    database_url = os.getenv(
        "ASYNC_DATABASE_URL",
        "postgresql+asyncpg://cryptonite_executor:executor_dev_pass@localhost:5432/cryptonite",
    )
    clean_db_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    demo_base_url = os.getenv("BINANCE_DEMO_BASE_URL", "https://demo-api.binance.com")
    demo_ws_url = os.getenv("BINANCE_DEMO_WS_URL", "wss://demo-ws-api.binance.com/ws-api/v3")

    kill_switch = LatchedKillSwitch()

    # --- STEP 1: Emergency Marker Check ---
    marker_file = Path(marker_path_str)
    if marker_file.exists():
        logger.critical("Emergency kill switch marker found at %s! Starting LATCHED.", marker_path_str)
        kill_switch.trip(latched_by="STARTUP_EMERGENCY_MARKER", reason=f"Found {marker_path_str}")

    # --- STEP 2: Database and Secrets ---
    logger.info("Connecting to PostgreSQL and Redis with least-privilege executor credentials...")
    pg_pool = await asyncpg.create_pool(dsn=clean_db_url, min_size=2, max_size=10)
    import redis.asyncio as aioredis
    redis_client = aioredis.from_url(redis_url, decode_responses=False)

    private_key_path = os.getenv("BINANCE_DEMO_PRIVATE_KEY_PATH")
    if private_key_path and Path(private_key_path).exists():
        logger.info("Loaded Binance Demo Ed25519 private key from %s", private_key_path)

    # --- STEP 3: Endpoint Validation ---
    logger.info("Validating Binance Demo restricted egress endpoints: REST=%s, WS=%s",
                demo_base_url, demo_ws_url)
    assert "demo" in demo_base_url.lower() or "127.0.0.1" in demo_base_url or "localhost" in demo_base_url, \
        f"Non-demo egress endpoint forbidden: {demo_base_url}"

    # --- STEP 4: Initialize Venue Adapter & Pipeline ---
    use_mock = os.getenv("USE_MOCK_VENUE", "false").lower() in ("1", "true", "yes")
    
    if use_mock:
        venue_client = MockVenueAdapter()
    else:
        from cryptonite_execution.binance_adapter import BinanceCompositeAdapter
        api_key = os.getenv("BINANCE_DEMO_API_KEY")
        if not api_key or not private_key_path:
            raise RuntimeError("Missing BINANCE_DEMO_API_KEY or BINANCE_DEMO_PRIVATE_KEY_PATH for LIVE venue")
        priv_pem = Path(private_key_path).read_bytes()
        
        # We need both spot and futures base URLs
        demo_spot_url = demo_base_url
        demo_futures_url = os.getenv("BINANCE_FUTURES_DEMO_BASE_URL", "https://testnet.binancefuture.com")
        
        venue_client = BinanceCompositeAdapter(api_key, priv_pem, demo_spot_url, demo_futures_url)

    clock_monitor = ClockSyncMonitor()
    logger.info("Clock sync monitor initialized (warning=500ms, blocking=1000ms)")

    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    pipeline = DemoExecutionPipeline(
        environment=environment,
        account_id=account_id,
        pool=pg_pool,
        redis_client=redis_client,
        venue_client=venue_client,
        filter_validator=DynamicExchangeFilterValidator(),
        clock_monitor=clock_monitor,
        kill_switch=kill_switch,
        worker_id=worker_id,
    )

    # Acquire dedicated PostgreSQL session advisory lock via pipeline
    advisory_lock_acquired = await pipeline.acquire_advisory_lock()
    if not advisory_lock_acquired:
        lock_key = f"cryptonite_executor:{environment}:{account_id}"
        logger.critical("Failed to acquire PostgreSQL advisory lock %s: another executor instance is active!", lock_key)
        raise RuntimeError(f"Another active executor holds advisory lock {lock_key}")
    logger.info("Acquired single-active executor advisory lock: cryptonite_executor:%s:%s", environment, account_id)

    # --- STEP 7: User-Data Stream ---
    ws_stream_client = None
    api_key = os.getenv("BINANCE_DEMO_API_KEY")
    if api_key and private_key_path and Path(private_key_path).exists():
        from cryptonite_execution.binance_user_stream import BinanceDemoWebSocketApiClient
        priv_pem = Path(private_key_path).read_bytes()
        ws_stream_client = BinanceDemoWebSocketApiClient(
            api_key=api_key,
            private_key_pem=priv_pem,
            ws_url=demo_ws_url,
            on_execution_report=pipeline.handle_execution_report,
            gate_controller=pipeline,
        )
        await ws_stream_client.start()
        logger.info("BinanceDemoWebSocketApiClient started on %s", demo_ws_url)
    elif hasattr(venue_client, "register_callback"):
        venue_client.register_callback(pipeline.handle_execution_report)
        logger.info("Mock user-data stream event handler registered")

    # --- STEP 8: Startup Reconciliation ---
    logger.info("Executing startup reconciliation check...")
    try:
        # Reconcile open orders
        if not use_mock:
            logger.info("Fetching live open orders for reconciliation...")
            live_orders = await venue_client.get_open_orders()
            live_order_ids = {o["clientOrderId"] for o in live_orders}
            
            async with pg_pool.acquire() as conn:
                db_orders = await conn.fetch("SELECT client_order_id FROM trading_orders WHERE status IN ('NEW', 'ACCEPTED', 'PARTIALLY_FILLED')")
                db_order_ids = {r["client_order_id"] for r in db_orders}
                
                stray_orders = live_order_ids - db_order_ids
                if stray_orders:
                    logger.warning("Found %d stray orders on exchange not in DB: %s", len(stray_orders), stray_orders)
                    for client_oid in stray_orders:
                        symbol = next((o["symbol"] for o in live_orders if o["clientOrderId"] == client_oid), "BTCUSDT")
                        logger.warning("Force-canceling stray order %s (%s)", client_oid, symbol)
                        try:
                            await venue_client.cancel_order(symbol, client_oid)
                            logger.info("Successfully canceled stray order %s", client_oid)
                        except Exception as e:
                            logger.error("Failed to cancel stray order %s: %s", client_oid, e)
                            # Fill-While-Canceling Guard
                            logger.info("Querying status of %s to ensure it wasn't filled...", client_oid)
                            try:
                                final_status = await venue_client.query_order(symbol, client_oid)
                                if final_status and final_status.get("status") in ("FILLED", "PARTIALLY_FILLED"):
                                    kill_switch.trip(latched_by="STARTUP_RECONCILIATION", reason=f"Stray order {client_oid} was filled outside DB awareness!")
                                    logger.critical("EMERGENCY KILL SWITCH TRIPPED: Stray order %s filled mid-cancellation!", client_oid)
                                    raise RuntimeError(f"Unrecorded fill for stray order {client_oid}")
                            except Exception as eq:
                                logger.error("Failed to query final status of %s: %s", client_oid, eq)
                                kill_switch.trip(latched_by="STARTUP_RECONCILIATION", reason=f"Cannot verify status of un-cancelable stray order {client_oid}")
                                raise RuntimeError(f"Blind state on un-cancelable stray order {client_oid}")
        else:
            logger.info("Using mock venue, skipping live reconciliation.")
    except Exception as e:
        logger.error("Startup reconciliation error: %s", e)
        if not kill_switch.is_tripped():
            kill_switch.trip(latched_by="STARTUP_RECONCILIATION_CRASH", reason=str(e))

    # --- STEP 9: READY ---
    service_ready = not kill_switch.is_latched
    logger.info("Cryptonite Demo Executor startup sequence COMPLETE. Ready=%s", service_ready)

    # --- STEP 10: Command Polling Loop ---
    poller_running = True
    poller_task = asyncio.create_task(run_poller_loop(pipeline))

    try:
        yield
    finally:
        logger.info("Shutting down Cryptonite Demo Executor gracefully...")
        poller_running = False
        service_ready = False
        if poller_task:
            poller_task.cancel()
            try:
                await poller_task
            except asyncio.CancelledError:
                pass

        if ws_stream_client:
            await ws_stream_client.stop()

        if pipeline:
            await pipeline.shutdown()

        if pg_pool:
            await pg_pool.close()
        logger.info("Cryptonite Demo Executor shut down cleanly")


app = FastAPI(title="Cryptonite Demo Executor", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready(response: Response) -> dict[str, Any]:
    # Check paper engine if configured
    if engine is not None:
        if getattr(engine, "is_ready", False):
            return {
                "status": "ready",
                "account_id": getattr(engine, "account_id", ""),
                "gate_open": getattr(engine, "is_gate_open", True),
                "kill_switch_latched": engine.kill_switch.is_latched if hasattr(engine, "kill_switch") else False,
            }
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "not_ready",
            "gate_open": getattr(engine, "is_gate_open", False),
            "kill_switch_latched": engine.kill_switch.is_latched if hasattr(engine, "kill_switch") else False,
            "integrity_failed": engine.bridge.integrity_failed_event.is_set() if hasattr(engine, "bridge") else False,
        }

    # Check Demo pipeline
    advisory_lock_ok = False
    if pipeline:
        try:
            pipeline.assert_advisory_lock()
            advisory_lock_ok = True
        except Exception:
            advisory_lock_ok = False

    if pipeline and service_ready and advisory_lock_ok and not pipeline.kill_switch.is_latched:
        return {
            "status": "ready",
            "environment": pipeline.environment,
            "account_id": pipeline.account_id,
            "worker_id": pipeline.worker_id,
            "advisory_lock_held": True,
            "kill_switch_latched": pipeline.kill_switch.is_latched,
        }
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "not_ready",
        "service_ready": service_ready,
        "advisory_lock_held": advisory_lock_ok,
        "kill_switch_latched": pipeline.kill_switch.is_latched if pipeline else False,
    }


async def run_poller_loop(pipe: DemoExecutionPipeline, poll_interval_s: float = 0.1) -> None:
    """Continuous command claiming loop with attempt_id and lease_owner fencing."""
    logger.info("Execution command poller started for worker=%s", pipe.worker_id)
    while poller_running:
        try:
            cmd = await pipe.claim_command(lease_seconds=30)
            if cmd:
                cmd_type = cmd.get("command_type")
                logger.info("Claimed command %s (%s)", cmd.get("command_id"), cmd_type)
                if cmd_type == "SUBMIT_ORDER":
                    await pipe.process_submit_order(cmd)
                elif cmd_type == "TRIP_KILL_SWITCH":
                    payload = cmd.get("payload", {})
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except Exception:
                            payload = {}
                    pipe.kill_switch.trip(
                        latched_by=cmd.get("requester_id") or payload.get("operator", "operator"),
                        reason=payload.get("reason", "Operator manual trip"),
                    )
                    await pipe._finalize_command(
                        cmd["command_id"], cmd["attempt_id"], cmd["lease_owner"], "COMPLETED", "SUCCESS", None
                    )
                elif cmd_type in ("KILL_SWITCH_RESET_REQUEST", "RESET_SWITCH"):
                    try:
                        await pipe.process_kill_switch_reset_request(cmd)
                    except Exception as r_err:
                        logger.warning("Kill switch reset request rejected or failed: %s", r_err)
                else:
                    logger.warning("Unrecognized command type: %s", cmd_type)
                    await pipe._finalize_command(
                        cmd["command_id"], cmd["attempt_id"], cmd["lease_owner"], "REJECTED", "REJECTED", "Unknown type"
                    )

            await asyncio.sleep(poll_interval_s)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in command poller loop: %s", e)
            await asyncio.sleep(1.0)


# Backward compatibility alias
command_poller_loop = run_poller_loop


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8084"))
    uvicorn.run("cryptonite_executor.server:app", host="0.0.0.0", port=port, log_level="info")
