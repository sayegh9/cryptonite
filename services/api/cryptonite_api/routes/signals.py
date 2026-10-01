"""Signal candidates query route."""

import json
from uuid import UUID

from cryptonite_scanner.models import SignalCandidateRead
from cryptonite_api.routes.auth import AUTHED_READ, require_operator, require_csrf
from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/v1/signals", tags=["signals"])


@router.get("", dependencies=AUTHED_READ)
async def get_signals(
    request: Request,
    symbol: str | None = Query(None, description="Symbol e.g. BTCUSDT"),
    instrument_id: str | None = Query(None, description="Exact instrument ID e.g. BTCUSDT.BINANCE"),
    timeframe: str | None = Query(None, description="Timeframe e.g. 15m, 1h"),
    spec_version_id: UUID | None = Query(None, description="Filter by spec version"),
    limit: int = Query(100, le=500, description="Max results to return"),
) -> list[SignalCandidateRead]:
    """Query recent signal candidates with optional filters."""
    pool = request.app.state.pool
    clauses = []
    args = []

    if instrument_id:
        args.append(instrument_id)
        clauses.append(f"instrument_id = ${len(args)}")
    elif symbol:
        inst = f"{symbol.upper()}.BINANCE"
        args.append(inst)
        clauses.append(f"instrument_id = ${len(args)}")

    if timeframe:
        args.append(timeframe.lower())
        clauses.append(f"timeframe = ${len(args)}")

    if spec_version_id:
        args.append(spec_version_id)
        clauses.append(f"spec_version_id = ${len(args)}")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    args.append(limit)
    limit_clause = f"LIMIT ${len(args)}"

    query = f"""
    SELECT id, scan_run_id, spec_version_id, instrument_id, timeframe,
           bar_open_ns, bar_close_inclusive_ns, interval_end_exclusive_ns,
           matched_rules, metrics, suggested_entry, suggested_stop, suggested_target,
           created_at
    FROM signal_candidates
    {where_sql}
    ORDER BY created_at DESC
    {limit_clause};
    """

    rows = await pool.fetch(query, *args)
    return [
        SignalCandidateRead(
            id=r["id"],
            scan_run_id=r["scan_run_id"],
            spec_version_id=r["spec_version_id"],
            instrument_id=r["instrument_id"],
            timeframe=r["timeframe"],
            bar_open_ns=r["bar_open_ns"],
            bar_close_inclusive_ns=r["bar_close_inclusive_ns"],
            interval_end_exclusive_ns=r["interval_end_exclusive_ns"],
            matched_rules=json.loads(r["matched_rules"]) if isinstance(r["matched_rules"], str) else r["matched_rules"],
            metrics=json.loads(r["metrics"]) if isinstance(r["metrics"], str) else r["metrics"],
            suggested_entry=r["suggested_entry"],
            suggested_stop=r["suggested_stop"],
            suggested_target=r["suggested_target"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.get("/{candidate_id}", dependencies=AUTHED_READ)
async def get_signal_detail(candidate_id: UUID, request: Request) -> SignalCandidateRead:
    """Get single candidate detail by ID."""
    pool = request.app.state.pool
    r = await pool.fetchrow(
        """
        SELECT id, scan_run_id, spec_version_id, instrument_id, timeframe,
               bar_open_ns, bar_close_inclusive_ns, interval_end_exclusive_ns,
               matched_rules, metrics, suggested_entry, suggested_stop, suggested_target,
               created_at
        FROM signal_candidates
        WHERE id = $1;
        """,
        candidate_id,
    )
    if not r:
        raise HTTPException(status_code=404, detail="Candidate not found")

    return SignalCandidateRead(
        id=r["id"],
        scan_run_id=r["scan_run_id"],
        spec_version_id=r["spec_version_id"],
        instrument_id=r["instrument_id"],
        timeframe=r["timeframe"],
        bar_open_ns=r["bar_open_ns"],
        bar_close_inclusive_ns=r["bar_close_inclusive_ns"],
        interval_end_exclusive_ns=r["interval_end_exclusive_ns"],
        matched_rules=json.loads(r["matched_rules"]) if isinstance(r["matched_rules"], str) else r["matched_rules"],
        metrics=json.loads(r["metrics"]) if isinstance(r["metrics"], str) else r["metrics"],
        suggested_entry=r["suggested_entry"],
        suggested_stop=r["suggested_stop"],
        suggested_target=r["suggested_target"],
        created_at=r["created_at"],
    )
