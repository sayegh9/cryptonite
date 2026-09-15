"""Gate-owned deduplicating bar router for strategies and indicators."""

import collections
import hashlib
import logging
from typing import Any

from nautilus_trader.model import Bar

logger = logging.getLogger("cryptonite.execution.router")


class GateOwnedBarRouter:
    """Ensures every market bar passes through exactly once to strategy indicators.
    
    Prevents duplicate indicator updates (e.g., double EMA/RSI calculations) during
    catalog rewarming or live reconnects by using a durable content-addressed dedupe key:
    dedupe_key = f"{instrument_id}:{bar_type}:{bar.ts_event}:{sha256(bar_json)}"
    """

    def __init__(self, max_cache_entries: int = 100_000) -> None:
        self.max_cache_entries = max_cache_entries
        self._seen_keys: collections.OrderedDict[str, bool] = collections.OrderedDict()
        self.max_processed_bar_ns: int = 0
        self.routed_count: int = 0
        self.deduped_count: int = 0

    def compute_dedupe_key(self, bar: Bar) -> str:
        """Compute durable deterministic key for a bar."""
        payload_bytes = bar.to_json_bytes()
        checksum = hashlib.sha256(payload_bytes).hexdigest()
        return f"{bar.bar_type.instrument_id}:{bar.bar_type}:{bar.ts_event}:{checksum}"

    def has_seen(self, bar: Bar) -> bool:
        """Check if a bar has already been routed."""
        key = self.compute_dedupe_key(bar)
        return key in self._seen_keys

    def route_bar(
        self,
        bar: Bar,
        strategies: list[Any],
    ) -> bool:
        """Route a bar through subscribed strategies iff it has not been routed before.
        
        Returns True if the bar was newly routed, False if it was deduplicated.
        """
        key = self.compute_dedupe_key(bar)
        if key in self._seen_keys:
            self.deduped_count += 1
            logger.debug("Deduplicating bar %s with key %s", bar, key)
            return False

        # Add to seen cache with LRU eviction
        self._seen_keys[key] = True
        if len(self._seen_keys) > self.max_cache_entries:
            self._seen_keys.popitem(last=False)

        # Forward bar to each strategy
        for strat in strategies:
            try:
                strat.on_bar(bar)
            except Exception as e:
                logger.error("Strategy %s raised error in on_bar: %s", strat, e)

        if bar.ts_event > self.max_processed_bar_ns:
            self.max_processed_bar_ns = bar.ts_event

        self.routed_count += 1
        return True
