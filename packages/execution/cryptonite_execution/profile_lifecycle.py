"""The auto-trade profile lifecycle: Start, Pause, Stop, finish Stop (ADR 0011 D5, D6, D10, D11, D13).

This is the single implementation. The API routes, the orchestrator's monitor and the Brain call it; nothing else
changes ``ai_mandates.status`` between ACTIVE, PAUSED, STOPPING and STOPPED, and nothing else writes the coin locks.

- ``start`` is one transaction: ``SELECT ... FOR UPDATE`` on the mandate, the master switch (an Ask profile is exempt),
  the profile rules, TOTP above Ask, the capital cap, the coin locks, ACTIVE, the audit rows. Two profiles that share a
  coin cannot both start: the lock table's primary key lets exactly one insert win and the other gets ``COIN_LOCKED``
  naming the holder.
- A profile in ACTIVE, PAUSED, STOPPING or HALTED holds its coin locks. ``finish_stop`` (STOPPING and flat) and archiving
  release them; ``pause`` and a halt keep them.
- ``archive`` retires a STOPPED profile (or a flat PAUSED/HALTED one) and releases its locks.
- ``stop`` stops new entries; with ``close`` it also queues reduce-only closes. Exits never stop (D13).

Audit: ``ai_autonomy_events`` kinds STARTED, STOPPING, STOPPED and ARCHIVED (migration 050), and ``ai_profile_decisions`` (LIFECYCLE).
"""

import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from cryptonite_schemas.mandate import Mandate, MandateStatus
from cryptonite_schemas.profile import (
    DecisionStage,
    ExecutionMode,
    base_asset,
    holds_coin_locks,
    lock_keys,
    profile_errors,
)
from cryptonite_schemas.runtime_settings import (
    AUTOTRADE_ENABLED_KEY,
    AUTOTRADE_MAX_ALLOCATED_KEY,
    RUNTIME_BOUNDS,
    read_number,
)

from cryptonite_execution.autonomy import (
    AutonomyState,
    AutonomyTrigger,
    Transition,
    TransitionRefused,
    transition,
)
from cryptonite_execution.flatten import flatten_instance
from cryptonite_execution.mandate_store import apply_transition, load_mandate, record_event
from cryptonite_execution.profile_log import record_decision

logger = logging.getLogger(__name__)

PAPER_ACCOUNT_ID = "PAPER-ACCOUNT-001"
# The statuses whose budgets count against the cap (D11); the same four hold coin locks (D6)
ALLOCATING_STATUSES = ("ACTIVE", "PAUSED", "STOPPING", "HALTED")
# Statuses a start takes back (the operator's resume); they need TOTP at every mode
RESUMING_STATUSES = (MandateStatus.PAUSED, MandateStatus.HALTED, MandateStatus.STOPPING)
# Entry orders still working; a protective STOP_MARKET belongs to a position, not to the entry side
WORKING_ORDER_STATUSES = ("PENDING", "SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED")


class LifecycleError(Exception):
    """A lifecycle request that is refused: a stable ``code`` and a ``detail`` dict for the caller to show."""

    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


@dataclass(frozen=True)
class AutotradeSettings:
    enabled: bool = False
    max_allocated_pct: Decimal = Decimal(str(RUNTIME_BOUNDS[AUTOTRADE_MAX_ALLOCATED_KEY].default))


def parse_settings(stored: Mapping[str, Any] | AutotradeSettings | None) -> AutotradeSettings:
    """The two auto-trade settings from stored values (``system_settings.value``); a missing or invalid value takes
    the default, which is the safe side (switch off, 90%)."""
    if isinstance(stored, AutotradeSettings):
        return stored
    stored = stored or {}
    out = {}
    for key in (AUTOTRADE_ENABLED_KEY, AUTOTRADE_MAX_ALLOCATED_KEY):
        bound = RUNTIME_BOUNDS[key]
        value, problem = read_number(stored.get(key), bound, bound.default)
        if problem:
            logger.warning("%s: %s; the default %g applies", key, problem, bound.default)
            value = bound.default
        out[key] = value
    return AutotradeSettings(enabled=out[AUTOTRADE_ENABLED_KEY] >= 1,
                             max_allocated_pct=Decimal(str(out[AUTOTRADE_MAX_ALLOCATED_KEY])))


