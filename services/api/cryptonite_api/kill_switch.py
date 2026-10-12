"""The PAPER kill switch, as the API reads it.

The AI status, the paper endpoints and the control endpoints read the same row, ``execution_kill_switch`` keyed by
(environment, account_id), through this module. A missing row is reported as missing, never as armed.

Not covered yet: the executor's order fence (packages/execution kill_switch.py), the orchestrator and the debate
gate still read the row themselves, and they still treat a missing row as unlatched. Fixing those is a follow-up.
"""

from typing import Any

PAPER_ENVIRONMENT = "PAPER"
PAPER_ACCOUNT_ID = "PAPER-ACCOUNT-001"


async def read_kill_switch(conn: Any, environment: str, account_id: str) -> dict[str, Any] | None:
    """The kill-switch row for one (environment, account), or ``None`` when that row does not exist.

    ``conn`` is an asyncpg connection or pool.
    """
    row = await conn.fetchrow(
        "SELECT * FROM execution_kill_switch WHERE environment = $1 AND account_id = $2;",
        environment,
        account_id,
    )
    return dict(row) if row else None
