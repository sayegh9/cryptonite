"""Debate stream consumption with recovery (review-plan B4, F-H6): at-least-once, bounded retries, a DLQ.

Delivery model (Redis consumer group ``debate_workers`` on the entry and exit debate streams):
  - Each process has a stable consumer name (``consumer_name``), so a restarted container finds its own
    pending entries again instead of leaving them under a random name forever.
  - A message is acked after its handler returns: the debate ran, or ended in a terminal state the
    handler recorded itself (gate skip, FAILED run). An exception that escapes the handler leaves the
    entry pending; it is retried by reclaim. A ``PoisonMessage`` goes to the DLQ at once.
  - While a handler runs, its entry's idle time is reset every ``LEASE_REFRESH_S`` (XCLAIM JUSTID, which
    does not count as a delivery), so only a dead or frozen worker's entries ever reach ``RECLAIM_IDLE_MS``.
  - On start each process takes back its own pending entries; every ``HOUSEKEEPING_S`` any replica
    XAUTOCLAIMs entries idle > ``RECLAIM_IDLE_MS``. XAUTOCLAIM is atomic, so replicas never share one.
  - Redis counts deliveries (XPENDING ``times_delivered``). An entry delivered more than
    ``MAX_DELIVERIES`` times is copied to ``stream:debate_dlq`` (bounded) with its reason and acked.
  - Handlers are idempotent per message (the entry debate's run row is unique on its stream message id),
    so a redelivery after a crash between commit and ack does not debate the candidate twice.
Housekeeping that must run once per tick (reaping stale runs, deleting idle consumers) is guarded by a
short Redis lock, so 3 replicas do not fight over it.
"""

import asyncio
import contextlib
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger("debate_worker.queue")

GROUP = "debate_workers"
DEBATE_QUEUE = "stream:debate_queue"
EXIT_DEBATE_QUEUE = "stream:exit_debate_queue"
STREAMS = (DEBATE_QUEUE, EXIT_DEBATE_QUEUE)
DLQ_STREAM = "stream:debate_dlq"
DLQ_MAXLEN = 10_000  # approximate trim; a DLQ entry keeps the full original payload for replay

MAX_DELIVERIES = 3
RECLAIM_IDLE_MS = 10 * 60 * 1000
LEASE_REFRESH_S = 60
HOUSEKEEPING_S = 60
CONSUMER_IDLE_DELETE_MS = 60 * 60 * 1000
STALE_RUN_S = 30 * 60
HOUSEKEEPING_LOCK = "debate_worker:housekeeping:lock"
HOUSEKEEPING_LOCK_TTL_S = 50  # shorter than the tick: a dead holder never blocks the next one

Handler = Callable[[str, str, dict, int], Awaitable[None]]


class PoisonMessage(Exception):
    """The message can never succeed (malformed payload): dead-letter it now instead of retrying."""


def consumer_name(environ: dict | None = None) -> str:
    """``debate-<hostname>`` (or ``DEBATE_WORKER_NAME``). Compose gives every replica container its own
    hostname (the container id), which survives a restart of that container: a crashed replica comes back
    under the same name and finds its pending entries. A recreated container (redeploy) gets a new name;
    the old consumer's entries are reclaimed by idle time and the consumer is deleted once idle > 1 h."""
    env = os.environ if environ is None else environ
    if name := env.get("DEBATE_WORKER_NAME"):
        return name
    return f"debate-{env.get('HOSTNAME') or socket.gethostname()}"


async def ensure_groups(redis) -> None:
    for stream in STREAMS:
        try:
            await redis.xgroup_create(stream, GROUP, id="0", mkstream=True)
            logger.info("Created consumer group %s for %s", GROUP, stream)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                logger.error("Error creating consumer group on %s: %s", stream, e)


async def deliveries(redis, stream: str, msg_id: str) -> int:
    """How many times Redis has delivered this pending entry (1 for a first read)."""
    rows = await redis.xpending_range(stream, GROUP, min=msg_id, max=msg_id, count=1)
    return int(rows[0]["times_delivered"]) if rows else 1


async def dead_letter(redis, stream: str, msg_id: str, fields: dict, reason: str, delivered: int, consumer: str) -> None:
    """Copy the entry to the DLQ with why and where it came from, and ack it, in one MULTI."""
    entry = {**fields, "dlq_source_stream": stream, "dlq_source_id": msg_id, "dlq_reason": reason[:200],
             "dlq_deliveries": delivered, "dlq_consumer": consumer, "dlq_at": int(time.time())}
    async with redis.pipeline(transaction=True) as pipe:
        pipe.xadd(DLQ_STREAM, entry, maxlen=DLQ_MAXLEN, approximate=True)
        pipe.xack(stream, GROUP, msg_id)
        await pipe.execute()
    logger.error("Dead-lettered %s %s after %d deliveries: %s", stream, msg_id, delivered, reason)


