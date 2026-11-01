"""Phase 2 shadow capture: three entry policies recorded on the same fresh scanner candidates (paper only).

- Policy A (deterministic): the gate's own inputs. Hard triage rules empty and setup grade A/B take at full
  size, grade C takes at half size, NO_TRADE (or no grade) skips.
- Policy B (the live pipeline): what the live triage, debate and route actually ended with.
- Policy C (narrow veto): one small Flash call that takes the candidate's direction unless it names a red flag.
  Shadow only, never routes, never awaited by the live path, capped per UTC day (``POLICY_C_DAILY_CAP``).

Nothing here may change what the live pipeline trades or decides: every write is best-effort (logged, never
raised) and no return value of the live path depends on it. Rows go to ``policy_decisions`` (migration 057),
one per (candidate, policy), first write wins.
"""

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from cryptonite_agents.debate_fleet import NARROW_VETO
from cryptonite_agents.gateway import CallContext
from cryptonite_scanner.brief import candidate_direction

logger = logging.getLogger("debate_worker.policies")

TAKE, SKIP, ERROR = "TAKE", "SKIP", "ERROR"
DEFAULT_POLICY_C_DAILY_CAP = 60
POLICY_C_TIMEOUT_S = 45.0
_REASON_MAX = 500
_ROUTED_STATUSES = frozenset({"COMPLETED", "PENDING_APPROVAL"})  # an entry handed to execution or to approval
_SIDE_OF = {"BUY": "LONG", "SELL": "SHORT"}
# A 'policy.veto' call whose answer never came back: the placeholder row stays ERROR (never a silent TAKE)
_PENDING = "PENDING"
_C_TASKS: set[asyncio.Task] = set()  # strong references: a fire-and-forget task must not be collected mid-call

_INSERT = (
    "INSERT INTO policy_decisions (candidate_id, policy, decision, side, size_factor, reason, model, cost_usd) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (candidate_id, policy) DO NOTHING RETURNING 1"
)
# B and C describe candidates that policy A saw: without an A row they are not part of the comparison
_INSERT_AFTER_A = (
    "INSERT INTO policy_decisions (candidate_id, policy, decision, side, size_factor, reason, model, cost_usd) "
    "SELECT $1::uuid, $2::char(1), $3::text, $4::text, $5::numeric, $6::text, $7::text, $8::numeric "
    "WHERE EXISTS (SELECT 1 FROM policy_decisions WHERE candidate_id = $1::uuid AND policy = 'A') "
    "ON CONFLICT (candidate_id, policy) DO NOTHING RETURNING 1"
)
_C_TODAY = (
    "SELECT count(*) FROM policy_decisions WHERE policy = 'C' "
    "AND decided_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
)
_C_LOCK = "SELECT pg_advisory_xact_lock(hashtext('policy_decisions:C:daily_cap'))"
_C_UPDATE = (
    "UPDATE policy_decisions SET decision = $3, side = $4, size_factor = $5, reason = $6, model = $7, cost_usd = $8 "
    "WHERE candidate_id = $1 AND policy = $2"
)


@dataclass(frozen=True)
class PolicyDecision:
    decision: str  # TAKE / SKIP / ERROR
    side: str | None = None  # LONG / SHORT
    size_factor: float | None = None
    reason: str | None = None

    def as_args(self) -> tuple:
        return self.decision, self.side, self.size_factor, self.reason


def policy_c_daily_cap() -> int:
    try:
        return max(0, int(os.getenv("POLICY_C_DAILY_CAP", str(DEFAULT_POLICY_C_DAILY_CAP))))
    except ValueError:
        return DEFAULT_POLICY_C_DAILY_CAP


