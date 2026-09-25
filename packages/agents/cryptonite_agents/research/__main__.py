"""Research CLI.

    python -m cryptonite_agents.research run --idea "..." --instrument BTCUSDT.BINANCE --timeframe 1h
        [--is-days 90] [--oos-days 30] [--trials 12]
    python -m cryptonite_agents.research enqueue --idea "..." --instrument ... --timeframe ...
    python -m cryptonite_agents.research worker        # works off research:queue
    python -m cryptonite_agents.research digest [--hours 24]

Needs API_BASE_URL + CRYPTONITE_AGENT_API_TOKEN (operator-scoped machine token), REDIS_URL, and
AI_TELEMETRY_DATABASE_URL for the gateway's ledger.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict

import redis.asyncio as aioredis
from redis.exceptions import RedisError
from cryptonite_agents.gateway import BudgetGuard, Gateway
from cryptonite_agents.research.client import ResearchApiClient
from cryptonite_agents.research.loop import ResearchLoop
from cryptonite_agents.research.queue import enqueue, publish_digest, spec_from, work_once


async def _gateway(redis) -> Gateway:
    url = os.getenv("AI_TELEMETRY_DATABASE_URL")
    pool = None
    if url:
        import asyncpg

        pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
    return Gateway(pool=pool, redis=redis, budget=BudgetGuard(pool=pool, redis=redis, spend_unknown=pool is None))


async def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(prog="cryptonite_agents.research")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("run", "enqueue"):
        p = sub.add_parser(name)
        p.add_argument("--idea", required=True)
        p.add_argument("--instrument", required=True)
        p.add_argument("--timeframe", required=True)
        p.add_argument("--is-days", type=int, default=90)
        p.add_argument("--oos-days", type=int, default=30)
        p.add_argument("--trials", type=int, default=12)
    sub.add_parser("worker")
    d = sub.add_parser("digest")
    d.add_argument("--hours", type=int, default=24)
    args = parser.parse_args(argv)

    # The socket timeout must outlast the queue's blocking pop: redis-py 8 otherwise times the read out
    # at exactly the BLPOP timeout, racing Redis's empty reply
    redis = aioredis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_timeout=30)
    try:
        if args.cmd == "digest":
            print(json.dumps(await publish_digest(redis, hours=args.hours), indent=2))
            return 0
        if args.cmd == "enqueue":
            pos = await enqueue(redis, args.idea, args.instrument, args.timeframe,
                                is_days=args.is_days, oos_days=args.oos_days, n_trials=args.trials)
            print(f"queued at position {pos}")
            return 0
        loop = ResearchLoop(await _gateway(redis), ResearchApiClient())
        if args.cmd == "run":
            item = {"idea": args.idea, "instrument_id": args.instrument, "timeframe": args.timeframe,
                    "is_days": args.is_days, "oos_days": args.oos_days, "n_trials": args.trials}
            print(json.dumps(asdict(await loop.run(spec_from(item))), indent=2, default=str))
            return 0
        while True:  # worker: a Redis blip must not kill it
            try:
                await work_once(redis, loop)
            except (RedisError, OSError) as e:
                logging.warning("research queue unavailable (%s); retrying in 5 s", e)
                await asyncio.sleep(5)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
