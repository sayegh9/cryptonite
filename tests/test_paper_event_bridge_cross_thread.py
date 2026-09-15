"""Test cross-thread EventBridge, fail-closed overflow, and IntegritySupervisor persistence."""

import asyncio
import threading
import time
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptonite_execution.bridge import (
    EventBridge,
    EventBridgeWorker,
    IntegritySupervisor,
)
from cryptonite_execution.kill_switch import LatchedKillSwitch


def test_event_bridge_watermarks_and_zero_db_overflow_fail_closed():
    """Verify that node thread callbacks never perform DB I/O, trip kill latch on 100% cap,
    set atomic INTEGRITY_FAILED flag, and signal the supervisor.
    """
    kill_switch = LatchedKillSwitch()
    bridge = EventBridge(kill_switch=kill_switch, capacity=10, degraded_watermark_pct=0.80)

    # 1. Enqueue 7 events: normal state
    for i in range(7):
        ok = bridge.enqueue_event({"id": i})
        assert ok is True
    assert not bridge.is_degraded
    assert not bridge.integrity_failed_event.is_set()
    assert not kill_switch.is_latched

    # 2. Enqueue 8th event: crosses 80% watermark -> DEGRADED
    bridge.enqueue_event({"id": 7})
    assert bridge.is_degraded is True
    assert not bridge.integrity_failed_event.is_set()

    # 3. Fill up to 10 events (100% capacity)
    bridge.enqueue_event({"id": 8})
    bridge.enqueue_event({"id": 9})
    assert bridge.queue.qsize() == 10

    # 4. Enqueue 11th event: hard-cap overflow
    # Node callback MUST NOT perform any DB I/O, but MUST fail closed immediately
    ok = bridge.enqueue_event({"id": 10})
    assert ok is False
    assert bridge.integrity_failed_event.is_set() is True
    assert kill_switch.is_latched is True
    assert bridge.supervisor_signal.is_set() is True
    assert bridge.incident_snapshot is not None
    assert bridge.incident_snapshot["reason"] == "QUEUE_OVERFLOW_FAIL_CLOSED"
    assert bridge.dropped_events_count == 1


@pytest.mark.asyncio
async def test_integrity_supervisor_persists_incident_to_postgres():
    """Verify that IntegritySupervisor asynchronously persists the incident snapshot to Postgres."""
    kill_switch = LatchedKillSwitch()
    bridge = EventBridge(kill_switch=kill_switch, capacity=5)

    mock_pool = MagicMock()
    mock_pool.execute = AsyncMock()

    supervisor = IntegritySupervisor(
        bridge=bridge,
        pg_pool=mock_pool,
        engine_run_id="ENGINE-RUN-TEST",
    )

    # Launch supervisor
    await supervisor.start()

    # Simulate overflow from another thread
    for i in range(6):
        bridge.enqueue_event({"event_index": i})

    assert bridge.supervisor_signal.is_set()

    # Wait briefly for supervisor loop to process signal
    await asyncio.sleep(0.2)
    await supervisor.stop()

    # Assert supervisor executed DB insert into integrity_incidents
    mock_pool.execute.assert_called_once()
    args = mock_pool.execute.call_args[0]
    assert "INSERT INTO integrity_incidents" in args[0]
    assert args[2] == "ENGINE-RUN-TEST"
    assert args[3] == "QUEUE_OVERFLOW_FAIL_CLOSED"


@pytest.mark.asyncio
async def test_event_bridge_worker_drains_micro_batches_atomically():
    """Verify that EventBridgeWorker drains the thread-safe queue and commits batch in 1 DB tx."""
    kill_switch = LatchedKillSwitch()
    bridge = EventBridge(kill_switch=kill_switch, capacity=100)

    # Populate 3 events from simulated node thread
    bridge.enqueue_event({
        "event_id": uuid.uuid4(),
        "native_event_id": "NAT-001",
        "client_order_id": "ORD-001",
        "event_version": 1,
        "event_type": "ORDER_ACCEPTED",
        "status": "ACCEPTED",
    })
    bridge.enqueue_event({
        "event_id": uuid.uuid4(),
        "native_event_id": "NAT-002",
        "client_order_id": "ORD-001",
        "event_version": 2,
        "event_type": "ORDER_FILLED",
        "status": "FILLED",
        "account_id": "PAPER-ACCOUNT-001",
        "instance_id": "INST-001",
        "instrument_id": "BTCUSDT.BINANCE",
        "side": "BUY",
        "price": Decimal("50000.00"),
        "quantity": Decimal("0.1000"),
        "commission": Decimal("5.00"),
    })

    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock()
    mock_tx.__aexit__ = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=mock_tx)
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    worker = EventBridgeWorker(
        bridge=bridge,
        pg_pool=mock_pool,
        engine_run_id="ENGINE-RUN-001",
        batch_size=10,
    )

    committed = await worker.drain_and_commit_batch()
    assert committed == 2
    assert worker.committed_events_count == 2
    assert bridge.queue.empty()

    # Verify execution calls inside transaction
    assert mock_conn.execute.call_count >= 2
