"""ADR 0011: account safety, profile style, debate slots and proportional AI budget."""

import asyncio
import datetime
import json
import logging
import math
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from cryptonite_agents.debate_fleet import TRIAGE
from cryptonite_agents.gateway import CallContext, Gateway
from cryptonite_agents.gateway.budget import DEFAULT_DAILY_CAP_USD, BudgetExceeded, parse_cap
from cryptonite_execution.profile_log import record_decision
from cryptonite_scanner.brief import hard_rejects
from cryptonite_schemas.debate import TriageDecision
from cryptonite_schemas.profile import STYLE_TIMEFRAMES
from mandate_routing import resolve_profile

logger = logging.getLogger("debate_worker.gate")

PAPER_ACCOUNT_ID = "PAPER-ACCOUNT-001"
AUTOMATIC_SOURCES = frozenset({"scanner"})
BRAIN_SOURCES = frozenset({"brain_on_demand"})
MAX_POSITION_PCT = 25.0
MIN_ENTRY_PCT = 1.0  # the dispatcher's floor: less than this headroom means no entry can be placed
COOLDOWN_S = 30 * 60
MIN_STALE_S = 15 * 60
# D1: the scanner's debate outbox can publish a candidate twice (a crash between XADD and marking its row
# published). Dedupe is the durable run row (debate_runs.candidate_id, partial unique index in migration
# 039), not a key set before any work: a failure before the run leaves nothing behind, so a PEL retry
# (F-H6) of the same message passes the gate again, while a delivery whose run exists is dropped.
TIMEFRAME_S = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
PROFILE_CAP_TTL_S = 2 * 86400
PROFILE_BUDGET_LOCK_TTL_S = 300
PROFILE_BUDGET_LOCK_RENEW_S = 20


def _utc_now_s() -> float:
    """Current UTC epoch seconds for day-scoped budget and slot accounting."""
    return datetime.datetime.now(datetime.UTC).timestamp()


class ProfileBudgetLockError(RuntimeError):
    """The profile budget lease is unavailable or lost; fail closed without claiming budget spend."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class GateDecision:
    allowed: bool
    reason: str
    triage: TriageDecision | None = None
    triage_trace_id: Any = None
    cooldown_key: str | None = None  # released by the caller if the debate it allowed fails
    cooldown_token: str | None = None  # the caller's own token: release only while it still holds the key
    profile: Any = None


# Atomic compare-and-delete: read and delete in one script, so between the two no TTL expiry or other
# candidate's SET can slip in and leave the stale owner deleting a window it no longer holds.
_RELEASE_COOLDOWN_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


async def release_cooldown(redis, key: str | None, token: str | None) -> None:
    """Compare-and-delete the cooldown: only the attempt whose token still holds it may release it, so a
    failing attempt never deletes a window that has since become another candidate's."""
    if not key or token is None:
        return
    try:
        await redis.eval(_RELEASE_COOLDOWN_LUA, 1, key, token)
    except Exception:  # a failed release only costs the cooldown window, never the retry itself
        logger.warning("Cooldown %s not released", key)


async def _count(redis, reason: str) -> None:
    try:
        key = f"debate_gate:{datetime.datetime.now(datetime.UTC):%Y-%m-%d}:{reason}"
        await redis.incr(key)
        await redis.expire(key, 8 * 86400)
    except Exception:
        pass


def decision_trace_id(payload: dict[str, Any]) -> str:
    """R7.4: the scanner's id for this setup (candidate payload), the trigger's own, or a new one."""
    try:
        candidate = json.loads(payload.get("candidate_payload") or "{}")
    except (TypeError, ValueError):
        candidate = {}
    trace = (candidate.get("decision_trace_id") if isinstance(candidate, dict) else None) or payload.get("decision_trace_id")
    try:
        return str(uuid.UUID(str(trace)))
    except (TypeError, ValueError):
        return str(uuid.uuid4())


def _trace_uuid(payload: dict[str, Any]) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(payload["decision_trace_id"])) if payload.get("decision_trace_id") else None
    except (TypeError, ValueError):
        return None


async def pre_debate_gate(pg_pool, redis, payload: dict[str, Any], gateway: Gateway | None,
                          brief: str | None, now_s: float | None = None,
                          brief_data: dict[str, Any] | None = None,
                          run_id: uuid.UUID | None = None, *, defer_record: bool = False) -> GateDecision:
    """Evaluate ADR 0011 gates; workers defer auditing until their unique run insert commits."""
    async with pg_pool.acquire() as conn:
        profile = await resolve_profile(conn, payload["instrument_id"], payload.get("mandate_id"))
    if profile is not None:
        payload["mandate_id"] = str(profile.id)
    decision = await _evaluate(pg_pool, redis, payload, gateway, brief, now_s, brief_data, profile)
    decision.profile = profile
    if profile is not None and not defer_record:
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await record_decision(conn, profile.id, "GATE", "ACCEPTED" if decision.allowed else "REFUSED",
                                      decision.reason, instrument_id=payload["instrument_id"], run_id=run_id)

    await _count(redis, decision.reason)
    trace = payload.get("decision_trace_id")
    if not decision.allowed:
        logger.info("Debate skipped for %s: %s (decision_trace_id=%s)", payload.get("instrument_id"), decision.reason, trace)
    else:
        logger.info("Debate allowed for %s: %s (decision_trace_id=%s)", payload.get("instrument_id"), decision.reason, trace)
    return decision


