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


RENEW_LEASE_LUA = """
local p = redis.call("XPENDING", KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #p > 0 and p[1][2] == ARGV[3] then
    redis.call("XCLAIM", KEYS[1], ARGV[1], ARGV[3], 0, ARGV[2], "JUSTID")
    return 1
end
return 0
"""

ACK_OWNED_LUA = """
local p = redis.call("XPENDING", KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #p > 0 and p[1][2] == ARGV[3] then
    redis.call("XACK", KEYS[1], ARGV[1], ARGV[2])
    return 1
end
return 0
"""

DLQ_OWNED_LUA = """
local p = redis.call("XPENDING", KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #p > 0 and p[1][2] == ARGV[3] then
    local xadd_args = {"XADD", KEYS[2], "MAXLEN", "~", ARGV[4], "*"}
    for i = 5, #ARGV do
        table.insert(xadd_args, ARGV[i])
    end
    redis.call(unpack(xadd_args))
    redis.call("XACK", KEYS[1], ARGV[1], ARGV[2])
    return 1
end
return 0
"""

DEL_IDLE_CONSUMER_LUA = """
local consumers = redis.call("XINFO", "CONSUMERS", KEYS[1], ARGV[1])
for i, c in ipairs(consumers) do
    local name = nil
    local pending = nil
    local idle = nil
    for j = 1, #c, 2 do
        if c[j] == "name" then name = c[j+1] end
        if c[j] == "pending" then pending = c[j+1] end
        if c[j] == "idle" then idle = c[j+1] end
    end
    if name == ARGV[2] then
        if tonumber(pending) == 0 and tonumber(idle) >= tonumber(ARGV[3]) then
            redis.call("XGROUP", "DELCONSUMER", KEYS[1], ARGV[1], ARGV[2])
            return 1
        else
            return 0
        end
    end
end
return 0
"""


async def deliveries(redis, stream: str, msg_id: str) -> int:
    """How many times Redis has delivered this pending entry (1 for a first read)."""
    rows = await redis.xpending_range(stream, GROUP, min=msg_id, max=msg_id, count=1)
    return int(rows[0]["times_delivered"]) if rows else 1


async def ack_if_owner(redis, stream: str, msg_id: str, consumer: str) -> bool:
    """XACK atomically only if consumer still owns the message in the PEL."""
    res = await redis.eval(ACK_OWNED_LUA, 1, stream, GROUP, msg_id, consumer)
    return bool(res)


async def dead_letter(redis, stream: str, msg_id: str, fields: dict, reason: str, delivered: int, consumer: str,
                      expected_owner: str | None = None) -> bool:
    """Copy the entry to DLQ and XACK atomically only if expected_owner still owns the message in the PEL."""
    owner = expected_owner if expected_owner is not None else consumer
    entry = {**fields, "dlq_source_stream": stream, "dlq_source_id": msg_id, "dlq_reason": str(reason)[:200],
             "dlq_deliveries": str(delivered), "dlq_consumer": consumer, "dlq_at": str(int(time.time()))}
    flat_args = [GROUP, msg_id, owner, str(DLQ_MAXLEN)]
    for k, v in entry.items():
        flat_args.extend([str(k), str(v)])
    res = await redis.eval(DLQ_OWNED_LUA, 2, stream, DLQ_STREAM, *flat_args)
    if res:
        logger.error("Dead-lettered %s %s after %d deliveries: %s", stream, msg_id, delivered, reason)
        return True
    logger.warning("Refused DLQ for %s %s: owner %s no longer owns the entry", stream, msg_id, owner)
    return False


CLAIM_OWNED_LUA = """
local p = redis.call("XPENDING", KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #p > 0 and p[1][2] == ARGV[3] then
    local res = redis.call("XCLAIM", KEYS[1], ARGV[1], ARGV[3], 0, ARGV[2])
    return res
end
return {}
"""


async def claim_if_owner(redis_client, stream: str, msg_id: str, consumer: str):
    """Atomically claims an entry with XCLAIM only if consumer is still the recorded owner in PEL."""
    res = await redis_client.eval(CLAIM_OWNED_LUA, 1, stream, GROUP, msg_id, consumer)
    if not res:
        return None
    entry = res[0]
    raw_fields = entry[1]
    if raw_fields is None:
        return (entry[0], None)
    if isinstance(raw_fields, list):
        if not raw_fields:
            return (entry[0], None)
        fields = dict(zip(raw_fields[0::2], raw_fields[1::2]))
    elif isinstance(raw_fields, dict):
        fields = raw_fields
    else:
        fields = None
    return (entry[0], fields)


