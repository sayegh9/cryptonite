"""A7.3: print each active mandate's autonomy gate checklist (what the orchestrator evaluates every 10 min).

The L2 candidacy run is a mandate's time at L1: the system proposes the promotion when every check
passes, and only the operator confirms it (TOTP). This prints where each mandate stands.

    docker compose -f infra/docker-compose.yml run --rm --no-deps test uv run python scripts/gate_status.py
"""

import asyncio
import json
import os

import asyncpg
from cryptonite_execution.autonomy_metrics import gate_report
from cryptonite_execution.mandate_store import load_mandate


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch("SELECT id FROM ai_mandates WHERE status IN ('ACTIVE', 'HALTED') ORDER BY created_at")
        for row in rows:
            m = await load_mandate(conn, row["id"])
            report, metrics = await gate_report(conn, m)
            print(json.dumps({
                "mandate": m.name, "level": int(m.autonomy_level), "status": str(m.status),
                "gate_for": f"L{int(m.autonomy_level) + 1}", "passed": report.passed,
                # informational checks (R4.2/R4.3) are shown with their 90% interval; they never decide "passed"
                "checks": [{"check": c.name, "need": c.required, "have": c.actual, "ok": c.passed,
                            **({"informational": True} if c.informational else {}),
                            **({"interval": list(c.interval)} if c.interval else {})} for c in report.checks],
            }, indent=1, default=str))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
