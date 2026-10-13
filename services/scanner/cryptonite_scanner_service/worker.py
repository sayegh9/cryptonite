"""Dedicated runnable Scanner Worker Daemon.

Consumes closed_bar_committed events from Redis Streams under the 'scanner-worker'
consumer group, claims manual scan_jobs, computes Polars indicators over resampled 1m data,
and persists scan_runs and idempotent signal_candidates before calling XACK.

Every match is recorded; only a fresh one (bar closed within one bar of now) is queued for debate, via a
scanner_debate_outbox row committed with the candidate and published at least once (D1 / F-H2).
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import polars as pl
import redis.asyncio as aioredis
from cryptonite_scanner.brief import brief_data, build_brief
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.models import (
    ALLOWED_AUTOMATIC_TIMEFRAMES,
    MIN_RISK_REWARD_RATIO,
    RiskConfig,
    RuleCondition,
    RuleGroup,
    RuleOperator,
    SignalCandidateRead,
    active_spec_cap,
    deduplicate_candidates,
)
from cryptonite_scanner.resampler import ONE_MINUTE_NS, TIMEFRAME_MINUTES, resample_1m_bars
from cryptonite_storage.queries import AnalyticsQueryEngine

BRIEF_TTL_S = 2 * 3600

HTF_MAP: dict[str, str] = {
    "1m": "5m",
    "5m": "15m",
    "15m": "1h",
    "1h": "4h",
    "4h": "1d",
}

logger = logging.getLogger("cryptonite.scanner.worker")

SCAN_LOOKBACK_BARS = 300  # EMA-200 plus warm-up, in bars of the run's largest timeframe

DEFAULT_DB_URL = "postgresql://cryptonite_scanner:scanner_dev_pass@127.0.0.1:5432/cryptonite"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_LAKE_PATH = "data/analytics_lake"

# D1 / F-H2: the debate queue is fed through scanner_debate_outbox (written in the candidate's transaction)
# and capped. Fresh-only enqueue makes ~100 entries a day, so 10,000 is weeks of history, never a backlog.
DEBATE_QUEUE = "stream:debate_queue"
DEBATE_QUEUE_MAXLEN = 10_000
DEBATE_RELAY_INTERVAL_S = 5.0
DEBATE_RELAY_LEASE_S = 60  # the committing scan publishes first; the relay takes a row only after this
DEBATE_RELAY_MAX_ATTEMPTS = 10
DEBATE_RELAY_BATCH = 50


def is_fresh(c: Any, now_ns: int) -> bool:
    """D1 / F-H2: a candidate is queued for debate only if its bar closed within one bar of now.

    Older matches are still recorded in ``signal_candidates`` (research), but ``ScannerEngine.evaluate`` runs
    over the whole lookback, so after a restart or backfill they are old news (82% of the 10-05 candidates
    were STALE at the gate). An unknown timeframe is never queued."""
    bar_ns = TIMEFRAME_MINUTES.get(c.timeframe, 0) * ONE_MINUTE_NS
    age_ns = now_ns - c.interval_end_exclusive_ns
    return bar_ns > 0 and 0 <= age_ns <= bar_ns


def is_fresh_payload(payload: dict[str, Any], now_ns: int) -> bool:
    """D1: the same one-bar window as ``is_fresh``, rechecked on the stored payload right before the queue
    write — a relay after an outage must not push a setup whose window has passed (or one whose window
    cannot be verified)."""
    bar_ns = TIMEFRAME_MINUTES.get(payload.get("timeframe"), 0) * ONE_MINUTE_NS
    age_ns = now_ns - int(payload.get("interval_end_exclusive_ns") or 0)
    return bar_ns > 0 and 0 <= age_ns <= bar_ns


def brief_key_for(instrument_id: str, timeframe: str, bar_close_inclusive_ns: int) -> str:
    return f"brief:{instrument_id}:{timeframe}:{bar_close_inclusive_ns}"


async def enqueue_debate(redis_client: Any, fields: dict[str, Any]) -> str:
    """XADD one entry to the debate queue, trimming it to about DEBATE_QUEUE_MAXLEN (F-H2: was unbounded)."""
    return await redis_client.xadd(DEBATE_QUEUE, fields, maxlen=DEBATE_QUEUE_MAXLEN, approximate=True)


def drop_unclosed_candidates(candidates: list[Any], now_ns: int) -> list[Any]:
    """Keep only candidates whose bar has closed (``interval_end_exclusive_ns <= now_ns``).

    B1 / F-C1: a candidate must never exist before its bar closed. The resampled frames are already cut at
    ``now`` before rule evaluation (a minute recorded while its kline was still open would otherwise make a
    forming window look complete); this second check, right before the commit, only guards against a rule
    path that bypasses that filter or the wall clock stepping back between the two."""
    early = [c for c in candidates if c.interval_end_exclusive_ns > now_ns]
    for c in early:
        logger.warning("Dropped candidate %s %s %s: its bar closes at %d, after now (%d)",
                       c.id, c.instrument_id, c.timeframe, c.interval_end_exclusive_ns, now_ns)
    return [c for c in candidates if c.interval_end_exclusive_ns <= now_ns] if early else candidates


class ScannerWorker:
    """Consumes committed closed bars and manual scan jobs to produce signal candidates."""

    def __init__(
        self,
        database_url: str | None = None,
        redis_url: str | None = None,
        analytics_lake_path: str | None = None,
        consumer_group: str = "scanner-worker",
        consumer_name: str | None = None,
    ) -> None:
        self.database_url = database_url or os.getenv("DATABASE_URL", DEFAULT_DB_URL)
        self.redis_url = redis_url or os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
        self.lake_path = Path(analytics_lake_path or os.getenv("ANALYTICS_LAKE_PATH", DEFAULT_LAKE_PATH)).resolve()
        self._file_digests: dict[tuple[str, int, int], str] = {}  # (path, size, mtime) -> sha256
        self.consumer_group = consumer_group
        self.consumer_name = consumer_name or f"worker-{os.getpid()}-{uuid4().hex[:6]}"

        self.pool: asyncpg.Pool | None = None
        self.redis: aioredis.Redis | None = None
        self.query_engine = AnalyticsQueryEngine(self.lake_path)
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []

        self.processed_events_count = 0
        self.executed_runs_count = 0
        self.detected_candidates_count = 0
        self.queued_candidates_count = 0  # published to the debate queue (fresh only, D1)

    async def start(self) -> None:
        """Initialize connections and start event / job consumption loops."""
        logger.info("Starting ScannerWorker consumer=%s group=%s", self.consumer_name, self.consumer_group)
        self.pool = await asyncpg.create_pool(self.database_url, min_size=2, max_size=10)
        self.redis = aioredis.from_url(self.redis_url, decode_responses=True, socket_timeout=30)  # redis-py 8 times a blocking read out at exactly its block time (LE-3)

        # Ensure Redis consumer group exists on cryptonite:stream:closed_bar_committed
        try:
            await self.redis.xgroup_create(
                "cryptonite:stream:closed_bar_committed",
                self.consumer_group,
                id="0",
                mkstream=True,
            )
            logger.info("Created Redis consumer group: %s", self.consumer_group)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                logger.warning("Error creating consumer group: %s", e)

        self._running = True

        # Run startup reconciliation and historical priming
        await self.reconcile_and_prime()

        # Launch consumer loops
        self._tasks.append(asyncio.create_task(self._event_consumer_loop()))
        self._tasks.append(asyncio.create_task(self._job_consumer_loop()))
        self._tasks.append(asyncio.create_task(self._debate_relay_loop()))

    def _partition_digests(self, inst_dir: Path, since_date: str) -> dict[str, str]:
        """sha256 per date partition (``date=YYYY-MM-DD``) from ``since_date`` on, over the sorted
        ``name:sha256`` of its parquet files. File hashes are cached by (path, size, mtime), so an
        unchanged file is never re-read; storing per-partition digests instead of every file path keeps
        a scan_runs row at a few kB instead of hundreds."""
        out: dict[str, str] = {}
        for part in sorted(inst_dir.glob("date=*")):
            if part.name.removeprefix("date=") < since_date:
                continue
            entries = []
            for f in sorted(part.glob("*.parquet")):
                try:
                    st = f.stat()
                except OSError:
                    continue
                key = (str(f), st.st_size, st.st_mtime_ns)
                digest = self._file_digests.get(key)
                if digest is None:
                    h = hashlib.sha256()
                    try:
                        with open(f, "rb") as fh:
                            while chunk := fh.read(65536):
                                h.update(chunk)
                        digest = h.hexdigest()
                    except OSError:
                        digest = ""
                    self._file_digests[key] = digest
                entries.append(f"{f.name}:{digest}")
            out[str(part)] = hashlib.sha256("\n".join(entries).encode()).hexdigest()
        return out

    async def _publish_brief(self, c: Any, candidate_payload: dict[str, Any], frames: dict[str, Any]) -> str | None:
        """Build the candidate's market brief once and cache it for the debate (blueprint §4.3)."""
        try:
            sym = c.instrument_id.split(".")[0].upper()
            derivatives: dict[str, Any] = {}
            if raw_mark := await self.redis.get(f"perp:mark:{sym}"):
                mark = json.loads(raw_mark)
                derivatives.update(mark_price=mark.get("mark_price"), funding_rate=mark.get("funding_rate"))
            if raw_oi := await self.redis.get(f"perp:oi:{sym}"):
                derivatives["open_interest"] = json.loads(raw_oi).get("open_interest")
            now = datetime.now(UTC)
            args = dict(candidate=candidate_payload, derivatives=derivatives or None,
                        last_bar_close_ns=c.bar_close_inclusive_ns, now=now)
            brief = build_brief(c.instrument_id, frames, **args)
            key = brief_key_for(c.instrument_id, c.timeframe, c.bar_close_inclusive_ns)
            await self.redis.set(key, brief, ex=BRIEF_TTL_S)
            try:  # the structured twin lets the gate apply the hard triage rules in code (R3.1)
                await self.redis.set(f"{key}:data", json.dumps(brief_data(c.instrument_id, frames, **args)), ex=BRIEF_TTL_S)
            except Exception as e:  # without it the gate falls back to the model, as before
                logger.warning("Market brief data failed for %s: %s", c.instrument_id, e)
            return key
        except Exception as e:  # a missing brief degrades the debate context; it never blocks the scan
            logger.warning("Market brief failed for %s: %s", c.instrument_id, e)
            return None

    async def _publish_debate_candidates(self, queued: list[tuple[Any, dict[str, Any], dict[str, Any]]]) -> None:
        """Fast path, right after the commit: brief, XADD and mark each fresh candidate's outbox row. A row
        this does not confirm stays PENDING and the relay loop retries it, so nothing is lost on a failure."""
        for c, fields, frames in queued:
            brief_key = None
            if self.redis:
                brief_key = await self._publish_brief(c, fields["candidate_payload"], frames)
            await self._publish_outbox_row(c.id, fields, brief_key or "", attempt_count=1)

    async def _publish_outbox_row(self, candidate_id: UUID, fields: dict[str, Any], brief_key: str,
                                  attempt_count: int) -> bool:
        """XADD one scanner_debate_outbox row to the debate queue and mark it PUBLISHED, or schedule a retry.

        Freshness is rechecked immediately before the queue write: a relay that only gets to a row after
        its one-bar window has passed completes it as ``EXPIRED`` (with the reason) instead of enqueueing
        old news; the debate gate never sees it."""
        payload = fields["candidate_payload"]
        now_ns = time.time_ns()
        if not is_fresh_payload(payload, now_ns):
            age_s = (now_ns - int(payload.get("interval_end_exclusive_ns") or 0)) / 1e9
            reason = f"expired before publish: bar closed {age_s:.0f}s ago (fresh window is one bar)"
            try:
                await self.pool.execute(
                    "UPDATE scanner_debate_outbox SET status = 'EXPIRED', last_error = $2 WHERE candidate_id = $1",
                    candidate_id, reason)
            except Exception as e:  # the row stays PENDING: the next relay pass completes it
                logger.error("Candidate %s: could not complete the expired row: %s", candidate_id, e)
            logger.info("Candidate %s not queued for debate: %s", candidate_id, reason)
            return False
        try:
            if not self.redis:
                raise RuntimeError("Redis client not connected")
            stream_id = await enqueue_debate(self.redis, {
                "instrument_id": fields["instrument_id"],
                "trigger_source": fields["trigger_source"],
                "candidate_payload": json.dumps(payload),
                "brief_key": brief_key,
            })
        except Exception as e:
            logger.error("Candidate %s not queued for debate (attempt %d): %s", candidate_id, attempt_count, e)
            await self._retry_outbox_row(candidate_id, attempt_count, str(e))
            return False
        self.queued_candidates_count += 1
        logger.info("Candidate %s %s %s queued for debate (decision_trace_id=%s)", candidate_id,
                    fields["instrument_id"], payload.get("timeframe"), payload.get("decision_trace_id"))
        try:
            await self.pool.execute(
                """UPDATE scanner_debate_outbox SET status = 'PUBLISHED', published_stream_id = $2,
                       published_at = now(), last_error = NULL WHERE candidate_id = $1""",
                candidate_id, str(stream_id))
        except Exception as e:  # the relay re-sends it after the lease; the debate gate drops the duplicate
            logger.error("Candidate %s queued as %s but not marked published: %s", candidate_id, stream_id, e)
        return True

    async def _retry_outbox_row(self, candidate_id: UUID, attempt_count: int, error: str) -> None:
        try:
            if attempt_count >= DEBATE_RELAY_MAX_ATTEMPTS:
                await self.pool.execute(
                    "UPDATE scanner_debate_outbox SET status = 'FAILED', last_error = $2 WHERE candidate_id = $1",
                    candidate_id, error)
                logger.error("Candidate %s: debate enqueue FAILED after %d attempts", candidate_id, attempt_count)
            else:
                await self.pool.execute(
                    """UPDATE scanner_debate_outbox SET last_error = $2,
                           next_attempt_at = now() + make_interval(secs => $3) WHERE candidate_id = $1""",
                    candidate_id, error, float(min(2 ** attempt_count, 300)))
        except Exception as e:  # the row keeps its lease, so the relay still picks it up
            logger.error("Candidate %s: could not record the failed enqueue: %s", candidate_id, e)

    async def relay_debate_outbox_once(self) -> int:
        """Claim PENDING rows whose lease or backoff has run out and publish them. Returns how many were queued."""
        if not self.pool:
            return 0
        rows = await self.pool.fetch(
            """
            WITH claimed AS (
                SELECT candidate_id FROM scanner_debate_outbox
                WHERE status = 'PENDING' AND next_attempt_at <= now()
                ORDER BY created_at
                LIMIT $2
                FOR UPDATE SKIP LOCKED
            )
            UPDATE scanner_debate_outbox o
            SET attempt_count = o.attempt_count + 1, next_attempt_at = now() + make_interval(secs => $1)
            FROM claimed
            WHERE o.candidate_id = claimed.candidate_id
            RETURNING o.candidate_id, o.fields, o.attempt_count;
            """,
            float(DEBATE_RELAY_LEASE_S), DEBATE_RELAY_BATCH,
        )
        published = 0
        for r in rows:
            fields = json.loads(r["fields"]) if isinstance(r["fields"], str) else r["fields"]
            payload = fields["candidate_payload"]
            brief_key = ""
            try:  # the brief the committing scan cached, if it got that far and it has not expired
                key = brief_key_for(fields["instrument_id"], payload["timeframe"], payload["bar_close_inclusive_ns"])
                if self.redis and await self.redis.exists(key):
                    brief_key = key
            except Exception as e:
                logger.warning("Brief lookup for candidate %s failed: %s", r["candidate_id"], e)
            published += await self._publish_outbox_row(r["candidate_id"], fields, brief_key, r["attempt_count"])
        if rows:
            logger.info("Debate outbox relay: %d of %d pending candidates queued", published, len(rows))
        return published

    async def _debate_relay_loop(self) -> None:
        while self._running:
            try:
                await self.relay_debate_outbox_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in debate outbox relay: %s", e)
            await asyncio.sleep(DEBATE_RELAY_INTERVAL_S)

    async def stop(self) -> None:
        """Stop worker tasks and release connections."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

        if self.redis:
            await self.redis.aclose()
            self.redis = None
        if self.pool:
            await self.pool.close()
            self.pool = None
        logger.info("ScannerWorker stopped")

    async def _load_instrument_precisions(self, instrument_ids: list[str]) -> dict[str, int]:
        """Fetch validated price_precision for a list of canonical instrument IDs from the DB.

        Fails closed for any instrument missing from the DB or lacking a valid price_precision.
        """
        precisions: dict[str, int] = {}
        if not self.pool or not instrument_ids:
            return precisions

        try:
            rows = await self.pool.fetch(
                """
                SELECT instrument_id, price_precision
                FROM instruments
                WHERE instrument_id = ANY($1) AND is_active = true;
                """,
                instrument_ids,
            )
            for r in rows:
                if r["price_precision"] is not None:
                    precisions[r["instrument_id"]] = int(r["price_precision"])
        except Exception as e:
            logger.warning("Error fetching instrument precisions from database: %s", e)

        return precisions

    async def reconcile_and_prime(self) -> None:
        """Reconcile scanner checkpoints against latest ingest watermarks on startup."""
        if not self.pool:
            return

        # Fetch active spec versions deterministically ordered
        rows = await self.pool.fetch(
            """
            SELECT s.slug, s.active_version_id, s.created_at, v.id as version_id, v.universe, v.rules, v.spec_hash
            FROM scan_specs s
            JOIN scan_spec_versions v ON s.active_version_id = v.id
            WHERE s.is_archived = false
            ORDER BY s.created_at ASC, s.slug ASC;
            """
        )

        cap = await active_spec_cap(self.pool)
        if len(rows) > cap:
            logger.error(
                "Active specs count (%d) exceeds the active-spec cap (%d)! "
                "Processing all %d active specs to avoid dropping trading signals, "
                "but operator action is required to archive excess specs.",
                len(rows),
                cap,
                len(rows),
            )

        for r in rows:
            version_id = r["version_id"]
            universe = json.loads(r["universe"]) if isinstance(r["universe"], str) else r["universe"]
            instruments = universe.get("instrument_ids", ["BTCUSDT.BINANCE"])

            for inst in instruments:
                symbol = inst.split(".")[0]
                checkpoint = await self.pool.fetchrow(
                    """
                    SELECT last_processed_watermark_ns
                    FROM scanner_checkpoints
                    WHERE spec_version_id = $1 AND instrument_id = $2 AND source_timeframe = '1m';
                    """,
                    version_id,
                    inst,
                )

                ingest_checkpoint = await self.pool.fetchrow(
                    """
                    SELECT last_timestamp_ns
                    FROM ingest_checkpoints
                    WHERE symbol = $1 AND timeframe = '1m';
                    """,
                    symbol,
                )

                latest_ingest_ts = ingest_checkpoint["last_timestamp_ns"] if ingest_checkpoint else None
                scanner_ts = checkpoint["last_processed_watermark_ns"] if checkpoint else None

                if latest_ingest_ts is not None and (scanner_ts is None or scanner_ts < latest_ingest_ts):
                    logger.info(
                        "Priming historical scan for %s on %s (scanner_ts=%s, ingest_ts=%s)",
                        r["slug"],
                        inst,
                        scanner_ts,
                        latest_ingest_ts,
                    )
                    await self._execute_scan(
                        spec_version_id=version_id,
                        spec_hash=r["spec_hash"],
                        universe_data=universe,
                        rules_data=json.loads(r["rules"]) if isinstance(r["rules"], str) else r["rules"],
                        trigger_reason="HISTORICAL_PRIMING",
                        trigger_event_id=None,
                        watermark_ns=latest_ingest_ts,
                    )

    async def _event_consumer_loop(self) -> None:
        """Reads closed_bar_committed stream and processes active scan specs."""
        stream = "cryptonite:stream:closed_bar_committed"
        while self._running:
            try:
                if not self.redis:
                    await asyncio.sleep(1.0)
                    continue

                # 1. Autoclaim pending messages from crashed workers (> 60s idle)
                try:
                    autoclaim_res = await self.redis.xautoclaim(
                        stream,
                        self.consumer_group,
                        self.consumer_name,
                        min_idle_time=60000,
                        start_id="0-0",
                        count=10,
                    )
                    if autoclaim_res and len(autoclaim_res) > 1 and autoclaim_res[1]:
                        for msg_id, msg_data in autoclaim_res[1]:
                            await self._handle_stream_message(stream, msg_id, msg_data)
                except Exception as ex:
                    logger.debug("xautoclaim check: %s", ex)

                # 2. Read new messages
                entries = await self.redis.xreadgroup(
                    self.consumer_group,
                    self.consumer_name,
                    {stream: ">"},
                    count=10,
                    block=1000,
                )
                if not entries:
                    continue

                for _, messages in entries:
                    for msg_id, msg_data in messages:
                        await self._handle_stream_message(stream, msg_id, msg_data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in event consumer loop: %s", e)
                await asyncio.sleep(1.0)

    async def _handle_stream_message(self, stream: str, msg_id: str, msg_data: dict[str, Any]) -> None:
        """Process a single closed_bar_committed stream entry and call XACK only after DB commit."""
        raw_payload = msg_data.get("data")
        if not raw_payload:
            await self.redis.xack(stream, self.consumer_group, msg_id)  # type: ignore
            return

        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        event_id = payload.get("event_id", str(uuid4()))
        instrument_id = payload.get("instrument_id", "BTCUSDT.BINANCE")
        bar_close_ns = payload.get("bar_close_inclusive_ns", time.time_ns())
        bar_timeframe = payload.get("timeframe")

        if not bar_timeframe or bar_timeframe not in ALLOWED_AUTOMATIC_TIMEFRAMES:
            logger.debug(
                "Skipping committed bar event %s: invalid or missing timeframe %r (allowed: %s)",
                event_id,
                bar_timeframe,
                ALLOWED_AUTOMATIC_TIMEFRAMES,
            )
            if self.redis:
                await self.redis.xack(stream, self.consumer_group, msg_id)
                self.processed_events_count += 1
            return

        # Load active scan spec versions deterministically ordered
        if not self.pool:
            return
        active_specs = await self.pool.fetch(
            """
            SELECT s.slug, s.active_version_id, s.created_at, v.id as version_id, v.universe, v.rules, v.spec_hash
            FROM scan_specs s
            JOIN scan_spec_versions v ON s.active_version_id = v.id
            WHERE s.is_archived = false
            ORDER BY s.created_at ASC, s.slug ASC;
            """
        )

        cap = await active_spec_cap(self.pool)
        if len(active_specs) > cap:
            logger.error(
                "Active specs count (%d) exceeds the active-spec cap (%d)! "
                "Processing all %d active specs to avoid dropping trading signals, "
                "but operator action is required to archive excess specs.",
                len(active_specs),
                cap,
                len(active_specs),
            )

        for spec in active_specs:
            universe = json.loads(spec["universe"]) if isinstance(spec["universe"], str) else spec["universe"]
            rules = json.loads(spec["rules"]) if isinstance(spec["rules"], str) else spec["rules"]
            allowed_instruments = universe.get("instrument_ids", [])
            if allowed_instruments and instrument_id not in allowed_instruments:
                continue

            spec_timeframes = universe.get("timeframes", [])
            if bar_timeframe and spec_timeframes and bar_timeframe not in spec_timeframes:
                continue

            await self._execute_scan(
                spec_version_id=spec["version_id"],
                spec_hash=spec["spec_hash"],
                universe_data=universe,
                rules_data=rules,
                trigger_reason="COMMITTED_BAR_EVENT",
                trigger_event_id=event_id,
                watermark_ns=bar_close_ns,
                bar_timeframe=bar_timeframe,
            )

        # XACK only after database transaction has completed
        if self.redis:
            await self.redis.xack(stream, self.consumer_group, msg_id)
            self.processed_events_count += 1

    async def _job_consumer_loop(self) -> None:
        """Claims pending scan_jobs with FOR UPDATE SKIP LOCKED and executes them."""
        while self._running:
            try:
                if not self.pool:
                    await asyncio.sleep(1.0)
                    continue

                claimed_job = None
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        row = await conn.fetchrow(
                            """
                            WITH claimed AS (
                                SELECT id
                                FROM scan_jobs
                                WHERE status = 'PENDING'
                                ORDER BY created_at ASC
                                LIMIT 1
                                FOR UPDATE SKIP LOCKED
                            )
                            UPDATE scan_jobs
                            SET status = 'RUNNING', started_at = now()
                            FROM claimed
                            WHERE scan_jobs.id = claimed.id
                            RETURNING scan_jobs.id, scan_jobs.request_id, scan_jobs.spec_version_id,
                                      scan_jobs.requested_watermark_ns, scan_jobs.requested_range_start_ns,
                                      scan_jobs.requested_range_end_ns;
                            """
                        )
                        if row:
                            claimed_job = dict(row)

                if not claimed_job:
                    await asyncio.sleep(0.2)
                    continue

                job_id = claimed_job["id"]
                spec_ver_id = claimed_job["spec_version_id"]

                # Fetch spec version
                spec_row = await self.pool.fetchrow(
                    "SELECT universe, rules, spec_hash FROM scan_spec_versions WHERE id = $1",
                    spec_ver_id,
                )
                if not spec_row:
                    await self.pool.execute(
                        "UPDATE scan_jobs SET status = 'FAILED', error_message = 'Spec version not found', completed_at = now() WHERE id = $1",
                        job_id,
                    )
                    continue

                universe = json.loads(spec_row["universe"]) if isinstance(spec_row["universe"], str) else spec_row["universe"]
                rules = json.loads(spec_row["rules"]) if isinstance(spec_row["rules"], str) else spec_row["rules"]

                run_id = await self._execute_scan(
                    spec_version_id=spec_ver_id,
                    spec_hash=spec_row["spec_hash"],
                    universe_data=universe,
                    rules_data=rules,
                    trigger_reason="MANUAL_JOB",
                    trigger_event_id=None,
                    watermark_ns=claimed_job.get("requested_watermark_ns"),
                    scan_job_id=job_id,
                )

                await self.pool.execute(
                    "UPDATE scan_jobs SET status = 'COMPLETED', scan_run_id = $2, completed_at = now() WHERE id = $1",
                    job_id,
                    run_id,
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in job consumer loop: %s", e)
                await asyncio.sleep(1.0)

    async def _execute_scan(
        self,
        spec_version_id: UUID,
        spec_hash: str,
        universe_data: dict[str, Any],
        rules_data: list[dict[str, Any]],
        trigger_reason: str,
        trigger_event_id: str | None,
        watermark_ns: int | None = None,
        scan_job_id: UUID | None = None,
        bar_timeframe: str | None = None,
    ) -> UUID:
        """Core execution logic: loads 1m lake data, resamples, evaluates rules, commits run & candidates."""
        if not self.pool:
            raise RuntimeError("Database pool not connected")

        run_id = uuid4()
        source_instruments = universe_data.get("instrument_ids", ["BTCUSDT.BINANCE"])
        source_timeframes = universe_data.get("timeframes", ["1m", "5m", "15m", "1h"])

        if trigger_reason == "COMMITTED_BAR_EVENT":
            # Automatic scan on a committed closed bar event:
            # strictly evaluate only the committed allowed event timeframe.
            # Refuse missing, None, or disallowed timeframes.
            if bar_timeframe in ALLOWED_AUTOMATIC_TIMEFRAMES and bar_timeframe in source_timeframes:
                target_timeframes = [bar_timeframe]
            else:
                target_timeframes = []
        else:
            # Manual jobs, priming, or backfills: evaluate configured timeframes
            target_timeframes = source_timeframes

        if not target_timeframes:
            logger.debug(
                "No eligible timeframes to scan for spec %s (trigger=%s, bar_tf=%s)",
                spec_version_id,
                trigger_reason,
                bar_timeframe,
            )
            return run_id

        # Parse rule groups
        parsed_groups: list[RuleGroup] = []
        for rg in rules_data:
            conditions = [
                RuleCondition(
                    field=c["field"],
                    operator=RuleOperator(c["operator"]),
                    value=c["value"],
                    lookback=c.get("lookback", 0),
                )
                for c in rg.get("conditions", [])
            ]
            parsed_groups.append(
                RuleGroup(
                    category=rg.get("category", "trigger"),
                    logic=rg.get("logic", "AND"),
                    conditions=conditions,
                )
            )

        risk_config = RiskConfig()
        source_partition_paths: list[str] = []
        source_partition_checksums: dict[str, str] = {}
        all_candidates = []
        max_seen_watermark = watermark_ns or 0

        frames_by_inst: dict[str, dict[str, Any]] = {}  # enriched frames, for the market brief
        # Only the history the indicators need: EMA-200 plus warm-up on the largest timeframe. Loading the
        # whole lake on every run grew with every backfilled day.
        all_eval_tfs = list(set(target_timeframes + [HTF_MAP[tf] for tf in target_timeframes if tf in HTF_MAP]))
        lookback_min = SCAN_LOOKBACK_BARS * max(TIMEFRAME_MINUTES.get(tf, 60) for tf in (all_eval_tfs or source_timeframes))
        start_ns = time.time_ns() - lookback_min * 60 * 1_000_000_000
        since_date = time.strftime("%Y-%m-%d", time.gmtime(start_ns / 1e9))
        precisions = await self._load_instrument_precisions(source_instruments)
        for inst in source_instruments:
            inst_price_prec = precisions.get(inst)
            if inst_price_prec is None:
                logger.warning(
                    "Instrument %s precision metadata missing or inactive in database. Failing closed.",
                    inst,
                )
            raw_symbol = inst.split(".")[0]
            # 1. Fetch 1m bars from Analytics Lake via DuckDB
            df_1m = self.query_engine.query_bars_df(symbol=raw_symbol, timeframe="1m", start_ns=start_ns)
            if df_1m.is_empty():
                continue

            # Provenance: one digest per date partition read, over (file name, file sha256) of its files
            inst_lake_dir = self.lake_path / "binance_klines_enriched" / "binance" / "spot" / "1m" / raw_symbol
            if inst_lake_dir.exists():
                for part, digest in self._partition_digests(inst_lake_dir, since_date).items():
                    source_partition_paths.append(part)
                    source_partition_checksums[part] = digest

            max_bar_ts = int(df_1m["ts_event_ns"].max()) if "ts_event_ns" in df_1m.columns else 0
            max_seen_watermark = max(max_seen_watermark, max_bar_ts)

            # 2. Resample and evaluate for each timeframe
            for tf in target_timeframes:
                # a bar still open at scan time is neither a signal nor brief context (B1 / F-C1)
                resampled_df = resample_1m_bars(df_1m, tf).filter(
                    pl.col("interval_end_exclusive_ns") <= time.time_ns()
                )
                if resampled_df.is_empty():
                    continue
                enriched_df = ScannerEngine.enrich_indicators(resampled_df)
                frames_by_inst.setdefault(inst, {})[tf] = enriched_df

                # Higher-timeframe context for structure target
                htf_tf = HTF_MAP.get(tf)
                htf_df = None
                if htf_tf:
                    if htf_tf in frames_by_inst.get(inst, {}):
                        htf_df = frames_by_inst[inst][htf_tf]
                    else:
                        resampled_htf = resample_1m_bars(df_1m, htf_tf).filter(
                            pl.col("interval_end_exclusive_ns") <= time.time_ns()
                        )
                        if not resampled_htf.is_empty():
                            htf_df = ScannerEngine.enrich_indicators(resampled_htf)
                            frames_by_inst.setdefault(inst, {})[htf_tf] = htf_df

                try:
                    candidates = ScannerEngine.evaluate(
                        df=enriched_df,
                        rule_groups=parsed_groups,
                        risk_config=risk_config,
                        instrument_id=inst,
                        timeframe=tf,
                        spec_version_id=spec_version_id,
                        scan_run_id=run_id,
                        price_precision=inst_price_prec,
                        htf_df=htf_df,
                    )
                except TypeError as te:
                    if "htf_df" in str(te):
                        candidates = ScannerEngine.evaluate(
                            df=enriched_df,
                            rule_groups=parsed_groups,
                            risk_config=risk_config,
                            instrument_id=inst,
                            timeframe=tf,
                            spec_version_id=spec_version_id,
                            scan_run_id=run_id,
                            price_precision=inst_price_prec,
                        )
                    else:
                        raise
                all_candidates.extend(candidates)

        now_ns = time.time_ns()
        all_candidates = drop_unclosed_candidates(all_candidates, now_ns)

        # 1. Pre-insert qualification gate: evaluate unrounded derived price ratio for each candidate.
        # Reject candidates failing R:R >= MIN_RISK_REWARD_RATIO BEFORE deduplication, so a passing
        # candidate on the same bar is not dropped in favor of a rejected duplicate.
        min_rr_dec = Decimal(str(MIN_RISK_REWARD_RATIO))
        qualified_candidates: list[SignalCandidateRead] = []
        for c in all_candidates:
            risk = abs(c.suggested_entry - c.suggested_stop)
            reward = abs(c.suggested_target - c.suggested_entry)
            derived_rr = (reward / risk) if risk > Decimal("0") else Decimal("0")
            if derived_rr < min_rr_dec:
                logger.info(
                    "Rejected candidate %s for %s %s at %d: R:R %.4f < %.2f "
                    "(entry=%s, stop=%s [%s], target=%s [%s])",
                    c.id,
                    c.instrument_id,
                    c.timeframe,
                    c.bar_close_inclusive_ns,
                    float(derived_rr),
                    MIN_RISK_REWARD_RATIO,
                    c.suggested_entry,
                    c.suggested_stop,
                    c.metrics.get("stop_type"),
                    c.suggested_target,
                    c.metrics.get("target_type"),
                )
                continue
            qualified_candidates.append(c)

        # 2. Sort qualified candidates deterministically (preferring higher R:R on duplicate bars)
        def _candidate_rank_key(c: SignalCandidateRead) -> tuple[str, str, int, Decimal, UUID]:
            risk = abs(c.suggested_entry - c.suggested_stop)
            reward = abs(c.suggested_target - c.suggested_entry)
            rr = (reward / risk) if risk > Decimal("0") else Decimal("0")
            return (c.instrument_id, c.timeframe, c.bar_close_inclusive_ns, -rr, c.id)

        qualified_candidates.sort(key=_candidate_rank_key)

        # 3. Deduplicate qualified candidates across specs per (instrument, timeframe, bar_close)
        all_candidates = deduplicate_candidates(qualified_candidates)

        # 4. Canonical sort order for pg_advisory_xact_lock to prevent deadlocks across concurrent workers/jobs
        all_candidates.sort(key=lambda c: (c.instrument_id, c.timeframe, c.bar_close_inclusive_ns))
        to_queue: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []  # fresh, new: published after the commit

        # 3. Commit run, candidates, checkpoints, and outbox in a single transaction
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # a. Insert scan_runs
                # Uses ON CONFLICT for event-triggered runs (partial unique index uq_scan_runs_event_triggered)
                if trigger_event_id:
                    inserted_run = await conn.fetchrow(
                        """
                        INSERT INTO scan_runs (
                            id, spec_version_id, spec_hash, scanner_version, indicator_version,
                            input_watermark_ns, source_partitions, source_partition_checksums,
                            trigger_reason, trigger_event_id, status, started_at, completed_at,
                            candidate_count, created_at
                        ) VALUES ($1, $2, $3, '0.2.0', '1.0.0', $4, $5::jsonb, $6::jsonb, $7, $8, 'COMPLETED', now(), now(), $9, now())
                        ON CONFLICT (spec_version_id, trigger_event_id) WHERE trigger_event_id IS NOT NULL
                        DO NOTHING
                        RETURNING id;
                        """,
                        run_id,
                        spec_version_id,
                        spec_hash,
                        max_seen_watermark,
                        json.dumps(source_partition_paths),
                        json.dumps(source_partition_checksums),
                        trigger_reason,
                        trigger_event_id,
                        len(all_candidates),
                    )
                    if not inserted_run:
                        # Duplicate event already executed
                        logger.debug("Duplicate event %s for spec %s ignored", trigger_event_id, spec_version_id)
                        return run_id
                else:
                    await conn.execute(
                        """
                        INSERT INTO scan_runs (
                            id, spec_version_id, spec_hash, scanner_version, indicator_version,
                            input_watermark_ns, source_partitions, source_partition_checksums,
                            trigger_reason, trigger_event_id, status, started_at, completed_at,
                            candidate_count, created_at
                        ) VALUES ($1, $2, $3, '0.2.0', '1.0.0', $4, $5::jsonb, $6::jsonb, $7, NULL, 'COMPLETED', now(), now(), $8, now());
                        """,
                        run_id,
                        spec_version_id,
                        spec_hash,
                        max_seen_watermark,
                        json.dumps(source_partition_paths),
                        json.dumps(source_partition_checksums),
                        trigger_reason,
                        len(all_candidates),
                    )

                # b. Insert candidates with idempotency key and serialized cross-spec deduplication
                for c in all_candidates:
                    # Serialise concurrent candidate insertions for this bar across specs/workers:
                    # Transaction-level advisory lock prevents SELECT-then-INSERT races across jobs.
                    # Lock keys are acquired in sorted order to prevent deadlocks.
                    lock_key = f"signal_candidate:{c.instrument_id}:{c.timeframe}:{c.bar_close_inclusive_ns}"
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0));", lock_key)

                    # Deduplicate across specs per (instrument, timeframe, bar)
                    existing_across_specs = None
                    if hasattr(conn, "fetchval"):
                        existing_across_specs = await conn.fetchval(
                            """
                            SELECT id FROM signal_candidates
                            WHERE instrument_id = $1 AND timeframe = $2 AND bar_close_inclusive_ns = $3
                            LIMIT 1;
                            """,
                            c.instrument_id,
                            c.timeframe,
                            c.bar_close_inclusive_ns,
                        )
                    elif hasattr(conn, "fetchrow"):
                        r = await conn.fetchrow(
                            """
                            SELECT id FROM signal_candidates
                            WHERE instrument_id = $1 AND timeframe = $2 AND bar_close_inclusive_ns = $3
                            LIMIT 1;
                            """,
                            c.instrument_id,
                            c.timeframe,
                            c.bar_close_inclusive_ns,
                        )
                        existing_across_specs = r.get("id") if isinstance(r, dict) else (r[0] if r else None)
                    if existing_across_specs:
                        logger.debug(
                            "Candidate %s for %s %s %d skipped: already exists across specs (%s)",
                            c.id,
                            c.instrument_id,
                            c.timeframe,
                            c.bar_close_inclusive_ns,
                            existing_across_specs,
                        )
                        continue

                    res = await conn.fetchrow(
                        """
                        INSERT INTO signal_candidates (
                            id, scan_run_id, spec_version_id, instrument_id, timeframe,
                            bar_open_ns, bar_close_inclusive_ns, interval_end_exclusive_ns,
                            matched_rules, metrics, suggested_entry, suggested_stop,
                            suggested_target, created_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11, $12, $13, now())
                        ON CONFLICT (spec_version_id, instrument_id, timeframe, bar_close_inclusive_ns)
                        DO NOTHING
                        RETURNING id;
                        """,
                        c.id,
                        run_id,
                        spec_version_id,
                        c.instrument_id,
                        c.timeframe,
                        c.bar_open_ns,
                        c.bar_close_inclusive_ns,
                        c.interval_end_exclusive_ns,
                        json.dumps(c.matched_rules),
                        json.dumps(c.metrics),
                        c.suggested_entry,
                        c.suggested_stop,
                        c.suggested_target,
                    )

                    # c. If candidate was newly inserted, create outbox event
                    if res:
                        self.detected_candidates_count += 1
                        outbox_id = str(uuid4())
                        candidate_payload = {
                            "schema_version": 1,
                            "producer": "cryptonite-scanner",
                            "occurred_at_ns": time.time_ns(),
                            "event_id": outbox_id,
                            "event_type": "signal_candidate",
                            "candidate_id": str(c.id),
                            "scan_run_id": str(run_id),
                            "spec_version_id": str(spec_version_id),
                            "instrument_id": c.instrument_id,
                            "timeframe": c.timeframe,
                            "bar_open_ns": c.bar_open_ns,
                            "bar_close_inclusive_ns": c.bar_close_inclusive_ns,
                            "interval_end_exclusive_ns": c.interval_end_exclusive_ns,
                            "suggested_entry": str(c.suggested_entry),
                            "suggested_stop": str(c.suggested_stop),
                            "suggested_target": str(c.suggested_target),
                            "matched_rules": c.matched_rules,
                            "metrics": c.metrics,
                            # R7.4: follows this setup through gate, triage, debate, approval, command and fill
                            "decision_trace_id": str(uuid4()),
                        }

                        # AI Debate Engine: only a bar that just closed is worth a debate (D1 / F-H2). The
                        # outbox row commits with the candidate; the queue write happens after the commit.
                        if is_fresh(c, now_ns):
                            fields = {"instrument_id": c.instrument_id, "trigger_source": "scanner",
                                      "candidate_payload": candidate_payload}
                            await conn.execute(
                                """
                                INSERT INTO scanner_debate_outbox (candidate_id, fields, attempt_count, next_attempt_at)
                                VALUES ($1, $2::jsonb, 1, now() + make_interval(secs => $3));
                                """,
                                c.id,
                                json.dumps(fields),
                                float(DEBATE_RELAY_LEASE_S),
                            )
                            to_queue.append((c, fields, frames_by_inst.get(c.instrument_id, {})))

                        await conn.execute(
                            """
                            INSERT INTO outbox_events (
                                event_id, stream_name, event_type, producer, payload,
                                status, attempt_count, next_attempt_at, created_at
                            ) VALUES ($1, $2, $3, $4, $5::jsonb, 'PENDING', 0, now(), now());
                            """,
                            outbox_id,
                            "cryptonite:stream:signal_candidate",
                            "signal_candidate",
                            "cryptonite-scanner",
                            json.dumps(candidate_payload),
                        )

                # d. Update scanner checkpoints
                for inst in source_instruments:
                    await conn.execute(
                        """
                        INSERT INTO scanner_checkpoints (
                            spec_version_id, instrument_id, source_timeframe,
                            last_processed_watermark_ns, last_run_id, updated_at
                        ) VALUES ($1, $2, '1m', $3, $4, now())
                        ON CONFLICT (spec_version_id, instrument_id, source_timeframe)
                        DO UPDATE SET
                            last_processed_watermark_ns = GREATEST(scanner_checkpoints.last_processed_watermark_ns, EXCLUDED.last_processed_watermark_ns),
                            last_run_id = EXCLUDED.last_run_id,
                            updated_at = now();
                        """,
                        spec_version_id,
                        inst,
                        max_seen_watermark,
                        run_id,
                    )

        if to_queue:
            await self._publish_debate_candidates(to_queue)

        self.executed_runs_count += 1
        logger.info("Executed scan run %s for spec %s: found %d candidates, %d fresh for debate",
                    run_id, spec_version_id, len(all_candidates), len(to_queue))
        return run_id