def policy_a(candidate: dict[str, Any] | None, brief: dict[str, Any] | None, grade: str | None) -> PolicyDecision:
    """Deterministic policy A on the gate's inputs: the candidate payload, ``brief_data`` and its setup grade.

    The hard triage rules are the gate's own (``gate._rule_rejection``, SHORT-aware); any hit skips. Then the
    grade decides: A or B take at full size, C at half size, NO_TRADE or an unknown grade skip."""
    from gate import _rule_rejection  # lazy: gate imports this module

    side = candidate_direction(candidate if isinstance(candidate, dict) else None)
    if side is None:
        return PolicyDecision(SKIP, None, None, "NO_DIRECTION")
    if (rule := _rule_rejection(brief)) is not None:
        return PolicyDecision(SKIP, side, None, rule)
    if grade in ("A", "B"):
        return PolicyDecision(TAKE, side, 1.0, f"GRADE_{grade}")
    if grade == "C":
        return PolicyDecision(TAKE, side, 0.5, "GRADE_C")
    return PolicyDecision(SKIP, side, None, "GRADE_NO_TRADE" if grade == "NO_TRADE" else "NO_GRADE")


def policy_b_for_refusal(reason: str) -> tuple:
    """A live refusal after policy A (hard rule, triage reject, caps, budget, stale final quote) is a SKIP."""
    return PolicyDecision(SKIP, None, None, reason).as_args()


def policy_b_for_route(final_status: str, recommendation: str | None) -> tuple:
    """The debate verdict and the route: an entry routed to approval or execution is a TAKE, anything else
    (a HOLD verdict, a route refusal) a SKIP."""
    side = _SIDE_OF.get(str(recommendation or "").upper())
    if side is not None and final_status in _ROUTED_STATUSES:
        return PolicyDecision(TAKE, side, 1.0, f"ROUTED_{final_status}").as_args()
    return PolicyDecision(SKIP, side, None, "DEBATE_HOLD" if side is None else f"ROUTE_{final_status}").as_args()


def policy_b_for_failure(error: BaseException) -> tuple:
    """A debate that failed (model, budget, storage) decided nothing: ERROR, kept apart from SKIP."""
    return PolicyDecision(ERROR, None, None, f"DEBATE_FAILED:{type(error).__name__}").as_args()


async def record_policy(conn, candidate_id, policy: str, decision: str, side: str | None = None,
                        size_factor: float | None = None, reason: str | None = None, *, model: str | None = None,
                        cost_usd: Any = None, require_a: bool = False) -> bool:
    """Write one ``policy_decisions`` row (first write wins). True when this call inserted it; never raises.

    Runs in a savepoint when ``conn`` has a transaction, so a failure never aborts the caller's transaction."""
    if candidate_id is None:
        return False
    try:
        args = (
            _INSERT_AFTER_A if require_a else _INSERT,
            uuid.UUID(str(candidate_id)), policy, decision, side,
            Decimal(str(size_factor)) if size_factor is not None else None,
            reason[:_REASON_MAX] if reason else reason, model,
            Decimal(str(cost_usd)) if cost_usd is not None else None,
        )
        transaction = getattr(conn, "transaction", None)
        if transaction is None:
            return await conn.fetchval(*args) is not None
        async with transaction():
            return await conn.fetchval(*args) is not None
    except Exception as e:
        logger.warning("policy %s for candidate %s not recorded: %s", policy, candidate_id, e)
        return False


async def record_policy_b(pg_pool, candidate_id, decision: str, side: str | None = None,
                          size_factor: float | None = None, reason: str | None = None) -> bool:
    """Policy B on its own connection, only for a candidate policy A saw. Never raises."""
    if candidate_id is None:
        return False
    try:
        async with pg_pool.acquire() as conn:
            return await record_policy(conn, candidate_id, "B", decision, side, size_factor, reason, require_a=True)
    except Exception as e:
        logger.warning("policy B for candidate %s not recorded: %s", candidate_id, e)
        return False


