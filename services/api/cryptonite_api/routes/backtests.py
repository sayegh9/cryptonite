"""Backtest execution, results inspection, paginated trades, and comparison endpoints."""

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import duckdb
from cryptonite_api.routes.auth import AUTHED_READ, OPERATOR_WRITE
from cryptonite_backtester.coverage import instrument_defined, window_problem
from cryptonite_backtester.execution_profiles import VALID_PROFILES, resolve_execution_profile
from cryptonite_backtester.runner import BacktestRunRequest, compute_run_config_hash
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1/backtests", tags=["backtests"])

_V2_KEYS = ("metrics_version", "benchmark", "fees_paid", "turnover", "regime_breakdown")


def _v2_metrics(raw: Any) -> dict[str, Any]:
    """metrics_version, benchmark, fees_paid, turnover kept under the artifact manifest's ``metrics``
    key (R2.2/R2.3), plus the per-regime trade breakdown (F1, reporting only; None when absent). A run
    without them is legacy (fill-only equity): only ``metrics_version: None``."""
    extra = json.loads(raw) if isinstance(raw, str) else raw
    extra = extra if isinstance(extra, dict) else {}
    return {k: extra.get(k) for k in _V2_KEYS}

DEFAULT_ARTIFACTS_PATH = "data/backtest_artifacts"


class CreateBacktestRequest(BaseModel):
    """Payload to initiate a new backtest run."""

    strategy_slug: str
    instrument_id: str = "BTCUSDT.BINANCE"
    bar_type: str = "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL"
    timeframe: str = "1m"
    parameters: dict[str, Any] = Field(default_factory=dict)
    warmup_start_ns: int
    evaluation_start_ns: int
    evaluation_end_ns: int
    execution_profile: str = "conservative"  # fees + slippage, as the paper engine; "optimistic" has no costs


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=OPERATOR_WRITE)
async def create_backtest_run(
    req: CreateBacktestRequest,
    request: Request,
) -> dict[str, Any]:
    """Enqueue a new deterministic backtest run."""
    pool = request.app.state.pool

    if req.execution_profile not in VALID_PROFILES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid execution_profile '{req.execution_profile}'. Must be one of {VALID_PROFILES}",
        )
    if req.warmup_start_ns >= req.evaluation_start_ns:
        raise HTTPException(
            status_code=400,
            detail="warmup_start_ns must be strictly less than evaluation_start_ns",
        )
    if req.evaluation_start_ns >= req.evaluation_end_ns:
        raise HTTPException(
            status_code=400,
            detail="evaluation_start_ns must be strictly less than evaluation_end_ns",
        )

    problem = await asyncio.to_thread(window_check, req)
    if problem:
        raise HTTPException(status_code=422, detail=f"No data for this window: {problem}")

    # Locate active strategy version
    strat_row = await pool.fetchrow(
        """
        SELECT sv.id AS version_id
        FROM strategies s
        JOIN strategy_versions sv ON sv.strategy_id = s.id
        WHERE s.slug = $1
        ORDER BY sv.version_num DESC
        LIMIT 1;
        """,
        req.strategy_slug,
    )
    if not strat_row:
        raise HTTPException(
            status_code=404,
            detail=f"Strategy '{req.strategy_slug}' not found.",
        )

    run_id = uuid4()
    version_id = strat_row["version_id"]

    _, profile_meta = resolve_execution_profile(
        profile_name=req.execution_profile,
        venue_name="BINANCE",
        starting_balance="10000.00 USDT",
        random_seed=42,
    )
    b_req = BacktestRunRequest(
        run_id=run_id,
        strategy_version_id=version_id,
        strategy_slug=req.strategy_slug,
        instrument_id=req.instrument_id,
        bar_type=req.bar_type,
        timeframe=req.timeframe,
        parameters=req.parameters,
        warmup_start_ns=req.warmup_start_ns,
        evaluation_start_ns=req.evaluation_start_ns,
        evaluation_end_ns=req.evaluation_end_ns,
        execution_profile=req.execution_profile,
    )
    run_config_hash = compute_run_config_hash(b_req, profile_meta)

    query = """
    INSERT INTO backtest_runs (
        id, strategy_version_id, instrument_id, bar_type, timeframe,
        parameters, warmup_start_ns, evaluation_start_ns, evaluation_end_ns,
        execution_profile, run_config_hash, status
    ) VALUES (
        $1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, 'PENDING'
    )
    RETURNING id, status, created_at;
    """
    row = await pool.fetchrow(
        query,
        run_id,
        version_id,
        req.instrument_id,
        req.bar_type,
        req.timeframe,
        json.dumps(req.parameters),
        req.warmup_start_ns,
        req.evaluation_start_ns,
        req.evaluation_end_ns,
        req.execution_profile,
        run_config_hash,
    )


    return {
        "id": str(row["id"]),
        "status": row["status"],
        "strategy_slug": req.strategy_slug,
        "instrument_id": req.instrument_id,
        "bar_type": req.bar_type,
        "timeframe": req.timeframe,
        "execution_profile": req.execution_profile,
        "created_at": row["created_at"].isoformat(),
    }


