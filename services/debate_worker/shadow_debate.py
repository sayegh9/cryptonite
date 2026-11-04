"""H13d: a reworked lite-debate prompt answers the same setups as the live one, in shadow only (docs/adr/0012).

Runs as a detached task started after the live debate has committed, recorded policy B and published its proposal alert.
It gets the lite call's own prompt and nothing the debate decided. Its only write is ``shadow_debate_verdicts``, which
nothing live reads; its call goes straight through the gateway, not the worker's ``run_agent`` (turns, publishes,
heartbeats), and carries no mandate or parent trace, so the profile's budget share and the decision trace tree never
count it. Whatever fails here is logged and ends here. Promoting the prompt is a later operator decision.
"""

import asyncio
import logging
import os
from decimal import Decimal

from cryptonite_agents.debate_fleet import DEBATE_LITE_SHADOW
from cryptonite_agents.gateway import BudgetExceeded, CallContext
from shadow_eval import PROMPT_VERSION

logger = logging.getLogger("debate_worker.shadow_debate")

SHADOW_TIMEOUT_S = 45
SHADOW_DAILY_USD_CAP = Decimal("1.00")
_REASONING_MAX = 2000
_TASKS: set[asyncio.Task] = set()  # strong references: a fire-and-forget task must not be collected mid-call

_SPENT_TODAY = (
    "SELECT COALESCE(SUM(cost_usd), 0) FROM shadow_debate_verdicts "
    "WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
)
_INSERT = (
    "INSERT INTO shadow_debate_verdicts (candidate_id, run_id, prompt_version, state, recommendation, conviction, "
    "reasoning, model, cost_usd) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) ON CONFLICT (candidate_id) DO NOTHING"
)


def enabled() -> bool:
    return os.getenv("DEBATE_SHADOW_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def spawn(pg_pool, gateway, candidate_id, run_id, prompt: str) -> asyncio.Task | None:
    """Start the shadow call as a fire-and-forget task; the live path never awaits it. Never raises."""
    try:
        if candidate_id is None or not enabled():
            return None
        task = asyncio.get_running_loop().create_task(run_shadow(pg_pool, gateway, candidate_id, run_id, prompt))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return task
    except Exception as e:
        logger.warning("shadow debate for candidate %s not started: %s", candidate_id, e)
        return None


async def _write(pg_pool, candidate_id, run_id, state: str, *, recommendation=None, conviction=None, reasoning=None,
                 model=None, cost_usd=None) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(_INSERT, candidate_id, run_id, PROMPT_VERSION, state, recommendation, conviction,
                           reasoning[:_REASONING_MAX] if reasoning else None, model, cost_usd)


async def _call(pg_pool, gateway, candidate_id, run_id, prompt: str) -> None:
    async with pg_pool.acquire() as conn:
        spent = Decimal(str(await conn.fetchval(_SPENT_TODAY)))
    if spent >= SHADOW_DAILY_USD_CAP:
        await _write(pg_pool, candidate_id, run_id, "SKIPPED_BUDGET", reasoning=f"spent ${spent} of ${SHADOW_DAILY_USD_CAP} today")
        return
    try:
        result = await asyncio.wait_for(
            DEBATE_LITE_SHADOW.run(gateway, prompt, context=CallContext(role=DEBATE_LITE_SHADOW.role)),
            timeout=SHADOW_TIMEOUT_S)
    except TimeoutError:
        await _write(pg_pool, candidate_id, run_id, "TIMEOUT", reasoning=f"no answer within {SHADOW_TIMEOUT_S} s")
        return
    except BudgetExceeded as e:  # the gateway's global budget, or this role's own cap
        await _write(pg_pool, candidate_id, run_id, "SKIPPED_BUDGET", reasoning=str(e))
        return
    except Exception as e:
        logger.warning("shadow debate call for candidate %s failed: %s", candidate_id, e)
        await _write(pg_pool, candidate_id, run_id, "ERROR", reasoning=f"{type(e).__name__}: {e}")
        return
    output = result.output
    await _write(pg_pool, candidate_id, run_id, "OK", recommendation=output.recommendation,
                 conviction=output.conviction_score, reasoning=output.reasoning, model=result.model,
                 cost_usd=result.cost_usd)


async def run_shadow(pg_pool, gateway, candidate_id, run_id, prompt: str) -> None:
    """The whole shadow step, catching everything (a cancel at shutdown too): nothing leaves this task."""
    try:
        await _call(pg_pool, gateway, candidate_id, run_id, prompt)
    except BaseException as e:
        logger.warning("shadow debate for candidate %s ended without a row: %s: %s", candidate_id, type(e).__name__, e)