async def capture_entry_policies(pg_pool, gateway, candidate_id, instrument_id: str, candidate: dict[str, Any],
                                 brief: str | None, brief_data: dict[str, Any] | None, *, profile_id=None) -> None:
    """Record policy A for a candidate that passed the deterministic gate, then start policy C in the
    background (only when this call recorded A, so a redelivery never pays for a second veto). Never raises."""
    try:
        grade = ((brief_data or {}).get("setup_grade") or {}).get("grade") if isinstance(brief_data, dict) else None
        a = policy_a(candidate, brief_data, grade)
        async with pg_pool.acquire() as conn:
            recorded = await record_policy(conn, candidate_id, "A", *a.as_args())
        if recorded and gateway is not None:
            spawn_policy_c(pg_pool, gateway, candidate_id, instrument_id,
                           candidate_direction(candidate if isinstance(candidate, dict) else None), brief)
    except Exception as e:
        logger.warning("entry policies for candidate %s not captured: %s", candidate_id, e)


def spawn_policy_c(pg_pool, gateway, candidate_id, instrument_id: str, side: str | None,
                   brief: str | None) -> asyncio.Task | None:
    """Start policy C as a fire-and-forget task; the live path never awaits it."""
    try:
        task = asyncio.get_running_loop().create_task(
            run_policy_c(pg_pool, gateway, candidate_id, instrument_id, side, brief))
    except Exception as e:
        logger.warning("policy C for candidate %s not started: %s", candidate_id, e)
        return None
    _C_TASKS.add(task)
    task.add_done_callback(_C_TASKS.discard)
    return task


async def _reserve_policy_c(pg_pool, candidate_id, side: str | None) -> bool:
    """Count today's C rows under a lock and, below the cap, insert this candidate's placeholder row
    (ERROR / PENDING, updated once the answer is in). False at the cap or when the row exists."""
    cap = policy_c_daily_cap()
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_C_LOCK)
            if int(await conn.fetchval(_C_TODAY) or 0) >= cap:
                logger.info("policy C daily cap (%d) reached: candidate %s not vetoed", cap, candidate_id)
                return False
            return await record_policy(conn, candidate_id, "C", ERROR, side, None, _PENDING, require_a=True)


async def _finish_policy_c(pg_pool, candidate_id, decision: PolicyDecision, model=None, cost_usd=None) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(_C_UPDATE, uuid.UUID(str(candidate_id)), "C", decision.decision, decision.side,
                           Decimal(str(decision.size_factor)) if decision.size_factor is not None else None,
                           (decision.reason or "")[:_REASON_MAX], model,
                           Decimal(str(cost_usd)) if cost_usd is not None else None)


def interpret_veto(output: Any, side: str) -> PolicyDecision:
    """A named red flag skips; anything else takes the candidate's direction at full size."""
    flag = str(getattr(output, "red_flag", "NONE") or "NONE")
    reason = str(getattr(output, "reason", "") or "")
    if flag != "NONE":
        return PolicyDecision(SKIP, side, None, f"{flag}: {reason}")
    return PolicyDecision(TAKE, side, 1.0, reason or "NO_RED_FLAG")


async def run_policy_c(pg_pool, gateway, candidate_id, instrument_id: str, side: str | None,
                       brief: str | None) -> None:
    """One ``policy.veto`` call for the candidate; a timeout or any failure records ERROR. Never raises."""
    try:
        if side is None:
            return  # nothing to veto: policy A already recorded NO_DIRECTION
        if not await _reserve_policy_c(pg_pool, candidate_id, side):
            return
        try:
            prompt = (f"Candidate: {side} {instrument_id}. Decide TAKE unless you find a named red flag.\n\n"
                      f"{brief or '(no market brief)'}")
            # No mandate in the context: this shadow call must never draw on a profile's AI budget
            result = await asyncio.wait_for(
                NARROW_VETO.run(gateway, prompt, context=CallContext(role=NARROW_VETO.role)), POLICY_C_TIMEOUT_S)
            await _finish_policy_c(pg_pool, candidate_id, interpret_veto(result.output, side),
                                   getattr(result, "model", None), getattr(result, "cost_usd", None))
        except Exception as e:  # TimeoutError included
            await _finish_policy_c(pg_pool, candidate_id,
                                   PolicyDecision(ERROR, side, None, f"{type(e).__name__}: {e}"[:200]))
    except Exception as e:
        logger.warning("policy C for candidate %s failed: %s", candidate_id, e)