def _rule_rejection(brief_data: dict[str, Any] | None) -> str | None:
    """The first hard triage rule the structured brief hits, or None (also for malformed data)."""
    try:
        data = brief_data
        candidate = data.get("candidate") if isinstance(data, dict) else None
        if isinstance(candidate, dict):
            levels = [candidate.get(k) for k in ("entry", "stop", "target")]
            if all(v is not None for v in levels):
                try:
                    entry, stop, target = map(float, levels)
                except (TypeError, ValueError, OverflowError):
                    entry = stop = target = 0.0  # malformed structure cannot skip the existing LONG rules
                if all(math.isfinite(v) for v in (entry, stop, target)) and 0 < target < entry < stop:
                    # The shared triage rule is LONG-oriented. Mirror only trend direction for a valid SHORT;
                    # reward:risk and chop inputs stay identical, and incomplete/wrong-side levels never opt in.
                    data = {**data, "timeframes": [
                        {**frame, "trend": "down" if frame.get("trend") == "up" else
                         "up" if frame.get("trend") == "down" else frame.get("trend")}
                        if isinstance(frame, dict) else frame for frame in data.get("timeframes") or []]}
        hits = hard_rejects(data) if data else []
    except Exception as e:  # a bad payload must not block the model path
        logger.warning("Triage rules skipped: %s", e)
        return None
    return f"TRIAGE_RULE_{hits[0]}" if hits else None


async def _account_refusal(conn, instrument_id: str) -> str | None:
    """The account-level checks that come before anything else for a debate: the reason it is refused, or None.
    Scanner candidates run them after their own dedupe; Brain-started debates run only these."""
    latched = await conn.fetchval(
        "SELECT is_latched FROM execution_kill_switch WHERE environment = 'PAPER' AND account_id = $1",
        PAPER_ACCOUNT_ID,
    )
    if latched:
        return "KILL_SWITCH_LATCHED"
    if await conn.fetchval("SELECT daily_loss_latched FROM paper_accounts WHERE account_id = $1", PAPER_ACCOUNT_ID):
        return "DAILY_LOSS_LATCHED"
    weight = await conn.fetchval(
        "SELECT COALESCE(SUM(weight_pct), 0) FROM agent_safe_positions WHERE instrument_id = $1", instrument_id
    )
    if float(weight or 0) > MAX_POSITION_PCT - MIN_ENTRY_PCT:
        return "POSITION_AT_CAP"
    return None


def profile_cap_key(mandate_id: Any, now_s: float) -> str:
    """ADR 0011 D-P1-6: one shared daily counter for every profile, across all worker replicas."""
    day = datetime.datetime.fromtimestamp(now_s, datetime.UTC).date().isoformat()
    return f"debate_gate:profile:{mandate_id}:{day}"


async def profile_budget_exhausted(conn, profile, now_s: float) -> bool:
    """Read the gateway's UTC cost ledger and the profile's trace-attributed share of the global cap."""
    start = datetime.datetime.combine(datetime.datetime.fromtimestamp(now_s, datetime.UTC).date(),
                                      datetime.time(), datetime.UTC)
    end = start + datetime.timedelta(days=1)
    cap = parse_cap(await conn.fetchval("SELECT value FROM system_settings WHERE key = 'LLM_DAILY_BUDGET_USD'"),
                    DEFAULT_DAILY_CAP_USD)
    row = await conn.fetchrow(
        """SELECT COALESCE(SUM(l.total_cost_usd), 0) AS global_spent,
           COALESCE(SUM(l.total_cost_usd) FILTER (WHERE t.mandate_id = $3), 0) AS profile_spent
           FROM llm_cost_ledger l LEFT JOIN ai_decision_traces t ON t.id = l.trace_id
           WHERE l.created_at >= $1 AND l.created_at < $2""", start, end, str(profile.id))
    total_weight = await conn.fetchval(
        "SELECT COALESCE(SUM(max_debates_per_day), 0) FROM ai_mandates WHERE status = 'ACTIVE'")
    weight = Decimal(str(total_weight or 0))
    share = cap * profile.max_debates_per_day / weight if weight > 0 else Decimal("0")
    return Decimal(str(row["global_spent"])) >= cap or Decimal(str(row["profile_spent"])) >= share


