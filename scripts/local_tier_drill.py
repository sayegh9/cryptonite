"""A6.1 fallback drill ("pull the plug"): send a small triage-style call every few seconds through the
real Gateway with a local primary and a cloud fallback, and print which model answered and how the
gateway sees the local tier. Stop and start Ollama on the host while it runs.

Run from the repo root (the test image mounts the repo; 172.17.0.1 is the docker bridge Ollama binds to):

    docker compose -f infra/docker-compose.yml run --rm --no-deps \\
        -e OLLAMA_BASE_URL=http://172.17.0.1:11434/v1 -e REDIS_URL="redis://:$REDIS_PASSWORD@redis:6379/0" \\
        test uv run python scripts/local_tier_drill.py --seconds 180

    # on the host, while it runs:
    sudo systemctl stop ollama     # expect: tier=DOWN, answers from the cloud fallback, no long stalls
    sudo systemctl start ollama    # expect: tier=up again within ~5 s, answers from the local model

Health is published to Redis ``local_tier:health``, so Mission Control's /ai/status shows the same state.
The fallback costs a few Flash-Lite tokens per call; nothing is written to the ledger (no DB pool).
"""

import argparse
import asyncio
import os
import time
from collections import Counter

import redis.asyncio as aioredis
from cryptonite_agents.gateway import (
    BudgetGuard,
    BudgetPriority,
    FallbackPolicy,
    Gateway,
    RoleRoute,
)
from cryptonite_agents.gateway.local_tier import LocalTierHealth

PROMPT = "Answer with one word, bullish or bearish: 'BTC breaks out to a new all-time high on rising volume.'"


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="ollama:gemma4:12b-it-qat")
    p.add_argument("--fallback", default="google:gemini-2.5-flash-lite")
    p.add_argument("--seconds", type=float, default=180)
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--timeout", type=float, default=30, help="per-call model timeout")
    args = p.parse_args()

    redis = aioredis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_timeout=5)
    health = LocalTierHealth(redis=redis, ttl_s=5)
    route = RoleRoute("triage", args.model, args.fallback, FallbackPolicy.DEGRADE, BudgetPriority.DEBATE,
                      max_output_tokens=16, thinking_budget=0, timeout_s=args.timeout)
    gw = Gateway(routes={"triage": route}, budget=BudgetGuard(default_cap_usd=1), local_tier=health)

    print(f"drill: primary={args.model} fallback={args.fallback} for {args.seconds:.0f}s, every {args.interval:.0f}s",
          flush=True)
    answered = Counter()
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        started = time.monotonic()
        try:
            res = await gw.run("triage", PROMPT)
            answered[res.model] += 1
            line = f"{res.model:<34} fell_back={str(res.fell_back):<5} {res.latency_ms:>6} ms  {str(res.output).strip()[:24]!r}"
        except Exception as e:  # the drill reports, it never stops on a failed call
            answered["ERROR"] += 1
            line = f"ERROR {type(e).__name__}: {e}"[:140]
        s = health.status
        tier = "up" if s and s.healthy else "DOWN"
        print(f"{time.strftime('%H:%M:%S')} tier={tier:<4} {line}", flush=True)
        await asyncio.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    print("answered by:", dict(answered), flush=True)
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
