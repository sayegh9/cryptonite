"""Live-vs-backtest report (remediation Phase 8, F3). Reporting only: reads paper results and backtest
results, changes nothing.

For each running (or paused) paper strategy instance whose strategy version has a COMPLETED backtest,
live results since the instance was deployed against the latest such backtest (same instrument
preferred): trades, win rate, mean return per trade, fees, and the slippage of fills against their
limit price where the order had one. Live trades are the instance's closed ``paper_positions``; a
trade's return is its realized PnL over its entry notional (entry price x bought quantity from
``paper_trades``). Backtest values come from ``backtest_results`` and, for mean return and slippage,
from the run's trades/orders Parquet artifacts when they are readable (otherwise None).

``compare_live_vs_backtest`` in ``cryptonite_agents.tools`` was not reused: it returns free text for a
model to interpret (last 50 fills of an instrument), not per-instance numbers.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

logger = logging.getLogger("cryptonite.api.live_vs_backtest")

ACTIVE_STATUSES = ("RUNNING", "PAUSED")


def _f(v: Any) -> float | None:
    return float(v) if v is not None else None


def adverse_slippage_bps(side: str, limit_price: float, fill_price: float) -> float | None:
    """How much worse than its limit price an order filled, in basis points (positive = worse:
    a BUY above its limit, a SELL below it). None without a usable limit price."""
    if not limit_price or limit_price <= 0 or not fill_price or fill_price <= 0:
        return None
    diff = (fill_price - limit_price) / limit_price * 10_000
    return diff if side.upper() == "BUY" else -diff


def summarize_trades(returns: list[float], pnls: list[float]) -> dict[str, Any]:
    """trades, win rate (PnL > 0) and mean return per trade; None where there is nothing to average."""
    n = len(pnls)
    return {
        "trades": n,
        "win_rate": round(sum(1 for p in pnls if p > 0) / n, 6) if n else None,
        "mean_return_per_trade": round(sum(returns) / len(returns), 8) if returns else None,
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


async def _live(conn, instance_id: str, since: datetime) -> dict[str, Any]:
    positions = await conn.fetch(
        """SELECT p.realized_pnl, p.entry_price,
                  (SELECT sum(t.quantity) FROM paper_trades t
                   WHERE t.instance_id = p.instance_id AND t.instrument_id = p.instrument_id AND t.side = 'BUY'
                     AND t.executed_at BETWEEN p.opened_at AND p.closed_at) AS bought_qty
           FROM paper_positions p
           WHERE p.instance_id = $1 AND p.status = 'CLOSED' AND p.opened_at >= $2
           ORDER BY p.closed_at""", instance_id, since)
    pnls = [float(p["realized_pnl"]) for p in positions]
    returns = []
    for p in positions:
        notional = float(p["entry_price"] or 0) * float(p["bought_qty"] or 0)
        if notional > 0:
            returns.append(float(p["realized_pnl"]) / notional)
    fees = await conn.fetchval(
        "SELECT COALESCE(sum(commission), 0) FROM paper_trades WHERE instance_id = $1 AND executed_at >= $2",
        instance_id, since)
    orders = await conn.fetch(
        """SELECT side, price, avg_fill_price FROM paper_orders
           WHERE instance_id = $1 AND created_at >= $2 AND filled_qty > 0
             AND price IS NOT NULL AND avg_fill_price IS NOT NULL""", instance_id, since)
    slips = [s for o in orders if (s := adverse_slippage_bps(o["side"], float(o["price"]), float(o["avg_fill_price"]))) is not None]
    return {**summarize_trades(returns, pnls), "fees": round(float(fees), 8),
            "slippage_bps": _mean(slips), "slippage_orders": len(slips)}


def _artifacts(artifacts_base: Path, run_id: str) -> tuple[float | None, float | None, int]:
    """(mean realized return per trade, mean adverse slippage bps vs limit price, orders measured) from
    the run's Parquet artifacts; Nones when they are missing or unreadable."""
    mean_return = slippage = None
    measured = 0
    run_dir = (artifacts_base / run_id).resolve()
    if run_dir.parent != artifacts_base:
        return None, None, 0
    con = duckdb.connect(":memory:")
    try:
        trades = run_dir / "trades.parquet"
        if trades.is_file():
            row = con.execute("SELECT avg(realized_return) FROM read_parquet(?)", [str(trades)]).fetchone()
            mean_return = round(float(row[0]), 8) if row and row[0] is not None else None
        orders = run_dir / "orders.parquet"
        if orders.is_file():
            rows = con.execute("SELECT side, price, avg_px FROM read_parquet(?) WHERE filled_qty > 0",
                               [str(orders)]).fetchall()
            slips = [s for side, px, fill in rows if (s := adverse_slippage_bps(str(side), float(px or 0), float(fill or 0))) is not None]
            slippage, measured = _mean(slips), len(slips)
    except Exception as e:  # reporting only
        logger.warning("Backtest artifacts of %s unreadable: %s", run_id, e)
    finally:
        con.close()
    return mean_return, slippage, measured


