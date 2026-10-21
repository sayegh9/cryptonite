"""Leverage plan Stage 5: print the perp paper gate (the checklist before any Binance futures testnet step).

Read-only. Every closed AI-mandate perp position since ``--since`` (default: the first one), in R against the loss
its card planned at the stop, plus liquidations and CLOSE_SHORT alerts. See ``cryptonite_execution.perp_gate``.

    docker compose -f infra/docker-compose.yml run --rm --no-deps test uv run python scripts/perp_gate_status.py
    ... scripts/perp_gate_status.py --mandate "Perps BTC-ETH" --since 2026-10-22
"""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg
from cryptonite_execution.perp_gate import PerpTrade, perp_gate

TRADES_SQL = """
    SELECT t.position_id::text, t.instrument_id, t.opened_at, t.closed_at, t.realized_pnl, t.entry_setup_grade,
           t.close_reason, c.payload->'leverage_plan'->>'loss_at_stop_usd' AS planned_loss
    FROM agent_safe_trade_timeline t
    LEFT JOIN execution_commands c ON c.command_id = t.entry_command_id
    WHERE t.status = 'CLOSED' AND t.instrument_id LIKE '%-PERP.%'
      AND ($1::text IS NULL OR t.mandate_name = $1) AND ($2::timestamptz IS NULL OR t.opened_at >= $2)
    ORDER BY t.closed_at
"""
LIQUIDATIONS_SQL = """
    SELECT count(*) FROM paper_orders
    WHERE client_order_id LIKE 'LIQ-%' AND instrument_id LIKE '%-PERP.%' AND instance_id LIKE 'AI!_MANDATE!_%' ESCAPE '!'
      AND ($1::timestamptz IS NULL OR created_at >= $1)
"""
SHORT_CLOSES_SQL = """
    SELECT dedupe_key FROM notifications_outbox
    WHERE kind = 'CLOSE_SHORT' AND payload->>'instrument_id' LIKE '%-PERP.%'
      AND ($1::timestamptz IS NULL OR created_at >= $1)
"""
SPOT_SQL = """
    SELECT avg(return_pct) FROM agent_safe_trade_timeline
    WHERE status = 'CLOSED' AND instrument_id NOT LIKE '%-PERP.%' AND closed_at >= $1
"""


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mandate", help="only this mandate (its name)")
    ap.add_argument("--since", type=datetime.fromisoformat, help="only positions opened from this date (UTC)")
    args = ap.parse_args()
    since = args.since.replace(tzinfo=UTC) if args.since and args.since.tzinfo is None else args.since

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch(TRADES_SQL, args.mandate, since)
        trades = [PerpTrade(r["position_id"], r["instrument_id"], r["opened_at"], r["closed_at"],
                            Decimal(str(r["realized_pnl"] or 0)),
                            Decimal(r["planned_loss"]) if r["planned_loss"] else None,
                            r["entry_setup_grade"], r["close_reason"]) for r in rows]
        window = since or min((t.opened_at for t in trades), default=None)
        liquidations = await conn.fetchval(LIQUIDATIONS_SQL, window)
        short_closes = [r["dedupe_key"] for r in await conn.fetch(SHORT_CLOSES_SQL, window)]
        spot = await conn.fetchval(SPOT_SQL, window) if window else None
    finally:
        await conn.close()

    report = perp_gate(trades, liquidations=int(liquidations or 0), short_closes=short_closes,
                       now=datetime.now(UTC), spot_mean_return_pct=float(spot) if spot is not None else None)
    print(json.dumps({
        "gate": "leverage plan Stage 5 -> Binance futures testnet", "passed": report.passed,
        "mandate": args.mandate, "since": window,
        "checks": [{"check": c.name, "need": c.required, "have": c.actual, "ok": c.passed,
                    **({"informational": True} if c.informational else {}),
                    **({"interval": list(c.interval)} if c.interval else {})} for c in report.checks],
        "stop_failures": list(report.stop_failures),
        "trades": [{"position": t.position_id, "instrument": t.instrument_id, "closed_at": t.closed_at,
                    "pnl": str(t.realized_pnl), "r": None if t.r is None else f"{t.r:.2f}", "grade": t.grade,
                    "close_reason": t.close_reason} for t in trades],
    }, indent=1, default=str))


if __name__ == "__main__":
    asyncio.run(main())