@router.get("", dependencies=AUTHED_READ)
async def list_backtest_runs(
    request: Request,
    strategy_slug: str | None = None,
    execution_profile: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List recent backtest runs with execution profiles and summary metrics."""
    pool = request.app.state.pool
    where_clauses = []
    params = []

    if strategy_slug:
        params.append(strategy_slug)
        where_clauses.append(f"s.slug = ${len(params)}")
    if execution_profile:
        params.append(execution_profile)
        where_clauses.append(f"br.execution_profile = ${len(params)}")
    if status:
        params.append(status)
        where_clauses.append(f"br.status = ${len(params)}")

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    params.append(limit)
    limit_clause = f"LIMIT ${len(params)}"

    query = f"""
    SELECT br.id, br.instrument_id, br.bar_type, br.timeframe, br.execution_profile,
           br.status, br.run_config_hash, br.created_at, br.started_at, br.completed_at,
           br.error_message, s.slug AS strategy_slug, s.name AS strategy_name,
           res.total_return, res.sharpe_ratio, res.max_drawdown, res.win_rate, res.total_trades,
           res.artifact_manifest->'metrics' AS v2_metrics
    FROM backtest_runs br
    JOIN strategy_versions sv ON sv.id = br.strategy_version_id
    JOIN strategies s ON s.id = sv.strategy_id
    LEFT JOIN backtest_results res ON res.backtest_run_id = br.id
    {where_sql}
    ORDER BY br.created_at DESC
    {limit_clause};
    """
    rows = await pool.fetch(query, *params)
    results = []
    for r in rows:
        item = {
            "id": str(r["id"]),
            "strategy_slug": r["strategy_slug"],
            "strategy_name": r["strategy_name"],
            "instrument_id": r["instrument_id"],
            "bar_type": r["bar_type"],
            "timeframe": r["timeframe"],
            "execution_profile": r["execution_profile"],
            "status": r["status"],
            "run_config_hash": r["run_config_hash"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            "error_message": r["error_message"],
            "metrics": None,
        }
        if r["total_return"] is not None:
            item["metrics"] = {
                "total_return": float(r["total_return"]),
                "sharpe_ratio": float(r["sharpe_ratio"]) if r["sharpe_ratio"] is not None else None,
                "max_drawdown": float(r["max_drawdown"]),
                "win_rate": float(r["win_rate"]),
                "total_trades": r["total_trades"],
                **_v2_metrics(r["v2_metrics"]),
            }
        results.append(item)
    return results


@router.get("/compare", dependencies=AUTHED_READ)
async def compare_backtest_runs(
    run_ids: str = Query(..., description="Comma-separated run UUIDs"),
    request: Request = None,
) -> dict[str, Any]:
    """Return comparative metrics matrix across multiple backtest runs."""
    pool = request.app.state.pool
    uids = []
    for raw_id in run_ids.split(","):
        clean = raw_id.strip()
        if clean:
            try:
                uids.append(UUID(clean))
            except ValueError:
                pass

    if not uids:
        raise HTTPException(status_code=400, detail="No valid UUIDs provided for comparison.")

    query = """
    SELECT br.id, br.instrument_id, br.timeframe, br.execution_profile, br.status,
           s.slug AS strategy_slug, s.name AS strategy_name, br.parameters,
           res.total_return, res.cagr, res.sharpe_ratio, res.sortino_ratio,
           res.max_drawdown, res.profit_factor, res.win_rate, res.total_trades,
           res.winning_trades, res.losing_trades, res.avg_trade_pnl, res.max_consecutive_losses,
           res.artifact_manifest->'metrics' AS v2_metrics
    FROM backtest_runs br
    JOIN strategy_versions sv ON sv.id = br.strategy_version_id
    JOIN strategies s ON s.id = sv.strategy_id
    LEFT JOIN backtest_results res ON res.backtest_run_id = br.id
    WHERE br.id = ANY($1::uuid[])
    ORDER BY br.created_at ASC;
    """
    rows = await pool.fetch(query, uids)
    runs_comparison = []
    for r in rows:
        runs_comparison.append({
            "id": str(r["id"]),
            "strategy_slug": r["strategy_slug"],
            "strategy_name": r["strategy_name"],
            "instrument_id": r["instrument_id"],
            "timeframe": r["timeframe"],
            "execution_profile": r["execution_profile"],
            "status": r["status"],
            "parameters": json.loads(r["parameters"]) if isinstance(r["parameters"], str) else r["parameters"],
            "metrics": {
                "total_return": float(r["total_return"]) if r["total_return"] is not None else None,
                "cagr": float(r["cagr"]) if r["cagr"] is not None else None,
                "sharpe_ratio": float(r["sharpe_ratio"]) if r["sharpe_ratio"] is not None else None,
                "sortino_ratio": float(r["sortino_ratio"]) if r["sortino_ratio"] is not None else None,
                "max_drawdown": float(r["max_drawdown"]) if r["max_drawdown"] is not None else None,
                "profit_factor": float(r["profit_factor"]) if r["profit_factor"] is not None else None,
                "win_rate": float(r["win_rate"]) if r["win_rate"] is not None else None,
                "total_trades": r["total_trades"],
                "winning_trades": r["winning_trades"],
                "losing_trades": r["losing_trades"],
                "avg_trade_pnl": float(r["avg_trade_pnl"]) if r["avg_trade_pnl"] is not None else None,
                "max_consecutive_losses": r["max_consecutive_losses"],
                **_v2_metrics(r["v2_metrics"]),
            } if r["total_return"] is not None else None,
        })
    return {"runs": runs_comparison}


_BAR_STEP_S = {"MINUTE": 60, "HOUR": 3_600, "DAY": 86_400}


def catalog_history(bar_type: str) -> tuple[tuple[int, int] | None, int]:
    """Contiguous catalog history and bar step (ns) of a bar type; raises 400 for an unsupported bar type."""
    from cryptonite_storage.nautilus_catalog import NautilusCatalogManager

    parts = bar_type.rsplit("-", 4)  # <instrument>-<n>-<UNIT>-LAST-EXTERNAL
    if len(parts) != 5 or not parts[1].isdigit() or parts[2] not in _BAR_STEP_S:
        raise HTTPException(status_code=400, detail=f"Unsupported bar_type '{bar_type}'.")
    step_ns = int(parts[1]) * _BAR_STEP_S[parts[2]] * 1_000_000_000
    mgr = NautilusCatalogManager(os.environ.get("NAUTILUS_CATALOG_PATH", "/app/data/nautilus_catalog"))
    try:
        intervals = [(int(s), int(e)) for s, e in mgr.get_intervals(identifier=bar_type)]
    except Exception:  # no data for this bar type yet
        intervals = []
    return contiguous_history(intervals, step_ns), step_ns


def window_check(req: "CreateBacktestRequest") -> str | None:
    """The 422 reason for a window the catalog cannot supply, or None (runs in a worker thread: catalog I/O)."""
    catalog = os.environ.get("NAUTILUS_CATALOG_PATH", "/app/data/nautilus_catalog")
    history, step_ns = catalog_history(req.bar_type)
    return window_problem(
        instrument_id=req.instrument_id, bar_type=req.bar_type,
        instrument_defined=instrument_defined(catalog, req.instrument_id),
        history=history, step_ns=step_ns,
        window_start_ns=req.warmup_start_ns, window_end_ns=req.evaluation_end_ns,
    )


def contiguous_history(intervals: list[tuple[int, int]], step_ns: int) -> tuple[int, int] | None:
    """(start, end) of the run of catalog intervals that ends at the newest bar, joining intervals whose
    gap is at most one bar step. File-level only: gaps inside a file are the backtest cadence check's job."""
    merged: tuple[int, int] | None = None
    for start, end in sorted(intervals, reverse=True):
        if merged is None:
            merged = (start, end)
        elif merged[0] - end <= step_ns:
            merged = (min(start, merged[0]), merged[1])
        else:
            break
    return merged


@router.get("/coverage", dependencies=AUTHED_READ)
async def catalog_coverage(bar_type: str = Query(..., min_length=1, max_length=128)) -> dict[str, Any]:
    """Contiguous Nautilus catalog history of a bar type (R5.0: the research loop refuses a spec that
    needs more history than this before it spends anything)."""
    merged, _ = catalog_history(bar_type)
    return {"bar_type": bar_type, "start_ns": merged[0] if merged else None, "end_ns": merged[1] if merged else None,
            "days": round((merged[1] - merged[0]) / 86_400e9, 2) if merged else 0.0}


@router.get("/{run_id}", dependencies=AUTHED_READ)
async def get_backtest_run(
    run_id: UUID,
    request: Request,
) -> dict[str, Any]:
    """Retrieve full details of a backtest run including tearsheet metrics and equity curve."""
    pool = request.app.state.pool

    query = """
    SELECT br.id, br.instrument_id, br.bar_type, br.timeframe, br.parameters,
           br.warmup_start_ns, br.evaluation_start_ns, br.evaluation_end_ns,
           br.execution_profile, br.run_config_hash, br.status, br.error_message,
           br.created_at, br.started_at, br.completed_at,
           s.slug AS strategy_slug, s.name AS strategy_name,
           res.total_return, res.cagr, res.sharpe_ratio, res.sortino_ratio,
           res.max_drawdown, res.profit_factor, res.win_rate, res.total_trades,
           res.winning_trades, res.losing_trades, res.avg_trade_pnl,
           res.max_consecutive_losses, res.downsampled_equity, res.monthly_returns,
           res.catalog_manifest, res.artifact_manifest
    FROM backtest_runs br
    JOIN strategy_versions sv ON sv.id = br.strategy_version_id
    JOIN strategies s ON s.id = sv.strategy_id
    LEFT JOIN backtest_results res ON res.backtest_run_id = br.id
    WHERE br.id = $1;
    """
    r = await pool.fetchrow(query, run_id)
    if not r:
        raise HTTPException(status_code=404, detail=f"Backtest run '{run_id}' not found.")

    res_data = None
    if r["total_return"] is not None:
        raw_eq = r["downsampled_equity"]
        downsampled_eq = json.loads(raw_eq) if isinstance(raw_eq, str) else raw_eq

        raw_mo = r["monthly_returns"]
        monthly_ret = json.loads(raw_mo) if isinstance(raw_mo, str) else raw_mo

        raw_cat = r["catalog_manifest"]
        cat_man = json.loads(raw_cat) if isinstance(raw_cat, str) else raw_cat

        raw_art = r["artifact_manifest"]
        art_man = json.loads(raw_art) if isinstance(raw_art, str) else raw_art

        res_data = {
            "metrics": {
                "total_return": float(r["total_return"]),
                "cagr": float(r["cagr"]) if r["cagr"] is not None else None,
                "sharpe_ratio": float(r["sharpe_ratio"]) if r["sharpe_ratio"] is not None else None,
                "sortino_ratio": float(r["sortino_ratio"]) if r["sortino_ratio"] is not None else None,
                "max_drawdown": float(r["max_drawdown"]),
                "profit_factor": float(r["profit_factor"]) if r["profit_factor"] is not None else None,
                "win_rate": float(r["win_rate"]),
                "total_trades": r["total_trades"],
                "winning_trades": r["winning_trades"],
                "losing_trades": r["losing_trades"],
                "avg_trade_pnl": float(r["avg_trade_pnl"]),
                "max_consecutive_losses": r["max_consecutive_losses"],
                **_v2_metrics(art_man.get("metrics") if isinstance(art_man, dict) else None),
            },
            "downsampled_equity": downsampled_eq,
            "monthly_returns": monthly_ret,
            "catalog_manifest": cat_man,
            "artifact_manifest": art_man,
        }

    return {
        "id": str(r["id"]),
        "strategy_slug": r["strategy_slug"],
        "strategy_name": r["strategy_name"],
        "instrument_id": r["instrument_id"],
        "bar_type": r["bar_type"],
        "timeframe": r["timeframe"],
        "parameters": json.loads(r["parameters"]) if isinstance(r["parameters"], str) else r["parameters"],
        "warmup_start_ns": r["warmup_start_ns"],
        "evaluation_start_ns": r["evaluation_start_ns"],
        "evaluation_end_ns": r["evaluation_end_ns"],
        "execution_profile": r["execution_profile"],
        "run_config_hash": r["run_config_hash"],
        "status": r["status"],
        "error_message": r["error_message"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
        "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
        "results": res_data,
    }


@router.get("/{run_id}/results", dependencies=AUTHED_READ)
async def get_backtest_results(
    run_id: UUID,
    request: Request,
) -> dict[str, Any]:
    """Retrieve full tearsheet results for a completed backtest run."""
    run = await get_backtest_run(run_id, request)
    if not run.get("results"):
        raise HTTPException(status_code=404, detail="Results not available for this run.")
    return run["results"]


@router.get("/{run_id}/trades", dependencies=AUTHED_READ)

async def get_backtest_trades(
    run_id: UUID,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=500, description="Items per page"),
) -> dict[str, Any]:
    """Retrieve paginated trade records from the immutable Parquet artifact."""
    artifacts_base = Path(os.getenv("BACKTEST_ARTIFACTS_PATH", DEFAULT_ARTIFACTS_PATH)).resolve()
    trades_path = artifacts_base / str(run_id) / "trades.parquet"

    if not trades_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Trades artifact for run '{run_id}' not found.",
        )

    con = duckdb.connect(":memory:")
    count_res = con.execute(f"SELECT count(*) FROM read_parquet('{trades_path}')").fetchone()
    total_trades = count_res[0] if count_res else 0

    offset = (page - 1) * page_size
    query = f"""
    SELECT position_id, instrument_id, side, ts_opened_ns, ts_closed_ns,
           entry_price, exit_price, quantity, realized_pnl, realized_return
    FROM read_parquet('{trades_path}')
    ORDER BY ts_opened_ns ASC
    LIMIT {page_size} OFFSET {offset};
    """
    df = con.execute(query).df()
    trades = df.to_dict(orient="records")

    return {
        "run_id": str(run_id),
        "total": total_trades,
        "page": page,
        "page_size": page_size,
        "trades": trades,
    }


@router.get("/{run_id}/manifest", dependencies=AUTHED_READ)
async def get_backtest_manifest(
    run_id: UUID,
) -> dict[str, Any]:
    """Retrieve full reproducibility manifest from disk artifact."""
    artifacts_base = Path(os.getenv("BACKTEST_ARTIFACTS_PATH", DEFAULT_ARTIFACTS_PATH)).resolve()
    manifest_path = artifacts_base / str(run_id) / "manifest.json"

    if not manifest_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Manifest artifact for run '{run_id}' not found.",
        )

    with open(manifest_path) as f:
        data = json.load(f)
    return data