_RELEASE_PROFILE_BUDGET_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
_RENEW_PROFILE_BUDGET_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


@asynccontextmanager
async def profile_budget_guard(pg_pool, redis, profile):
    """Serialize profile calls and check the share using the UTC day at each call boundary."""
    if profile is None:
        yield
        return
    lock_key = f"debate_gate:profile_budget_lock:{profile.id}"
    lock_token = str(uuid.uuid4())
    while True:
        try:
            if await redis.set(lock_key, lock_token, nx=True, ex=PROFILE_BUDGET_LOCK_TTL_S):
                break
        except Exception as exc:
            raise ProfileBudgetLockError("PROFILE_BUDGET_LOCK_UNAVAILABLE") from exc
        # A live owner renews its bounded lease while a model call runs. Wait for it to finish
        # rather than turning ordinary 90s gateway latency into a false budget refusal.
        await asyncio.sleep(0.1)
    lease_lost = asyncio.Event()

    async def renew_lease():
        while True:
            await asyncio.sleep(PROFILE_BUDGET_LOCK_RENEW_S)
            try:
                renewed = await redis.eval(
                    _RENEW_PROFILE_BUDGET_LOCK_LUA, 1, lock_key, lock_token, PROFILE_BUDGET_LOCK_TTL_S
                )
            except Exception:
                renewed = 0
            if not renewed:
                lease_lost.set()
                return

    renew_task = asyncio.create_task(renew_lease())
    try:
        async with pg_pool.acquire() as conn:
            if await profile_budget_exhausted(conn, profile, _utc_now_s()):
                raise BudgetExceeded("debate.profile", "PROFILE_DAILY_SHARE_EXHAUSTED")
        yield
        try:
            owner = await redis.get(lock_key)
        except Exception:
            owner = None
        if isinstance(owner, bytes):
            owner = owner.decode()
        if lease_lost.is_set() or owner != lock_token:
            raise ProfileBudgetLockError("PROFILE_BUDGET_LOCK_LOST")
        async with pg_pool.acquire() as conn:
            if await profile_budget_exhausted(conn, profile, _utc_now_s()):
                raise BudgetExceeded("debate.profile", "PROFILE_DAILY_SHARE_EXHAUSTED")
    finally:
        renew_task.cancel()
        try:
            await renew_task
        except asyncio.CancelledError:
            pass
        try:
            await redis.eval(_RELEASE_PROFILE_BUDGET_LOCK_LUA, 1, lock_key, lock_token)
        except Exception:
            logger.warning("Profile budget lock %s could not be released; its lease will expire", profile.id)


_RESERVE_CANDIDATE_SLOT_LUA = """
if redis.call('GET', KEYS[2]) and redis.call('GET', KEYS[1]) then return 1 end
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
if used >= tonumber(ARGV[1]) then return 0 end
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
redis.call('SET', KEYS[2], '1', 'EX', ARGV[2])
return 1
"""


async def _profile_slot_full(redis, key: str, cap: int, candidate_id) -> bool:
    """A retry can reuse its reservation even when the daily counter is full."""
    if candidate_id and await redis.get(f"{key}:candidate:{candidate_id}"):
        return False
    return int(await redis.get(key) or 0) >= cap


async def _reserve_profile_slot(redis, key: str, cap: int, candidate_id=None) -> bool:
    """Reserve at most one daily slot per identified candidate, including crash retries."""
    if candidate_id:
        return bool(await redis.eval(_RESERVE_CANDIDATE_SLOT_LUA, 2, key,
                                     f"{key}:candidate:{candidate_id}", cap, PROFILE_CAP_TTL_S))
    used = await redis.incr(key)
    await redis.expire(key, PROFILE_CAP_TTL_S)
    if used > cap:
        await redis.decr(key)
        return False
    return True


