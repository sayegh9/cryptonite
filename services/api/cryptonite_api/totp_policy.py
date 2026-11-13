"""The operator's global TOTP switch (``auth.totp_enabled``, Settings -> Security).

While off, the API asks for no TOTP code: not at sign-in and not for any action (approvals, profile starts, loosened
limits, Telegram approvals). Switching it off needs one last valid code (a T/L key whose higher value is the tighter);
switching it on is free. Only a stored 0 turns it off: an unreadable, invalid or fractional value counts as on, so a
database fault never removes the second factor.
"""

import logging
from typing import Any

from cryptonite_schemas.runtime_settings import RUNTIME_BOUNDS, read_number

logger = logging.getLogger(__name__)

TOTP_SWITCH_KEY = "auth.totp_enabled"


SQL = "SELECT value FROM system_settings WHERE key = $1"


async def totp_enabled(pool: Any, conn: Any = None) -> bool:
    """Pass ``conn`` when the caller already holds a connection: the read then uses it and never waits on the pool
    for a second one (a request holding one connection while waiting for another can starve the pool)."""
    try:
        if conn is not None:
            raw = await conn.fetchval(SQL, TOTP_SWITCH_KEY)
        else:
            async with pool.acquire() as held:
                raw = await held.fetchval(SQL, TOTP_SWITCH_KEY)
    except Exception as e:
        logger.warning("TOTP switch unreadable (%s): TOTP stays required", type(e).__name__)
        return True
    value, problem = read_number(raw, RUNTIME_BOUNDS[TOTP_SWITCH_KEY], 1.0)
    if problem:
        logger.warning("TOTP switch: %s; TOTP stays required", problem)
        return True
    return value != 0
