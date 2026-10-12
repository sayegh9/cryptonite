"""The PAPER kill switch, read from one place.

The AI status, the paper endpoint and the control endpoint all read the same row: ``execution_kill_switch``, keyed
by (environment, account_id). This module holds that key and the read, so the three cannot disagree about which
row they mean. A missing row is reported as missing, never as armed: a kill switch fails closed.
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