async def load_settings(conn) -> AutotradeSettings:
    rows = await conn.fetch("SELECT key, value FROM system_settings WHERE key = ANY($1)",
                            [AUTOTRADE_ENABLED_KEY, AUTOTRADE_MAX_ALLOCATED_KEY])
    return parse_settings({r["key"]: r["value"] for r in rows})


# The master switch applies at runtime, not only at Start (ADR 0011 D10): while it is off a VETO or AUTO profile behaves
# as ASK. The runtime paths (routing, the mandate gate) read it through this loader, cached for a short while.
SWITCH_CACHE_S = 30.0
_switch_cache: tuple[float, bool] | None = None


def reset_switch_cache() -> None:
    global _switch_cache
    _switch_cache = None


async def autotrade_switch_on(conn) -> bool:
    """``autotrade.enabled`` as the runtime paths read it: cached ``SWITCH_CACHE_S``; OFF when it is missing, invalid or
    cannot be read (fail safe: the switch is the thing that must never be on by accident)."""
    global _switch_cache
    now = time.monotonic()
    if _switch_cache is not None and _switch_cache[0] > now:
        return _switch_cache[1]
    try:
        async with conn.transaction():  # a savepoint: a failed read must not abort the caller's transaction
            raw = await conn.fetchval("SELECT value FROM system_settings WHERE key = $1", AUTOTRADE_ENABLED_KEY)
    except Exception as e:
        logger.warning("Could not read %s: %s; the switch counts as off", AUTOTRADE_ENABLED_KEY, e)
        return False
    on = parse_settings({AUTOTRADE_ENABLED_KEY: raw}).enabled
    _switch_cache = (now + SWITCH_CACHE_S, on)
    return on


async def _equity(conn) -> Decimal:
    return Decimal(str(await conn.fetchval(
        "SELECT total_equity FROM paper_accounts WHERE account_id = $1", PAPER_ACCOUNT_ID) or 0))


async def lock_capital(conn) -> None:
    """Serialise the capital check across transactions (``start`` and a budget raise): two checks that each see room
    cannot both commit past the cap. A transaction-scoped advisory lock, so it is released at commit or rollback;
    call inside the transaction, before ``check_capital``."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('autotrade.capital'))")


async def check_capital(conn, mandate_id: uuid.UUID, budget: Decimal, settings: AutotradeSettings) -> None:
    """D11: the budgets of the profiles that hold capital, this one at ``budget``, stay within the cap.
    Raises ``CAPITAL_OVER_ALLOCATED``."""
    allocated = Decimal(str(await conn.fetchval(
        "SELECT COALESCE(SUM(capital_budget_usd), 0) FROM ai_mandates WHERE status = ANY($1) AND id <> $2",
        list(ALLOCATING_STATUSES), mandate_id) or 0))
    equity = await _equity(conn)
    if allocated + budget > equity * settings.max_allocated_pct / 100:
        raise LifecycleError("CAPITAL_OVER_ALLOCATED", {
            "allocated": str(allocated), "budget": str(budget), "equity": str(equity),
            "cap_pct": str(settings.max_allocated_pct)})


async def coin_holder(conn, instrument_id: str) -> tuple[uuid.UUID, str] | None:
    """The (mandate id, name) that holds the coin lock on the instrument's base asset, or None."""
    row = await conn.fetchrow(
        """SELECT l.mandate_id, m.name FROM ai_profile_coin_locks l JOIN ai_mandates m ON m.id = l.mandate_id
           WHERE l.base_asset = $1 AND m.status = ANY($2)""", base_asset(instrument_id), list(ALLOCATING_STATUSES))
    return (row["mandate_id"], row["name"]) if row else None


async def coin_lock_conflict(conn, mandate: Mandate, instrument_id: str, what: str) -> str | None:
    """The ``COIN_LOCKED`` refusal detail when another mandate holds the instrument's coin, else None (D6). For the
    entry checks of the Brain card and the inbox debate; ``what`` names the thing to reject ("card", "debate"). Read
    only. An instrument with no known base asset is not refused here (the orchestrator's gate decides)."""
    try:
        holder = await coin_holder(conn, instrument_id)
    except ValueError:
        return None
    if holder is None or holder[0] == mandate.id:
        return None
    return (f"COIN_LOCKED: {base_asset(instrument_id)} belongs to profile {holder[1]}; mandate {mandate.name} cannot "
            f"enter {instrument_id}, so nothing was queued; reject this {what}")


