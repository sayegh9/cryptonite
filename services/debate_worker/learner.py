"""Learner service (AI-first blueprint A7.1): labels decision outcomes and writes reflections.

One instance only (the debate worker runs several replicas; reflections must not be written
twice). Every ``LEARNER_INTERVAL_S``: label elapsed decision horizons from the 1m lake, then write
reflections for decisions whose 24h outcome is known. Publishes ``learner:heartbeat``.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import asyncpg
import redis.asyncio as redis
from candidate_outcomes import label_candidates
from cryptonite_agents.gateway import BudgetGuard, Gateway
from cryptonite_storage.queries import AnalyticsQueryEngine
from outcomes import label_pending
from reflection import ReflectionEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("cryptonite.learner")

INTERVAL_S = int(os.getenv("LEARNER_INTERVAL_S", "300"))
HEARTBEAT_KEY = "learner:heartbeat"


async def main() -> None:
    redis_client = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True, socket_timeout=30)
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3)
    telemetry_url = os.getenv("AI_TELEMETRY_DATABASE_URL")
    telemetry_pool = await asyncpg.create_pool(telemetry_url, min_size=1, max_size=2) if telemetry_url else None
    gateway = Gateway(pool=telemetry_pool, redis=redis_client,
                      budget=BudgetGuard(pool=telemetry_pool, redis=redis_client, spend_unknown=telemetry_pool is None))
    lake = AnalyticsQueryEngine(Path(os.getenv("ANALYTICS_LAKE_PATH", "/app/data/analytics_lake")))
    reflections = ReflectionEngine(pool, lake, gateway)
    logger.info("Learner running every %ds", INTERVAL_S)
    while True:
        started = time.monotonic()
        labelled = reflected = cand_labelled = 0
        try:
            labelled = await label_pending(pool, lake)
            try:
                cand_labelled = await label_candidates(pool, lake)
            except Exception as e:  # candidate labels are best-effort
                logger.exception("Candidate labelling failed: %s", e)
            reflected = await reflections.process_pending_reflections()
        except Exception as e:  # one bad pass must not stop learning
            logger.exception("Learner pass failed: %s", e)
        try:
            await redis_client.set(HEARTBEAT_KEY, json.dumps({"at": time.time(), "labelled": labelled,
                                                              "candidates_labelled": cand_labelled,
                                                              "reflections": reflected}), ex=INTERVAL_S * 3)
        except Exception:
            pass
        await asyncio.sleep(max(5.0, INTERVAL_S - (time.monotonic() - started)))


if __name__ == "__main__":
    asyncio.run(main())
