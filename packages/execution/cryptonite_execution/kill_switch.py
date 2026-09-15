"""In-memory latched emergency kill switch with audited reset constraints."""

import threading
import time
from typing import Any


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

    @property
    def is_latched(self) -> bool:
        with self._lock:
            return self._is_latched

    def trip(self, latched_by: str, reason: str) -> None:
        """Trip the kill switch into latched state."""
        with self._lock:
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