LOCK_ATTEMPTS = 3


async def acquire_locks(conn, mandate_id: uuid.UUID, instruments) -> None:
    """Insert the coin locks of ``instruments`` for the mandate, in a fixed order (no lock-order deadlock between
    two concurrent starts). A coin another mandate holds raises ``COIN_LOCKED`` with the holder. Call inside the
    transaction that sets the status."""
    for coin in sorted(lock_keys(instruments)):
        # a lock whose holder was archived by hand (or stopped without finish_stop) holds nothing: clear it
        await conn.execute(
            """DELETE FROM ai_profile_coin_locks l USING ai_mandates m
               WHERE m.id = l.mandate_id AND l.base_asset = $1 AND m.status IN ('ARCHIVED', 'STOPPED')""", coin)
        for _ in range(LOCK_ATTEMPTS):
            async with conn.transaction():  # a savepoint: the lookup below must not run in an aborted transaction
                inserted = await conn.fetchval(
                    """INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ($1, $2)
                       ON CONFLICT (base_asset) DO NOTHING RETURNING 1""", coin, mandate_id)
            if inserted:
                break
            row = await conn.fetchrow(
                """SELECT l.mandate_id, m.name FROM ai_profile_coin_locks l JOIN ai_mandates m ON m.id = l.mandate_id
                   WHERE l.base_asset = $1""", coin)
            if row is None:
                continue  # the holder released it between the insert and the lookup: take it now
            if row["mandate_id"] == mandate_id:
                break  # already ours
            raise LifecycleError("COIN_LOCKED", {"coin": coin, "holder_id": str(row["mandate_id"]),
                                                 "holder_name": row["name"]})
        else:  # never go on without the lock
            raise LifecycleError("COIN_LOCKED", {"coin": coin, "holder_id": None, "holder_name": None})


async def _coins_in_use(conn, instance_id: uuid.UUID | str) -> set[str]:
    """Base assets the instance still has an open position or a working order on (the coins it cannot let go)."""
    rows = await conn.fetch(
        """SELECT instrument_id FROM paper_positions WHERE instance_id = $1 AND status = 'OPEN' AND quantity > 0
           UNION SELECT instrument_id FROM paper_orders WHERE instance_id = $1 AND status = ANY($2)""",
        instance_id, list(WORKING_ORDER_STATUSES))
    coins = set()
    for r in rows:
        try:
            coins.add(base_asset(r["instrument_id"]))
        except ValueError:
            continue
    return coins


async def sync_locks(conn, mandate: Mandate) -> list[str]:
    """Make the held locks match the instruments of a mandate that holds locks (an instrument edit): new coins are
    locked (``COIN_LOCKED`` if another profile has one), coins no instrument covers any more are released, except a
    coin the mandate's instance still has an open position or a working order on: that one stays locked (its exits
    keep running, D13) and is returned, sorted, as ``locks_retained``. A mandate that does not hold locks (STOPPED,
    ARCHIVED) is left alone."""
    if not holds_coin_locks(mandate.status.value):
        return []
    wanted = lock_keys(mandate.instruments)
    await acquire_locks(conn, mandate.id, mandate.instruments)
    held = {r["base_asset"] for r in await conn.fetch(
        "SELECT base_asset FROM ai_profile_coin_locks WHERE mandate_id = $1", mandate.id)}
    retained = (held - wanted) & await _coins_in_use(conn, mandate.instance_id)
    await conn.execute("DELETE FROM ai_profile_coin_locks WHERE mandate_id = $1 AND NOT (base_asset = ANY($2))",
                       mandate.id, sorted(wanted | retained))
    return sorted(retained)


async def release_locks(conn, mandate_id: uuid.UUID) -> int:
    """Delete the mandate's coin locks (``finish_stop`` and archive only). Returns how many were held."""
    result = await conn.execute("DELETE FROM ai_profile_coin_locks WHERE mandate_id = $1", mandate_id)
    return int(result.rsplit(" ", 1)[-1])


async def _load(conn, mandate_id: uuid.UUID | str) -> Mandate:
    m = await load_mandate(conn, mandate_id, for_update=True)
    if m is None:
        raise LifecycleError("MANDATE_NOT_FOUND")
    return m


