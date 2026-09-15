"""Clock synchronization and transport skew monitor.

Implements standard NTP-style clock offset calculation for Binance REST / WebSocket:
RTT = t_recv - t_send
offset = server_time - (t_send + RTT / 2)

Guardrails:
- 500ms warning threshold (logged as WARNING)
- 1000ms blocking threshold (raises ClockDriftExceededError, gates order submission)
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("cryptonite.execution.clock")


class ClockDriftExceededError(Exception):
    """Raised when clock drift between local system and Binance exceeds 1000ms."""


@dataclass(frozen=True)
class ClockSample:
    """Represents a single clock synchronization measurement."""
    t_send_ms: int
    server_time_ms: int
    t_recv_ms: int
    rtt_ms: int
    offset_ms: float
    is_warning: bool
    is_blocked: bool


class ClockSyncMonitor:
    """Monitors clock synchronization with Binance venue."""

    WARN_THRESHOLD_MS: float = 500.0
    BLOCK_THRESHOLD_MS: float = 1000.0

    def __init__(
        self,
        time_fetcher: Optional[Callable[[], Awaitable[int]]] = None,
        sync_interval_secs: float = 30.0,
    ):
        self._time_fetcher = time_fetcher
        self._sync_interval_secs = sync_interval_secs
        self._last_sample: Optional[ClockSample] = None
        self._task: Optional[asyncio.Task] = None
        self._running: bool = False

    @property
    def last_sample(self) -> Optional[ClockSample]:
        return self._last_sample

    def compute_sample(self, t_send_ms: int, server_time_ms: int, t_recv_ms: int) -> ClockSample:
        """Compute RTT and offset from measurement timestamps."""
        rtt = max(0, t_recv_ms - t_send_ms)
        # Offset: positive means local clock is behind server; negative means local clock is ahead
        offset = float(server_time_ms) - (float(t_send_ms) + (float(rtt) / 2.0))
        abs_offset = abs(offset)
        is_warning = abs_offset >= self.WARN_THRESHOLD_MS
        is_blocked = abs_offset >= self.BLOCK_THRESHOLD_MS

        sample = ClockSample(
            t_send_ms=t_send_ms,
            server_time_ms=server_time_ms,
            t_recv_ms=t_recv_ms,
            rtt_ms=rtt,
            offset_ms=offset,
            is_warning=is_warning,
            is_blocked=is_blocked,
        )
        self._last_sample = sample

        if is_blocked:
            logger.error(
                f"CRITICAL: Clock drift exceeded block threshold! Offset: {offset:.1f}ms "
                f"(RTT: {rtt}ms, Limit: ±{self.BLOCK_THRESHOLD_MS}ms)"
            )
        elif is_warning:
            logger.warning(
                f"WARNING: Clock drift elevated. Offset: {offset:.1f}ms "
                f"(RTT: {rtt}ms, Warn: ±{self.WARN_THRESHOLD_MS}ms)"
            )
        else:
            logger.debug(f"Clock sync healthy. Offset: {offset:.1f}ms, RTT: {rtt}ms")

        return sample

    def assert_clock_in_bounds(self, sample: Optional[ClockSample] = None) -> None:
        """Verify the current clock sample is within safe bounds (< 1000ms drift)."""
        current = sample or self._last_sample
        if current is None:
            # If never sampled, we do not have proof of safe clock
            return
        if current.is_blocked:
            raise ClockDriftExceededError(
                f"Clock drift {current.offset_ms:.1f}ms exceeds safety threshold "
                f"±{self.BLOCK_THRESHOLD_MS}ms. Order execution is gated."
            )

    async def sample_now(self) -> ClockSample:
        """Perform an immediate clock sync probe using the configured fetcher."""
        if not self._time_fetcher:
            raise RuntimeError("No time_fetcher configured for ClockSyncMonitor")

        t_send = int(time.time() * 1000)
        server_time = await self._time_fetcher()
        t_recv = int(time.time() * 1000)

        return self.compute_sample(t_send, server_time, t_recv)

    async def start(self) -> None:
        """Start the background synchronization loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the background synchronization loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        while self._running:
            try:
                if self._time_fetcher:
                    await self.sample_now()
            except Exception as e:
                logger.warning(f"Error during periodic clock synchronization probe: {e}")
            await asyncio.sleep(self._sync_interval_secs)
