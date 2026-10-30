"""Market data ingress configuration and dual-freshness health monitoring."""

import logging
import time
from typing import Any

from cryptonite_schemas.profile import Market, market_of, underlying_spot
from nautilus_trader.model import InstrumentId

logger = logging.getLogger("cryptonite.execution.ingress")


class IngressHealthMonitor:
    """Tracks sequence continuity and evaluates dual freshness between telemetry and LiveNode cache."""

    def __init__(self, max_staleness_s: float = 5.0) -> None:
        self.max_staleness_s = max_staleness_s
        self._health: dict[str, dict[str, Any]] = {}

    def update_health(
        self,
        instrument_id: str,
        session_id: str,
        seq: int,
        event_ts_ns: int,
        arrival_ts_ns: int,
    ) -> None:
        """Update telemetry record for an instrument and check sequence continuity."""
        prev = self._health.get(instrument_id)
        has_gap = False
        if prev and prev["session_id"] == session_id:
            expected_seq = prev["last_seq"] + 1
            if seq != expected_seq:
                logger.warning(
                    "Sequence gap detected for %s: expected %d, got %d",
                    instrument_id,
                    expected_seq,
                    seq,
                )
                has_gap = True

        self._health[instrument_id] = {
            "session_id": session_id,
            "last_seq": seq,
            "event_ts_ns": event_ts_ns,
            "arrival_ts_ns": arrival_ts_ns,
            "has_gap": has_gap,
            "updated_at_ns": time.time_ns(),
        }

    def is_fresh(
        self,
        instrument_id: str,
        node_cache: Any,
        now_ns: int | None = None,
    ) -> bool:
        """Evaluate dual freshness:
        
        1. Ingress health sequence is continuous and fresh (arrival <= max_staleness_s).
        2. LiveNode cache contains a valid quote for instrument_id.
        3. quote.ts_init is fresh and not lagging verified health arrival timestamp.
        """
        now = now_ns or time.time_ns()
        max_delta_ns = int(self.max_staleness_s * 1e9)

        # Condition 1: Health telemetry is continuous and recent
        quote_instrument_id = underlying_spot(instrument_id) if market_of(instrument_id) is Market.SPOT_MARGIN else instrument_id
        health = self._health.get(quote_instrument_id)
        if not health:
            return False

        if health.get("has_gap", False):
            return False

        if (now - health["arrival_ts_ns"]) > max_delta_ns:
            return False

        # Condition 2: Node cache contains quote
        if node_cache is None:
            return False

        try:
            inst = InstrumentId.from_str(quote_instrument_id)
            quote = node_cache.quote(inst)
        except Exception:
            return False

        if quote is None:
            return False

        # Condition 3: Quote timestamp in node cache is fresh and consistent with health
        if (now - quote.ts_init) > max_delta_ns:
            return False

        # Quote must not predate the verified health event by more than 1 second (jitter window)
        if quote.ts_init < (health["event_ts_ns"] - int(1e9)):
            return False

        return True

    def get_health_snapshot(self) -> dict[str, dict[str, Any]]:
        return dict(self._health)