async def _latest_backtest(conn, strategy_version_id: Any, instrument_id: str):
    return await conn.fetchrow(
        """SELECT br.id, br.instrument_id, br.timeframe, br.execution_profile, br.completed_at,
                  res.total_trades, res.win_rate, res.artifact_manifest->'metrics' AS v2
           FROM backtest_runs br JOIN backtest_results res ON res.backtest_run_id = br.id
           WHERE br.strategy_version_id = $1 AND br.status = 'COMPLETED'
           ORDER BY (br.instrument_id = $2) DESC, br.completed_at DESC NULLS LAST, br.created_at DESC
           LIMIT 1""", strategy_version_id, instrument_id)


def _delta(a: float | None, b: float | None) -> float | None:
    return round(a - b, 8) if a is not None and b is not None else None


async def live_vs_backtest_report(pool, artifacts_base: Path) -> dict[str, Any]:
    """``{"instances": [...], "without_backtest": [...]}`` for every RUNNING / PAUSED instance."""
    artifacts_base = artifacts_base.resolve()
    out: list[dict[str, Any]] = []
    without: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        instances = await conn.fetch(
            """SELECT instance_id, strategy_name, strategy_version_id, instrument_id, timeframe, status, created_at
               FROM paper_strategy_instances WHERE status = ANY($1::text[]) ORDER BY created_at""",
            list(ACTIVE_STATUSES))
        for inst in instances:
            base = {"instance_id": inst["instance_id"], "strategy_name": inst["strategy_name"],
                    "strategy_version_id": str(inst["strategy_version_id"]), "instrument_id": inst["instrument_id"],
                    "timeframe": inst["timeframe"], "status": inst["status"],
                    "deployed_at": inst["created_at"].isoformat()}
            bt = await _latest_backtest(conn, inst["strategy_version_id"], inst["instrument_id"])
            if bt is None:
                without.append(base)
                continue
            live = await _live(conn, inst["instance_id"], inst["created_at"])
            v2 = bt["v2"]
            v2 = json.loads(v2) if isinstance(v2, str) else (v2 or {})
            mean_return, slippage, measured = _artifacts(artifacts_base, str(bt["id"]))
            backtest = {"run_id": str(bt["id"]), "instrument_id": bt["instrument_id"], "timeframe": bt["timeframe"],
                        "execution_profile": bt["execution_profile"],
                        "completed_at": bt["completed_at"].isoformat() if bt["completed_at"] else None,
                        "trades": bt["total_trades"], "win_rate": _f(bt["win_rate"]),
                        "mean_return_per_trade": mean_return,
                        "fees": _f(v2.get("fees_paid")) if isinstance(v2, dict) else None,
                        "slippage_bps": slippage, "slippage_orders": measured}
            out.append({**base, "live": live, "backtest": backtest,
                        "delta": {"win_rate": _delta(live["win_rate"], backtest["win_rate"]),
                                  "mean_return_per_trade": _delta(live["mean_return_per_trade"],
                                                                  backtest["mean_return_per_trade"])}})
    return {"instances": out, "without_backtest": without}