class DebateQueue:
    def __init__(self, redis, consumer: str, handler: Handler, *, reclaim_idle_ms: int = RECLAIM_IDLE_MS,
                 lease_refresh_s: float = LEASE_REFRESH_S):
        self.redis = redis
        self.consumer = consumer
        self.handler = handler
        self.reclaim_idle_ms = reclaim_idle_ms
        self.lease_refresh_s = lease_refresh_s

    async def handle(self, stream: str, msg_id: str, fields: dict, delivered: int = 1) -> bool:
        """Run one entry; True if it was acked (done or dead-lettered), False if left pending for retry."""
        if delivered > MAX_DELIVERIES:
            await dead_letter(self.redis, stream, msg_id, fields, "MAX_DELIVERIES", delivered, self.consumer)
            return True
        lease = asyncio.create_task(self._keep_lease(stream, msg_id))
        try:
            await self.handler(stream, msg_id, fields, delivered)
        except PoisonMessage as e:
            await dead_letter(self.redis, stream, msg_id, fields, f"POISON: {e}", delivered, self.consumer)
            return True
        except Exception as e:  # transient or unknown: stays pending; reclaimed after the idle window
            logger.error("Message %s on %s failed (delivery %d/%d), left for retry: %s",
                         msg_id, stream, delivered, MAX_DELIVERIES, e)
            return False
        finally:
            lease.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lease
        await self.redis.xack(stream, GROUP, msg_id)
        return True

    async def _keep_lease(self, stream: str, msg_id: str) -> None:
        """Reset the entry's idle time while we work on it, unless another consumer has taken it over."""
        while True:
            await asyncio.sleep(self.lease_refresh_s)
            try:
                rows = await self.redis.xpending_range(stream, GROUP, min=msg_id, max=msg_id, count=1)
                if not rows or rows[0]["consumer"] != self.consumer:
                    logger.warning("Lost the lease on %s %s (reclaimed after an idle gap)", stream, msg_id)
                    return
                await self.redis.xclaim(stream, GROUP, self.consumer, 0, [msg_id], justid=True)
            except Exception as e:
                logger.warning("Lease refresh for %s failed: %s", msg_id, e)

    async def read(self, block_ms: int = 5000) -> int:
        """Read and handle new entries (``>``). Returns how many were handled."""
        messages = await self.redis.xreadgroup(GROUP, self.consumer, {s: ">" for s in STREAMS}, count=1, block=block_ms)
        handled = 0
        for stream, msgs in messages or []:
            for msg_id, fields in msgs:
                logger.info("Received message %s from %s: %s", msg_id, stream, fields)
                await self.handle(stream, msg_id, fields)
                handled += 1
        return handled

    async def recover_own(self) -> int:
        """On start: entries still pending under this consumer name were in flight when it died."""
        handled = 0
        for stream in STREAMS:
            rows = await self.redis.xpending_range(stream, GROUP, min="-", max="+", count=100, consumername=self.consumer)
            if not rows:
                continue
            ids = [r["message_id"] for r in rows]
            for msg_id, fields in await self.redis.xclaim(stream, GROUP, self.consumer, 0, ids):
                if fields is None:  # trimmed from the stream; nothing left to run
                    await self.redis.xack(stream, GROUP, msg_id)
                    continue
                await self.handle(stream, msg_id, fields, await deliveries(self.redis, stream, msg_id))
                handled += 1
        return handled

    async def reclaim(self, count: int = 10) -> int:
        """XAUTOCLAIM entries idle longer than the reclaim window to this consumer, then run them."""
        handled = 0
        for stream in STREAMS:
            start = "0-0"
            while True:
                res = await self.redis.xautoclaim(stream, GROUP, self.consumer, self.reclaim_idle_ms,
                                                  start_id=start, count=count)
                start, claimed = res[0], res[1]
                for msg_id, fields in claimed:
                    delivered = await deliveries(self.redis, stream, msg_id)
                    logger.warning("Reclaimed %s on %s (delivery %d)", msg_id, stream, delivered)
                    await self.handle(stream, msg_id, fields, delivered)
                    handled += 1
                if start in ("0-0", b"0-0"):
                    break
        return handled


async def delete_idle_consumers(redis, keep: str, idle_ms: int = CONSUMER_IDLE_DELETE_MS) -> list[str]:
    """XGROUP DELCONSUMER every consumer idle > ``idle_ms`` with no pending entries (never ``keep``)."""
    deleted = []
    for stream in STREAMS:
        try:
            consumers = await redis.xinfo_consumers(stream, GROUP)
        except Exception as e:
            logger.warning("XINFO CONSUMERS %s failed: %s", stream, e)
            continue
        for c in consumers:
            if c["name"] != keep and int(c["pending"]) == 0 and int(c["idle"]) > idle_ms:
                await redis.xgroup_delconsumer(stream, GROUP, c["name"])
                deleted.append(f"{stream}/{c['name']}")
    if deleted:
        logger.info("Deleted %d idle consumers: %s", len(deleted), ", ".join(deleted))
    return deleted


async def reap_stale_runs(pg_pool, max_age_s: int = STALE_RUN_S) -> list:
    """debate_runs RUNNING for longer than ``max_age_s`` -> FAILED / REAPED_STALE. Returns their ids."""
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """UPDATE debate_runs SET status = 'FAILED', completed_at = now(), failure_reason = 'REAPED_STALE'
               WHERE status = 'RUNNING' AND created_at < now() - make_interval(secs => $1)
               RETURNING id""",
            max_age_s,
        )
    ids = [r["id"] for r in rows]
    if ids:
        logger.warning("Reaped %d stale debate runs: %s", len(ids), ", ".join(map(str, ids)))
    return ids


async def housekeeping(redis, pg_pool, queue: DebateQueue) -> None:
    """Every tick on every replica: reclaim. Under the lock (one replica per tick): reap and clean up."""
    try:
        await queue.reclaim()
    except Exception as e:
        logger.error("Reclaim failed: %s", e)
    try:
        if not await redis.set(HOUSEKEEPING_LOCK, queue.consumer, nx=True, ex=HOUSEKEEPING_LOCK_TTL_S):
            return
        await reap_stale_runs(pg_pool)
        await delete_idle_consumers(redis, keep=queue.consumer)
    except Exception as e:
        logger.error("Housekeeping failed: %s", e)