async def _evaluate(pg_pool, redis, payload, gateway, brief, now_s, brief_data=None, profile=None) -> GateDecision:
    source = payload.get("trigger_source", "scanner")
    if source in BRAIN_SOURCES:
        async with pg_pool.acquire() as conn:
            refusal = await _account_refusal(conn, payload["instrument_id"])
        return GateDecision(False, refusal) if refusal else GateDecision(True, "BRAIN_TRIGGER")
    if source not in AUTOMATIC_SOURCES:
        return GateDecision(True, "OPERATOR_TRIGGER")

    instrument_id = payload["instrument_id"]
    candidate = json.loads(payload.get("candidate_payload") or "{}")
    timeframe = candidate.get("timeframe", "?")
    now_s = now_s if now_s is not None else datetime.datetime.now(datetime.UTC).timestamp()

    cid = None
    if candidate.get("candidate_id"):
        try:
            cid = uuid.UUID(str(candidate["candidate_id"]))
        except (TypeError, ValueError):
            cid = None  # a malformed id matches nothing: it can never dedupe another candidate

    async with pg_pool.acquire() as conn:
        if cid is not None and await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM debate_runs WHERE candidate_id = $1)", cid
        ):
            return GateDecision(False, "DUPLICATE_CANDIDATE")
        if refusal := await _account_refusal(conn, instrument_id):
            return GateDecision(False, refusal)

    close_ns = candidate.get("bar_close_inclusive_ns")
    if close_ns:
        max_age = max(MIN_STALE_S, 2 * TIMEFRAME_S.get(timeframe, 0))
        if now_s - int(close_ns) / 1e9 > max_age:
            return GateDecision(False, "STALE_CANDIDATE")

    if not brief:  # no market data: a debate would argue from nothing
        return GateDecision(False, "NO_BRIEF")

    slot_key = None
    cap = 0
    if profile is not None:
        if timeframe not in STYLE_TIMEFRAMES[profile.style]:
            return GateDecision(False, "TIMEFRAME_NOT_IN_STYLE")
        cap = profile.max_debates_per_day
        if gateway is None:
            slot_key = profile_cap_key(profile.id, _utc_now_s())
            if await _profile_slot_full(redis, slot_key, cap, cid):
                return GateDecision(False, "PROFILE_DEBATE_DAILY_CAP")
            async with pg_pool.acquire() as conn:
                if await profile_budget_exhausted(conn, profile, _utc_now_s()):
                    return GateDecision(False, "AI_BUDGET_EXHAUSTED")

    cooldown_key = f"debate:cooldown:{instrument_id}:{timeframe}"
    # Candidate-aware: the value is the candidate's token, so a crash or cancellation after this point
    # leaves a window that its OWN redelivery still passes (no durable row exists yet), while every other
    # candidate waits it out. Unidentified payloads keep the legacy opaque value and the old semantics.
    cooldown_token = f"cid:{cid}" if cid else "1"
    held = await redis.get(cooldown_key)
    if held is None:
        if not await redis.set(cooldown_key, cooldown_token, nx=True, ex=COOLDOWN_S):
            if cid is None or await redis.get(cooldown_key) != cooldown_token:
                return GateDecision(False, "COOLDOWN")
    elif cid is None or held != cooldown_token:
        return GateDecision(False, "COOLDOWN")

    # Like a model rejection, a rule rejection keeps the cooldown: one decision per setup window
    if (rule := _rule_rejection(brief_data)) is not None:
        return GateDecision(False, rule)

    if gateway is None:
        if profile is not None:
            slot_key = profile_cap_key(profile.id, _utc_now_s())
        if slot_key is not None and not await _reserve_profile_slot(redis, slot_key, cap, cid):
            await release_cooldown(redis, cooldown_key, cooldown_token)
            return GateDecision(False, "PROFILE_DEBATE_DAILY_CAP")
        return GateDecision(True, "NO_TRIAGE_GATEWAY", cooldown_key=cooldown_key, cooldown_token=cooldown_token)
    try:
        # The setup's decision_trace_id as the trace parent links triage (and its F2 shadow) to outcomes
        async with profile_budget_guard(pg_pool, redis, profile):
            if profile is not None:
                slot_key = profile_cap_key(profile.id, _utc_now_s())
                if await _profile_slot_full(redis, slot_key, cap, cid):
                    await release_cooldown(redis, cooldown_key, cooldown_token)
                    return GateDecision(False, "PROFILE_DEBATE_DAILY_CAP")
            result = await TRIAGE.run(gateway, brief, parent_trace_id=_trace_uuid(payload),
                                      context=CallContext(role=TRIAGE.role, mandate_id=str(profile.id)) if profile else None)
    except BudgetExceeded:
        await release_cooldown(redis, cooldown_key, cooldown_token)
        return GateDecision(False, "AI_BUDGET_EXHAUSTED")
    except Exception as e:  # gateway, budget or anything else: no triage, no paid debate; retry next candidate
        logger.warning("Triage unavailable for %s: %s", instrument_id, e)
        await release_cooldown(redis, cooldown_key, cooldown_token)
        return GateDecision(False, "TRIAGE_UNAVAILABLE")
    if not result.output.worth_debate:
        return GateDecision(False, "TRIAGE_REJECTED", result.output, result.trace_id)
    if profile is not None:
        slot_key = profile_cap_key(profile.id, _utc_now_s())
    if slot_key is not None and not await _reserve_profile_slot(redis, slot_key, cap, cid):
        await release_cooldown(redis, cooldown_key, cooldown_token)
        return GateDecision(False, "PROFILE_DEBATE_DAILY_CAP", result.output, result.trace_id)
    return GateDecision(True, "TRIAGE_ACCEPTED", result.output, result.trace_id, cooldown_key, cooldown_token)