async def _cancel_pending_add_risk(conn, mandate_id: uuid.UUID) -> None:
    await conn.execute(
        """UPDATE ai_approvals SET status = 'CANCELLED', decided_via = 'system', decided_at = now()
           WHERE mandate_id = $1 AND status = 'PENDING' AND action_class = 'ADD_RISK'""", mandate_id)


async def _set_status(conn, m: Mandate, status: MandateStatus, kind_detail: str, *, actor: str, reason: str) -> None:
    """Move to STOPPING / STOPPED: status, audit event. (ACTIVE, PAUSED and HALTED go through ``apply_transition``.)"""
    await conn.execute("UPDATE ai_mandates SET status = $2, updated_at = now() WHERE id = $1", m.id, status.value)
    await record_event(conn, m.id, status.value, from_level=int(m.autonomy_level), to_level=int(m.autonomy_level),
                       reason=f"{status.value}: {reason}", actor=actor,
                       detail={"lifecycle": status.value, "from": m.status.value, "step": kind_detail})


async def start(conn, mandate_id: uuid.UUID | str, *, actor: str, totp_ok: bool,
                settings: Mapping[str, Any] | AutotradeSettings | None, reason: str | None = None) -> Mandate:
    """Start (or restart) a profile. One transaction; every refusal is a ``LifecycleError`` and changes nothing.

    Order: master switch (Ask is exempt), profile rules, TOTP (above Ask, and for a PAUSED, HALTED or STOPPING profile,
    as the resume was), capital cap, coin locks, ACTIVE. Starting an ACTIVE profile is a no-op success."""
    cfg = parse_settings(settings)
    async with conn.transaction():
        m = await _load(conn, mandate_id)
        if m.status is MandateStatus.ACTIVE:
            return m
        if m.status is MandateStatus.ARCHIVED:
            raise LifecycleError("NOT_STARTABLE", {"status": m.status.value})
        mode = m.execution_mode
        if mode is not ExecutionMode.ASK and not cfg.enabled:
            raise LifecycleError("AUTOTRADE_DISABLED", {"mode": mode.value})
        if errors := profile_errors(m.profile_fields(), running=True):
            raise LifecycleError("PROFILE_INVALID", {"codes": errors})
        if (mode is not ExecutionMode.ASK or m.status in RESUMING_STATUSES) and not totp_ok:
            raise LifecycleError("TOTP_REQUIRED", {"mode": mode.value, "status": m.status.value})
        await lock_capital(conn)
        await check_capital(conn, m.id, m.capital_budget_usd, cfg)
        await acquire_locks(conn, m.id, m.instruments)
        t = Transition(AutonomyState(m.autonomy_level, m.status, m.allow_perps),
                       AutonomyState(m.autonomy_level, MandateStatus.ACTIVE, m.allow_perps), "STARTED",
                       reason or f"operator started the profile ({m.status.value} -> ACTIVE)")
        await apply_transition(conn, m.id, t, actor=actor, detail={"lifecycle": "STARTED", "mode": mode.value,
                                                                    "from": m.status.value})
        await record_decision(conn, m.id, DecisionStage.LIFECYCLE, "STARTED", t.reason, mode=mode.value,
                              was=m.status.value, actor=actor)
        return await _load(conn, m.id)


async def pause(conn, mandate_id: uuid.UUID | str, *, actor: str, reason: str = "operator pause") -> Mandate:
    """ACTIVE -> PAUSED: no new entries, locks and exits kept. Pending ADD_RISK approvals are cancelled."""
    async with conn.transaction():
        m = await _load(conn, mandate_id)
        try:
            t = transition(AutonomyState(m.autonomy_level, m.status, m.allow_perps), AutonomyTrigger.PAUSE,
                           reason=reason or "operator pause")
        except TransitionRefused as e:
            raise LifecycleError(e.code, {"status": m.status.value}) from e
        await apply_transition(conn, m.id, t, actor=actor)
        await _cancel_pending_add_risk(conn, m.id)
        await record_decision(conn, m.id, DecisionStage.LIFECYCLE, "PAUSED", t.reason, actor=actor)
        return await _load(conn, m.id)