class DebateQueue:
    def __init__(self, redis, consumer: str, handler: Handler, *, reclaim_idle_ms: int = RECLAIM_IDLE_MS,
                 lease_refresh_s: float = LEASE_REFRESH_S):
        self.redis = redis
        self.consumer = consumer
        self.handler = handler
        self.reclaim_idle_ms = reclaim_idle_ms
        self.lease_refresh_s = lease_refresh_s
        self._next_stream_idx = 0

    async def handle(self, stream: str, msg_id: str, fields: dict, delivered: int = 1) -> bool:
        """Run one entry; True if it was acked (done or dead-lettered), False if left pending for retry."""
        if delivered > MAX_DELIVERIES:
            return await dead_letter(self.redis, stream, msg_id, fields, "MAX_DELIVERIES", delivered, self.consumer)

        handler_task: asyncio.Task | None = None
        ownership_lost = asyncio.Event()

        async def keep_lease():
            while True:
                await asyncio.sleep(self.lease_refresh_s)
                try:
                    renewed = await self.redis.eval(RENEW_LEASE_LUA, 1, stream, GROUP, msg_id, self.consumer)
                    if not renewed:
                        logger.warning("Lost lease ownership on %s %s (reclaimed); canceling handler fail-closed", stream, msg_id)
                        ownership_lost.set()
                        if handler_task and not handler_task.done():
                            handler_task.cancel()
                        return
                except Exception as e:
                    logger.warning("Lease renewal failed for %s %s: %s; canceling handler fail-closed", stream, msg_id, e)
                    ownership_lost.set()
                    if handler_task and not handler_task.done():
                        handler_task.cancel()
                    return

        lease_task = asyncio.create_task(keep_lease())
        try:
            handler_task = asyncio.create_task(self.handler(stream, msg_id, fields, delivered))
            await handler_task
        except asyncio.CancelledError:
            if ownership_lost.is_set():
                logger.error("Handler for %s %s cancelled due to lease ownership loss", stream, msg_id)
                return False
            raise
        except PoisonMessage as e:
            return await dead_letter(self.redis, stream, msg_id, fields, f"POISON: {e}", delivered, self.consumer)
        except Exception as e:  # transient or unknown: stays pending; reclaimed after the idle window
            logger.error("Message %s on %s failed (delivery %d/%d), left for retry: %s",
                         msg_id, stream, delivered, MAX_DELIVERIES, e)
            return False
        finally:
            lease_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lease_task

        if ownership_lost.is_set():
            return False

        # Atomic owner-checked ACK: stale owner never acks
        acked = await ack_if_owner(self.redis, stream, msg_id, self.consumer)
        if not acked:
            logger.warning("Could not ACK %s %s: ownership lost to another consumer", stream, msg_id)
            return False
        return True

    async def read(self, block_ms: int = 5000) -> int:
        """Read and handle at most ONE entry from a single stream at a time (round-robin across STREAMS).
        Never reads multiple streams in one XREADGROUP call, which would deliver one entry per stream
        and leave the second unleased while the first runs."""
        stream = STREAMS[self._next_stream_idx % len(STREAMS)]
        self._next_stream_idx = (self._next_stream_idx + 1) % len(STREAMS)
        messages = await self.redis.xreadgroup(GROUP, self.consumer, {stream: ">"}, count=1, block=block_ms)
        handled = 0
        for s, msgs in messages or []:
            for msg_id, fields in msgs:
                logger.info("Received message %s from %s: %s", msg_id, s, fields)
                await self.handle(s, msg_id, fields)
                handled += 1
        return handled

    async def recover_own(self) -> int:
        """On start: entries still pending under this consumer name were in flight when it died.
        Take one at a time and atomically verify current ownership so transferred entries are not stolen."""
        handled = 0
        for stream in STREAMS:
            rows = await self.redis.xpending_range(stream, GROUP, min="-", max="+", count=100, consumername=self.consumer)
            if not rows:
                continue
            for r in rows:
                msg_id = r["message_id"]
                claimed = await claim_if_owner(self.redis, stream, msg_id, self.consumer)
                if not claimed:
                    logger.info("Entry %s on %s was transferred to another consumer: not recovered", msg_id, stream)
                    continue
                _id, fields = claimed
                if fields is None:  # trimmed from stream
                    await ack_if_owner(self.redis, stream, msg_id, self.consumer)
                    continue
                await self.handle(stream, msg_id, fields, await deliveries(self.redis, stream, msg_id))
                handled += 1
        return handled

    async def reclaim(self, count: int = 1) -> int:
        """XAUTOCLAIM entries idle longer than the reclaim window to this consumer, one at a time.
        Taking one at a time prevents claiming multiple entries that would sit unleased while waiting."""
        handled = 0
        for stream in STREAMS:
            start = "0-0"
            while True:
                res = await self.redis.xautoclaim(stream, GROUP, self.consumer, self.reclaim_idle_ms,
                                                  start_id=start, count=1)
                start, claimed = res[0], res[1]
                if not claimed:
                    if start in ("0-0", b"0-0"):
                        break
                    continue
                for msg_id, fields in claimed:
                    delivered = await deliveries(self.redis, stream, msg_id)
                    logger.warning("Reclaimed %s on %s (delivery %d)", msg_id, stream, delivered)
                    await self.handle(stream, msg_id, fields, delivered)
                    handled += 1
                if start in ("0-0", b"0-0"):
                    break
        return handled


async def delete_idle_consumers(redis, keep: str, idle_ms: int = CONSUMER_IDLE_DELETE_MS) -> list[str]:
    """Atomically delete consumers idle > ``idle_ms`` with current PEL count == 0 (never ``keep``)."""
    deleted = []
    for stream in STREAMS:
        try:
            consumers = await redis.xinfo_consumers(stream, GROUP)
        except Exception as e:
            logger.warning("XINFO CONSUMERS %s failed: %s", stream, e)
            continue
        for c in consumers:
            c_name = c["name"]
            if c_name == keep:
                continue
            res = await redis.eval(DEL_IDLE_CONSUMER_LUA, 1, stream, GROUP, c_name, idle_ms)
            if res:
                deleted.append(f"{stream}/{c_name}")
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
