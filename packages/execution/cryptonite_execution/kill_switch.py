"""In-memory latched emergency kill switch with audited reset constraints."""

import threading
import time
import os
import urllib.request
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

def send_alert(message: str) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    def _send():
        try:
            req = urllib.request.Request(
                SLACK_WEBHOOK_URL,
                data=json.dumps({"text": message}).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=5.0)
        except Exception as e:
            logger.error(f"Failed to send Slack alert: {e}")
    threading.Thread(target=_send, daemon=True).start()


class LatchedKillSwitch:
    """Thread-safe latched execution kill switch.
    
    Once tripped, rejects all new order submissions except internal reduce-only
    liquidation/flatten orders. Can only be reset when all open orders and positions are zero.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._is_latched: bool = False
        self._latched_at_ns: int | None = None
        self._latched_by: str | None = None
        self._reason: str | None = None
        self._reset_at_ns: int | None = None
        self._reset_by: str | None = None
        
        self._pool: Any = None
        self._environment: Optional[str] = None
        self._account_id: Optional[str] = None
        self._cache_updated_at: float = 0.0
        self._generation: int = 0

    def configure(self, pool: Any, environment: str, account_id: str) -> None:
        with self._lock:
            self._pool = pool
            self._environment = environment
            self._account_id = account_id
            self._cache_updated_at = 0.0

    @property
    def is_latched(self) -> bool:
        """Backward-compatible sync property. Unsafe for async strict fencing."""
        with self._lock:
            return self._is_latched

    async def is_latched_async(self) -> bool:
        """Fail-Closed Cached Fencing check."""
        now = time.monotonic()
        
        with self._lock:
            if self._is_latched:
                return True
            if self._pool and (now - self._cache_updated_at) <= 0.05:
                return False
                
        if not self._pool:
            # If not configured with DB, fallback to memory state
            with self._lock:
                return self._is_latched
                
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT is_latched, latch_generation FROM execution_kill_switch WHERE environment = $1 AND account_id = $2",
                    self._environment, self._account_id
                )
                with self._lock:
                    if row:
                        if row["is_latched"]:
                            self._is_latched = True
                        self._generation = row["latch_generation"]
                    self._cache_updated_at = time.monotonic()
                    return self._is_latched
        except Exception:
            # Fail closed
            return True

    def trip(self, latched_by: str, reason: str) -> None:
        """Trip the kill switch into latched state."""
        with self._lock:
            if not self._is_latched:
                send_alert(f"🚨 *KILL SWITCH TRIPPED* 🚨\nBy: {latched_by}\nReason: {reason}")
            self._is_latched = True
            self._latched_at_ns = time.time_ns()
            self._latched_by = latched_by
            self._reason = reason

    def can_reset(self, open_orders_count: int, open_positions_count: int) -> bool:
        """Reset is prohibited unless open orders and open positions are completely zero."""
        return open_orders_count == 0 and open_positions_count == 0

    def reset(self, reset_by: str, open_orders_count: int, open_positions_count: int) -> bool:
        """Attempt to reset the latched switch. Fails if open exposure exists."""
        with self._lock:
            if not self.can_reset(open_orders_count, open_positions_count):
                return False
            self._is_latched = False
            self._reset_at_ns = time.time_ns()
            self._reset_by = reset_by
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "is_latched": self._is_latched,
                "latched_at_ns": self._latched_at_ns,
                "latched_by": self._latched_by,
                "reason": self._reason,
                "reset_at_ns": self._reset_at_ns,
                "reset_by": self._reset_by,
            }