async def stop(conn, mandate_id: uuid.UUID | str, *, actor: str, close: bool) -> Mandate:
    """ACTIVE, PAUSED or HALTED -> STOPPING (a halted profile stops without TOTP: stopping only reduces risk): no new entries, the coin locks are kept until the profile is flat. With ``close``
    a reduce-only CLOSE_POSITION is queued for every open position of the profile; without it the positions run on
    their exits. A profile that is flat is finished at once (STOPPED, locks released)."""
    async with conn.transaction():
        m = await _load(conn, mandate_id)
        if m.status is MandateStatus.STOPPED:
            return m
        if m.status not in (MandateStatus.ACTIVE, MandateStatus.PAUSED, MandateStatus.HALTED, MandateStatus.STOPPING):
            raise LifecycleError("NOT_STOPPABLE", {"status": m.status.value})
        if m.status is not MandateStatus.STOPPING:
            await _set_status(conn, m, MandateStatus.STOPPING, "stop", actor=actor,
                              reason="stop and close" if close else "stop, positions run on their exits")
            await _cancel_pending_add_risk(conn, m.id)
            await record_decision(conn, m.id, DecisionStage.LIFECYCLE, "STOPPING", None, actor=actor, close=close)
        closes = 0
        if close:
            closes = await flatten_instance(conn, m.instance_id, "PROFILE_STOP", m.id, PAPER_ACCOUNT_ID)
        if await _is_flat(conn, m):
            await finish_stop(conn, m.id)
        elif close:
            logger.info("Profile %s stopping: %d close(s) queued", m.name, closes)
        return await _load(conn, m.id)


async def _is_flat(conn, m: Mandate) -> bool:
    """No open position and no working entry order under the profile's instance."""
    if await conn.fetchval("SELECT 1 FROM paper_positions WHERE instance_id = $1 AND status = 'OPEN' AND quantity > 0 LIMIT 1",
                           m.instance_id):
        return False
    return not await conn.fetchval(
        "SELECT 1 FROM paper_orders WHERE instance_id = $1 AND status = ANY($2) AND order_type <> 'STOP_MARKET' LIMIT 1",
        m.instance_id, list(WORKING_ORDER_STATUSES))


async def finish_stop(conn, mandate_id: uuid.UUID | str) -> bool:
    """STOPPING and flat -> STOPPED, coin locks released. True when it moved, False when it is not STOPPING or still
    holds a position or a working order. Safe to call every monitor pass."""
    async with conn.transaction():
        m = await load_mandate(conn, mandate_id, for_update=True)
        if m is None or m.status is not MandateStatus.STOPPING or not await _is_flat(conn, m):
            return False
        released = await release_locks(conn, m.id)
        await _set_status(conn, m, MandateStatus.STOPPED, "finish_stop", actor="risk:mandate_monitor",
                          reason=f"flat, {released} coin lock(s) released")
        await record_decision(conn, m.id, DecisionStage.LIFECYCLE, "STOPPED", "flat, locks released",
                              released=released)
        return True


async def archive(conn, mandate_id: uuid.UUID | str, *, actor: str) -> Mandate:
    """Retire a profile for good (no TOTP: it only removes risk). Allowed from STOPPED, or from PAUSED or HALTED when
    the profile is flat; anything else (ACTIVE, STOPPING, or a position still open) is ``NOT_ARCHIVABLE``. The coin
    locks are released in the same transaction and the audit event kind is ARCHIVED. Archiving an ARCHIVED profile is
    a no-op."""
    async with conn.transaction():
        m = await _load(conn, mandate_id)
        if m.status is MandateStatus.ARCHIVED:
            return m
        if m.status is not MandateStatus.STOPPED:
            if m.status not in (MandateStatus.PAUSED, MandateStatus.HALTED):
                raise LifecycleError("NOT_ARCHIVABLE", {"status": m.status.value})
            if not await _is_flat(conn, m):
                raise LifecycleError("NOT_ARCHIVABLE", {"status": m.status.value, "reason": "open position or working order"})
        released = await release_locks(conn, m.id)
        await _cancel_pending_add_risk(conn, m.id)
        await _set_status(conn, m, MandateStatus.ARCHIVED, "archive", actor=actor,
                          reason=f"{released} coin lock(s) released")
        await record_decision(conn, m.id, DecisionStage.LIFECYCLE, "ARCHIVED", "archived, locks released",
                              released=released, was=m.status.value, actor=actor)
        return await _load(conn, m.id)
