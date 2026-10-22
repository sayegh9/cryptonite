"""Reduce-only close commands for a mandate's open positions (hard stop, daily latch, profile stop).

Moved out of the orchestrator's mandate monitor so packages (``profile_lifecycle``) can queue closes without importing a
service; ``mandate_monitor`` re-exports both names.
"""

import json
import time
import uuid
from decimal import Decimal

CLOSE_RETRY_AFTER_S = 30


async def enqueue_flatten(conn, instance_id: str, instrument_id: str, quantity: Decimal, reason: str,
                          mandate_id: uuid.UUID | None, account_id: str) -> bool:
    """One risk-reducing CLOSE_POSITION per open position; a rejected close is retried after a pause."""
    prefix = f"mandate_stop:{instance_id}:{instrument_id}"
    last = await conn.fetchrow(
        "SELECT status, created_at FROM execution_commands WHERE idempotency_key LIKE $1 || ':%' ORDER BY created_at DESC LIMIT 1",
        prefix,
    )
    if last is not None:
        if last["status"] not in ("REJECTED", "FAILED", "COMPLETED"):
            return False  # still pending or running
        if time.time() - last["created_at"].timestamp() < CLOSE_RETRY_AFTER_S:
            return False  # a completed close needs a moment to reach paper_positions
    attempt = await conn.fetchval("SELECT count(*) FROM execution_commands WHERE idempotency_key LIKE $1 || ':%'", prefix)
    await conn.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, requester_id,
               payload, status, environment, account_id, mandate_id)
           VALUES ($1, $2, 'CLOSE_POSITION', $3, 'risk:mandate_monitor', $4::jsonb, 'PENDING', 'PAPER', $5, $6)
           ON CONFLICT (idempotency_key) DO NOTHING""",
        uuid.uuid4(), f"{prefix}:{attempt}", instance_id,
        json.dumps({"instrument_id": instrument_id, "quantity": str(quantity), "reason": reason,
                    "mandate_id": str(mandate_id) if mandate_id else None, "reduce_only": True}),
        account_id, mandate_id,
    )
    return True


async def flatten_instance(conn, instance_id: str, reason: str, mandate_id: uuid.UUID | None, account_id: str) -> int:
    rows = await conn.fetch(
        "SELECT instrument_id, quantity FROM paper_positions WHERE instance_id = $1 AND status = 'OPEN' AND quantity > 0",
        instance_id,
    )
    n = 0
    for r in rows:
        n += await enqueue_flatten(conn, instance_id, r["instrument_id"], Decimal(str(r["quantity"])), reason, mandate_id, account_id)
    return n
