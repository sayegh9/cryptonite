"""R2.4 / D3: queue a fresh run of completed backtests so they get v2 metrics (mark-to-market equity,
daily grid, benchmark, fees). The old run and its results are left as they are (badged "legacy").

Each selected run is copied into a new PENDING ``backtest_runs`` row with the same strategy version,
instrument, bar type, parameters, windows and execution profile; the backtest worker runs it.

    uv run python scripts/rerun_backtests.py --run-id <uuid> [--run-id <uuid> ...]
    uv run python scripts/rerun_backtests.py --since 2026-09-01 [--include-v2] [--dry-run]
    uv run python scripts/rerun_backtests.py --run-id <uuid> --recent-days 90 [--warmup-days 10] [--profile conservative]

By default only legacy runs (no ``metrics_version`` in their results) are selected. ``--recent-days``
replaces the original windows with the last N days (plus warm-up) when the original period is no
longer in the catalog; ``--profile`` overrides the execution profile.
"""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
from cryptonite_backtester.execution_profiles import resolve_execution_profile
from cryptonite_backtester.runner import BacktestRunRequest, compute_run_config_hash

SELECT = """
SELECT br.id, br.strategy_version_id, br.instrument_id, br.bar_type, br.timeframe, br.parameters,
       br.warmup_start_ns, br.evaluation_start_ns, br.evaluation_end_ns, br.execution_profile,
       s.slug AS strategy_slug, res.artifact_manifest->'metrics'->>'metrics_version' AS metrics_version
FROM backtest_runs br
JOIN strategy_versions sv ON sv.id = br.strategy_version_id
JOIN strategies s ON s.id = sv.strategy_id
JOIN backtest_results res ON res.backtest_run_id = br.id
WHERE br.status = 'COMPLETED' AND ({where})
ORDER BY br.created_at
"""

INSERT = """
INSERT INTO backtest_runs (
    id, strategy_version_id, instrument_id, bar_type, timeframe, parameters,
    warmup_start_ns, evaluation_start_ns, evaluation_end_ns, execution_profile, run_config_hash, status
) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, 'PENDING')
"""


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-id", action="append", default=[], type=UUID, help="run to re-run (repeatable)")
    p.add_argument("--since", type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
                   help="re-run every completed run created at or after this date (UTC)")
    p.add_argument("--include-v2", action="store_true", help="also re-run runs that already have v2 metrics")
    p.add_argument("--dry-run", action="store_true", help="list the runs, queue nothing")
    p.add_argument("--recent-days", type=int, help="evaluate the last N days instead of the original window")
    p.add_argument("--warmup-days", type=int, default=10, help="warm-up before --recent-days (default 10)")
    p.add_argument("--profile", choices=("optimistic", "adverse_slippage", "conservative"),
                   help="execution profile for the new runs (default: the original run's)")
    args = p.parse_args(argv)
    if not args.run_id and args.since is None:
        p.error("give --run-id and/or --since")
    return args


def recent_window(days: int, warmup_days: int, now: datetime | None = None) -> tuple[int, int, int]:
    """(warmup_start_ns, evaluation_start_ns, evaluation_end_ns) ending at the last whole minute
    before ``now`` minus 5 minutes, so the window stays clear of the bar still being recorded."""
    end = (now or datetime.now(UTC)).replace(second=0, microsecond=0) - timedelta(minutes=5)
    start = end - timedelta(days=days)
    warmup = start - timedelta(days=warmup_days)
    ns = lambda t: int(t.timestamp()) * 1_000_000_000  # noqa: E731
    return ns(warmup), ns(start), ns(end)


async def rerun(conn: asyncpg.Connection, run_ids: list[UUID], since: datetime | None, *,
                include_v2: bool = False, dry_run: bool = False, recent_days: int | None = None,
                warmup_days: int = 10, profile: str | None = None) -> list[tuple[UUID, UUID | None]]:
    """(old run id, new run id or None on a dry run) for every selected run."""
    clauses, params = [], []
    if run_ids:
        params.append(run_ids)
        clauses.append(f"br.id = ANY(${len(params)}::uuid[])")
    if since is not None:
        params.append(since)
        clauses.append(f"br.created_at >= ${len(params)}")
    rows = await conn.fetch(SELECT.format(where=" OR ".join(clauses)), *params)
    queued: list[tuple[UUID, UUID | None]] = []
    for r in rows:
        if r["metrics_version"] is not None and not include_v2:
            continue
        if dry_run:
            queued.append((r["id"], None))
            continue
        parameters = json.loads(r["parameters"]) if isinstance(r["parameters"], str) else r["parameters"]
        new_id = uuid4()
        if recent_days:
            warmup_ns, start_ns, end_ns = recent_window(recent_days, warmup_days)
        else:
            warmup_ns, start_ns, end_ns = r["warmup_start_ns"], r["evaluation_start_ns"], r["evaluation_end_ns"]
        exec_profile = profile or r["execution_profile"]
        req = BacktestRunRequest(
            run_id=new_id, strategy_slug=r["strategy_slug"], strategy_version_id=r["strategy_version_id"],
            instrument_id=r["instrument_id"], bar_type=r["bar_type"], timeframe=r["timeframe"],
            parameters=parameters, warmup_start_ns=warmup_ns,
            evaluation_start_ns=start_ns, evaluation_end_ns=end_ns,
            execution_profile=exec_profile,
        )
        _, meta = resolve_execution_profile(exec_profile, starting_balance="10000.00 USDT", random_seed=42)
        await conn.execute(INSERT, new_id, r["strategy_version_id"], r["instrument_id"], r["bar_type"],
                           r["timeframe"], json.dumps(parameters), warmup_ns, start_ns,
                           end_ns, exec_profile, compute_run_config_hash(req, meta))
        queued.append((r["id"], new_id))
    return queued


async def main(argv: list[str] | None = None) -> None:
    args = _parse(argv)
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        done = await rerun(conn, args.run_id, args.since, include_v2=args.include_v2, dry_run=args.dry_run,
                           recent_days=args.recent_days, warmup_days=args.warmup_days, profile=args.profile)
    finally:
        await conn.close()
    for old, new in done:
        print(f"{old} -> {new or '(dry run)'}")
    print(f"{len(done)} run(s) {'selected' if args.dry_run else 'queued'}")


if __name__ == "__main__":
    asyncio.run(main())
