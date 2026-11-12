"""The operator's global TOTP switch (``auth.totp_enabled``, Settings -> Security).

While off, the API asks for no TOTP code: not at sign-in and not for any action (approvals, profile starts, loosened
limits, Telegram approvals). Switching it off needs one last valid code (a T/L key whose higher value is the tighter);
switching it on is free. An unreadable value counts as on: a database fault never removes the second factor.
"""

import logging
from typing import Any

from cryptonite_schemas.runtime_settings import RUNTIME_BOUNDS, read_number

logger = logging.getLogger(__name__)

TOTP_SWITCH_KEY = "auth.totp_enabled"


async def totp_enabled(pool: Any) -> bool:
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval("SELECT value FROM system_settings WHERE key = $1", TOTP_SWITCH_KEY)
    except Exception as e:
        logger.warning("TOTP switch unreadable (%s): TOTP stays required", type(e).__name__)
        return True
    value, problem = read_number(raw, RUNTIME_BOUNDS[TOTP_SWITCH_KEY], 1.0)
    if problem:
        logger.warning("TOTP switch: %s; TOTP stays required", problem)
        return True
    return value >= 1
