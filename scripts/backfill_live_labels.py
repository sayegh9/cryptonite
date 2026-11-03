#!/usr/bin/env python3
"""H11: fill ``candidate_outcomes.net_r_live`` (migration 062) on existing labels, oldest signal first.

Read-only by default: computes the live-parity label (``cryptonite_schemas.exit_rules.simulate_live_exit`` under the
exits of the profile holding each coin now) for one batch and prints a summary; nothing is written. ``--apply`` writes
the four 062 columns on rows where ``live_exit_model IS NULL`` and repeats until none are left (idempotent: a second
run finds nothing). The 055 / 057 columns are never touched. The analytics lake is only read. The learner does the
same one batch per pass, so ``--apply`` only speeds the backfill up.

Run in the learner container (it has the lake path and the database role) after migration 062; the image has no
``scripts/``, so the script goes in on stdin:

    docker compose -f infra/docker-compose.yml exec -T learner python - < scripts/backfill_live_labels.py
    docker compose -f infra/docker-compose.yml exec -T learner python - --apply < scripts/backfill_live_labels.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

# On stdin (``python - < scripts/...``, the documented way in) there is no __file__: the container cwd is /app
_HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd() / "scripts"
for _path in (_HERE.parent / "services" / "debate_worker", _HERE):
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import asyncpg  # noqa: E402
from candidate_outcomes import backfill_live_labels  # noqa: E402
from cryptonite_storage.queries import AnalyticsQueryEngine  # noqa: E402


async def main(apply: bool, batch: int, max_batches: int) -> None:
    lake = AnalyticsQueryEngine(Path(os.getenv("ANALYTICS_LAKE_PATH", "/app/data/analytics_lake")))
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"),
                                     min_size=1, max_size=2)
    outcomes: Counter[str] = Counter()
    values: list[float] = []
    try:
        for _ in range(max_batches if apply else 1):
            rows = await backfill_live_labels(pool, lake, batch, dry_run=not apply)
            for _, net, outcome, _, _ in rows:
                outcomes[outcome or "NONE"] += 1
                if net is not None:
                    values.append(float(net))
            if not apply or len(rows) < batch:
                break
    finally:
        await pool.close()
    verb = "wrote" if apply else "would write (dry run, first batch only)"
    mean = f"{sum(values) / len(values):+.3f}" if values else "n/a"
    print(f"{verb} {sum(outcomes.values())} rows; net_r_live on {len(values)}, mean {mean}; outcomes {dict(outcomes)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the 062 columns (default: dry run)")
    parser.add_argument("--batch", type=int, default=300)
    parser.add_argument("--max-batches", type=int, default=200)
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.batch, args.max_batches))
