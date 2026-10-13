"""The operator session and login policy from system_settings (ADR 0007, Phase 2).

Read every minute and applied to the auth manager, so an edit in Settings takes effect without a restart. The rule is
the one the other runtime settings follow: a missing value takes the default, a valid stored value applies, and an
invalid one (or a failed read) keeps the last valid value.
"""

import asyncio
import logging
from typing import Any

from cryptonite_schemas.runtime_settings import RUNTIME_BOUNDS, read_number

logger = logging.getLogger(__name__)

AUTH_KEYS = ("auth.session_ttl_s", "auth.idle_timeout_s", "auth.lockout_s", "auth.max_login_attempts")
REFRESH_S = 60
# The manager's field for each key (the names apply_policy takes)
_FIELD = {
    "auth.session_ttl_s": "session_ttl_s",
    "auth.idle_timeout_s": "idle_timeout_s",
    "auth.lockout_s": "lockout_s",
    "auth.max_login_attempts": "max_login_attempts",
}


async def refresh_auth_policy(pool: Any, manager: Any, last_good: dict[str, float]) -> dict[str, float]:
    """Read the stored policy, apply the valid values to ``manager`` and return what is in force."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM system_settings WHERE key = ANY($1)", list(AUTH_KEYS))
    except Exception as e:
        logger.warning("Operator session policy not re-read (%s); keeping the last valid values", e)
        return last_good
    stored = {r["key"]: r["value"] for r in rows}
    applied: dict[str, float] = {}
    for key in AUTH_KEYS:
        value, problem = read_number(stored.get(key), RUNTIME_BOUNDS[key], last_good.get(key, RUNTIME_BOUNDS[key].default))
        if problem:
            logger.error("Operator session policy refused, keeping the last valid value: %s: %s", key, problem)
        applied[key] = value
    manager.apply_policy(**{_FIELD[key]: applied[key] for key in AUTH_KEYS})
    return applied


async def run_auth_policy(state: Any) -> None:
    """The refresh loop the API starts at boot (``state`` is ``app.state``): the first pass applies the stored policy."""
    last = {key: RUNTIME_BOUNDS[key].default for key in AUTH_KEYS}
    while True:
        last = await refresh_auth_policy(state.pool, state.auth_manager, last)
        await asyncio.sleep(REFRESH_S)
