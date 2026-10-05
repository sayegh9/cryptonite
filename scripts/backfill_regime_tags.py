"""F1: tag already-labelled decision outcomes with the market regime at decision time.

The learner stores the regime with every new label (migration 034); this fills rows labelled before
that, from the 1h analytics lake. Idempotent (only rows with ``regime IS NULL``); decisions without
enough 1h history stay untagged. Reporting only.

    docker compose -f infra/docker-compose.yml run --rm --no-deps test \\
        uv run python scripts/backfill_regime_tags.py [--dry-run]
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
from cryptonite_storage.queries import AnalyticsQueryEngine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "debate_worker"))
from outcomes import backfill_regimes  # noqa: E402


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--lake", default=os.getenv("ANALYTICS_LAKE_PATH", "data/analytics_lake"))
    args = p.parse_args()
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    try:
        counts = await backfill_regimes(pool, AnalyticsQueryEngine(Path(args.lake)), dry_run=args.dry_run)
    finally:
        await pool.close()
    print(json.dumps({**counts, "dry_run": args.dry_run}))


if __name__ == "__main__":
    asyncio.run(main())
