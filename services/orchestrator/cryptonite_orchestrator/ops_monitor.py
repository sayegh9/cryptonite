"""Operations publishing from the orchestrator (remediation R1.8, R7.5).

- ``risk:effective_limits``: the limits this process's ``GuardedOrderDispatcher`` actually enforces
  (read from its attributes, never from constants elsewhere) plus the account backstop, published at
  start-up and every ``OPS_EVERY_S``. The API's ``GET /control/enforced-limits`` serves it; the key
  expires when the orchestrator stops publishing, so a dead orchestrator reads as "unknown".
  Before each publish the operator's tighten-only overrides (F7, ``risk_overrides``) are re-read and
  applied, so the published values are the applied ones, including the F4 cross-mandate cap
  (``cross_mandate_exposure_pct``). The 30-day betas the F4 check uses are refreshed hourly here too.
- Heartbeat alerts: learner, exit monitor, local model tier and ``sentiment:last_ok`` are checked on
  the same timer; a heartbeat that goes down raises one alert through the existing alert path
  (``channel:system_alerts`` + ``notifications_outbox``, where it queues while the opt-in notifier is
  stopped), and a recovery raises an INFO one. A heartbeat that was never seen is "unknown", not down.
- Stuck commands: a command still PENDING, CLAIMED or RUNNING after ``STUCK_COMMAND_S`` (a CLOSE_POSITION after
  ``STOP_COMMAND_S``) raises one CRITICAL alert per command id, through the same alert path.
"""

import asyncio
import datetime
import json
import logging
import os
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any

from cryptonite_schemas.mandate import ACCOUNT_BACKSTOP_DRAWDOWN_PCT
from cryptonite_schemas.runtime_settings import RUNTIME_BOUNDS, read_number

from cryptonite_orchestrator import risk_overrides
from cryptonite_orchestrator.mandate_monitor import alert

logger = logging.getLogger("cryptonite.orchestrator.ops_monitor")

EFFECTIVE_LIMITS_KEY = "risk:effective_limits"
OPS_EVERY_S = 60
EFFECTIVE_LIMITS_TTL_S = 3 * OPS_EVERY_S
HEARTBEAT_GRACE_S = float(os.getenv("HEARTBEAT_ALERT_GRACE_S", "300"))  # down this long before alerting
SENTIMENT_STALE_S = float(os.getenv("SENTIMENT_STALE_ALERT_S", str(24 * 3600)))  # only debates exercise it

LEARNER_HEARTBEAT = "learner:heartbeat"
EXIT_MONITOR_HEARTBEAT = "exit_monitor:heartbeat"
LOCAL_TIER_HEALTH = "local_tier:health"
SENTIMENT_LAST_OK = "sentiment:last_ok"

STUCK_COMMAND_S = 120  # any command still PENDING, CLAIMED or RUNNING this long since it was queued
STOP_COMMAND_S = 30  # a CLOSE_POSITION (a stop) waiting this long: the orchestrator queue may be blocked
STUCK_COMMAND_WINDOW_H = 24  # only PAPER commands queued this recently: an older row is history, not a live block
# ...except an UNKNOWN CLOSE_POSITION, which blocks stops for its instrument until someone resolves it, however old
UNKNOWN_OUTCOME = "UNKNOWN_PENDING_RECONCILIATION"
RUNBOOK = "docs/runbooks/command-queue.md"


def _plain(value: Decimal) -> str:
    """Decimal('5.000') -> '5'; Decimal('0.5') -> '0.5' (no exponent)."""
    return format(value.normalize(), "f")


def effective_limits(dispatcher, now: datetime.datetime | None = None,
                     cross_mandate_exposure_pct: Decimal | None = None) -> dict[str, Any]:
    """The contract of ``GET /api/v1/control/enforced-limits`` from the dispatcher's own attributes
    (and the F4 cap this process applies)."""
    now = now or datetime.datetime.now(datetime.UTC)
    if cross_mandate_exposure_pct is None:
        cross_mandate_exposure_pct = risk_overrides.STATE.cross_cap_pct
    pct = Decimal("100")
    enforced_at = risk_overrides.STATE.enforced_at.isoformat().replace("+00:00", "Z") if getattr(risk_overrides.STATE, "enforced_at", None) else None
    return {
        "as_of": now.isoformat().replace("+00:00", "Z"),
        "effective_version": int(getattr(risk_overrides.STATE, "last_applied_version", 0)) if getattr(risk_overrides.STATE, "last_applied_version", 0) else 0,
        "enforced_at": enforced_at,
        "paper": {
            "max_daily_loss_pct": _plain(Decimal(str(dispatcher.max_daily_loss_pct)) * pct),
            "min_order_equity_pct": _plain(Decimal(str(dispatcher.min_order_equity_pct)) * pct),
            "max_order_equity_pct": _plain(Decimal(str(dispatcher.max_position_concentration_pct)) * pct),
            "taker_fee_bps": _plain(Decimal(str(dispatcher.taker_fee)) * Decimal("10000")),
            "account_backstop_drawdown_pct": _plain(Decimal(str(ACCOUNT_BACKSTOP_DRAWDOWN_PCT))),
            "cross_mandate_exposure_pct": _plain(Decimal(str(cross_mandate_exposure_pct))),
        },
    }


async def publish_effective_limits(engine, redis, now: datetime.datetime | None = None) -> dict[str, Any] | None:
    if redis is None:
        return None
    limits = effective_limits(engine.dispatcher, now)
    await redis.set(EFFECTIVE_LIMITS_KEY, json.dumps(limits), ex=EFFECTIVE_LIMITS_TTL_S)
    return limits


def _text(raw) -> str | None:
    return raw.decode() if isinstance(raw, bytes) else raw


ALERT_KEYS = ("alerts.heartbeat_grace_s", "alerts.sentiment_stale_s")


async def load_alert_settings(pool, last_good: tuple[float, float]) -> tuple[float, float]:
    """The heartbeat grace and the sentiment staleness for this pass (ADR 0007): the stored values, read every pass.
    A failed read, or a refused value, keeps the last valid one."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM system_settings WHERE key = ANY($1)", list(ALERT_KEYS))
    except Exception as e:
        logger.warning("Alert settings not re-read (%s); keeping the last valid values", e)
        return last_good
    stored = {r["key"]: r["value"] for r in rows}
    grace_bound = replace(RUNTIME_BOUNDS["alerts.heartbeat_grace_s"], default=HEARTBEAT_GRACE_S)
    stale_bound = replace(RUNTIME_BOUNDS["alerts.sentiment_stale_s"], default=SENTIMENT_STALE_S)
    grace, grace_problem = read_number(stored.get(ALERT_KEYS[0]), grace_bound, last_good[0])
    stale, stale_problem = read_number(stored.get(ALERT_KEYS[1]), stale_bound, last_good[1])
    for key, problem in zip(ALERT_KEYS, (grace_problem, stale_problem)):
        if problem:
            logger.error("Alert setting refused, keeping the last valid value: %s: %s", key, problem)
    return grace, stale


async def heartbeat_states(redis, now_s: float, sentiment_stale_s: float = SENTIMENT_STALE_S) -> dict[str, bool | None]:
    """name -> True (up), False (down) or None (unknown: never published)."""
    learner = await redis.get(LEARNER_HEARTBEAT)  # both expire when their loop stops
    exit_monitor = await redis.get(EXIT_MONITOR_HEARTBEAT)
    states: dict[str, bool | None] = {"learner": bool(learner), "exit_monitor": bool(exit_monitor)}
    local = _text(await redis.get(LOCAL_TIER_HEALTH))
    try:  # written only once a gateway has probed the tier (a role is routed to it)
        states["local_tier"] = bool(json.loads(local).get("healthy")) if local else None
    except (TypeError, ValueError, AttributeError):
        states["local_tier"] = None
    last_ok = _text(await redis.get(SENTIMENT_LAST_OK))
    try:
        states["sentiment"] = (now_s - float(last_ok)) <= sentiment_stale_s if last_ok else None
    except (TypeError, ValueError):
        states["sentiment"] = None
    return states


HEARTBEAT_TEXT = {
    "learner": "Learner heartbeat missing: outcomes and reflections are not being written",
    "exit_monitor": "Exit monitor heartbeat missing: AI positions have no stop/trail protection; Brain approvals are refused",
    "local_tier": "Local model tier down: local roles fall back to the cloud model; trade decisions fail closed",
    "sentiment": "Sentiment service has not tagged headlines recently: the Sentiment Analyst judges raw headlines",
}


class HeartbeatWatch:
    """Raises one alert per outage (after ``grace_s``) and one when it recovers."""

    def __init__(self, grace_s: float = HEARTBEAT_GRACE_S) -> None:
        self.grace_s = grace_s
        self.down_since: dict[str, float] = {}
        self.alerted: set[str] = set()

    def step(self, states: dict[str, bool | None], now_s: float) -> list[tuple[str, str]]:
        """Transitions to report: (name, "down" | "recovered")."""
        events: list[tuple[str, str]] = []
        for name, up in states.items():
            if up is False:
                since = self.down_since.setdefault(name, now_s)
                if name not in self.alerted and now_s - since >= self.grace_s:
                    self.alerted.add(name)
                    events.append((name, "down"))
            elif up is True:
                self.down_since.pop(name, None)
                if name in self.alerted:
                    self.alerted.discard(name)
                    events.append((name, "recovered"))
        return events


async def check_heartbeats(pool, redis, watch: HeartbeatWatch, now_s: float | None = None,
                           sentiment_stale_s: float = SENTIMENT_STALE_S) -> list[tuple[str, str]]:
    if redis is None:
        return []
    now_s = now_s if now_s is not None else time.time()
    events = watch.step(await heartbeat_states(redis, now_s, sentiment_stale_s), now_s)
    for name, what in events:
        episode = str(int(watch.down_since.get(name, now_s))) if what == "down" else str(int(now_s))
        detail = {"heartbeat": name, "episode": episode}
        if what == "down":
            message, tier = HEARTBEAT_TEXT[name], "CRITICAL"
            logger.error("Heartbeat down: %s", name)
        else:
            message, tier = f"{name.replace('_', ' ').capitalize()} heartbeat recovered", "INFO"
            logger.info("Heartbeat recovered: %s", name)
        async with pool.acquire() as conn:
            await alert(conn, redis, f"HEARTBEAT_{what.upper()}", message, detail, tier=tier,
                        dedupe_key=f"heartbeat:{name}:{what}:{episode}")
    return events


STUCK_COMMANDS_SQL = """
    SELECT command_id, command_type, target_instance_id, status, command_outcome,
           payload->>'instrument_id' AS instrument_id,
           EXTRACT(EPOCH FROM (now() - created_at)) AS age_s
    FROM execution_commands
    WHERE environment = 'PAPER'
      AND status IN ('PENDING', 'CLAIMED', 'RUNNING')
      AND (created_at > now() - make_interval(hours => $3::int)
           OR (command_type = 'CLOSE_POSITION' AND command_outcome = 'UNKNOWN_PENDING_RECONCILIATION'))
      AND (created_at < now() - make_interval(secs => $1::float8)
           OR (command_type = 'CLOSE_POSITION' AND created_at < now() - make_interval(secs => $2::float8)))
    ORDER BY created_at
"""


def stuck_command_message(row) -> str:
    """The alert text for one stuck command. An UNKNOWN_PENDING_RECONCILIATION row is not a queue problem: it is a
    held outcome, and an unresolved CLOSE_POSITION blocks stops for its instrument until it is resolved."""
    kind, target, age = row["command_type"], row["target_instance_id"] or "-", int(row["age_s"])
    if row["command_outcome"] == UNKNOWN_OUTCOME:
        if kind == "CLOSE_POSITION":
            instrument = row["instrument_id"] or target
            return (f"CLOSE_POSITION for {target} is UNKNOWN_PENDING_RECONCILIATION after {age}s: stops for "
                    f"{instrument} are blocked until this command is resolved. Runbook: {RUNBOOK}")
        return (f"{kind} for {target} is UNKNOWN_PENDING_RECONCILIATION after {age}s: it is held and never retried. "
                f"Runbook: {RUNBOOK}")
    if kind == "CLOSE_POSITION":
        return (f"A stop is waiting: CLOSE_POSITION for {target} pending {age}s "
                "— the orchestrator queue may be blocked")
    return f"{kind} for {target} is {row['status']} after {age}s — the orchestrator queue may be blocked"


async def check_stuck_commands(pool, redis, alerted: set[str]) -> list[str]:
    """One CRITICAL alert for each PAPER command that has waited too long (queued in the last STUCK_COMMAND_WINDOW_H
    hours, or an UNKNOWN CLOSE_POSITION at any age; an older row is history, not a live block): the orchestrator queue
    may be blocked, or, for an UNKNOWN outcome, the command is held until someone resolves it
    (docs/runbooks/command-queue.md).

    ``alerted`` holds the command ids already alerted (one alert per command; the outbox dedupe key backs it up) and
    forgets a command once it has left the queue. Returns the command ids alerted by this pass."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(STUCK_COMMANDS_SQL, float(STUCK_COMMAND_S), float(STOP_COMMAND_S),
                                STUCK_COMMAND_WINDOW_H)
        waiting = {str(r["command_id"]) for r in rows}
        new = []
        for r in rows:
            cid = str(r["command_id"])
            if cid in alerted:
                continue
            alerted.add(cid)
            new.append(cid)
            target, age = r["target_instance_id"] or "-", int(r["age_s"])
            detail = {"command_id": cid, "command_type": r["command_type"], "target": target, "status": r["status"],
                      "outcome": r["command_outcome"], "age_s": age}
            await alert(conn, redis, "STUCK_COMMAND", stuck_command_message(r), detail, tier="CRITICAL",
                        dedupe_key=f"stuck-command:{cid}")
        alerted &= waiting
    if new:
        logger.error("%d command(s) waiting too long: %s", len(new), ", ".join(new))
    return new


# One statement: the approval is cancelled with the reason, and the operator is told (Telegram), once per approval
# (the dedupe key), whichever way it was approved: chat card, Mission Control or Telegram.
RECONCILE_APPROVALS_SQL = """
    WITH cancelled AS (
        UPDATE ai_approvals a
        SET status = 'CANCELLED', decided_via = 'system', decided_by = 'system', decided_at = now(),
            summary = COALESCE(a.summary, '') || ' · NOT EXECUTED: ' || c.status || COALESCE(' (' || c.error_message || ')', '')
        FROM execution_commands c
        WHERE a.command_id = c.command_id AND a.status = 'APPROVED' AND c.status IN ('REJECTED', 'FAILED')
        RETURNING a.id, a.summary, c.command_id
    ), told AS (
        INSERT INTO notifications_outbox (tier, kind, title, body, payload, dedupe_key)
        SELECT 'INFO', 'APPROVED_NOT_EXECUTED', 'Approved order was not executed',
               summary || '. No position was opened.',
               jsonb_build_object('approval_id', id::text, 'command_id', command_id::text),
               'not-executed:' || id::text
        FROM cancelled
        ON CONFLICT (dedupe_key) DO NOTHING
    )
    SELECT id FROM cancelled
"""


async def reconcile_approvals(pool) -> int:
    """An approved card whose paper command was refused or failed was not executed. Mark the approval CANCELLED with
    the reason in its summary, so it no longer reads as an executed order."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(RECONCILE_APPROVALS_SQL)
    if rows:
        logger.warning("Reconciled %d approved card(s) whose command was not executed", len(rows))
    return len(rows)


async def ops_monitor(engine, pool, redis, interval_s: float = OPS_EVERY_S) -> None:
    """Start-up publish, then every ``interval_s``: risk-limit overrides (F7) and betas (F4, hourly),
    effective limits and heartbeat alerts."""
    watch = HeartbeatWatch()
    alert_values = (HEARTBEAT_GRACE_S, SENTIMENT_STALE_S)
    stuck_alerted: set[str] = set()
    while True:
        alert_values = await load_alert_settings(pool, alert_values)  # ADR 0007: the stored grace and staleness
        watch.grace_s = alert_values[0]
        try:
            await reconcile_approvals(pool)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Approval reconcile failed: %s", e)
        await risk_overrides.refresh_risk_limits(engine, pool)  # logs and keeps what is applied on failure
        try:
            await risk_overrides.refresh_betas(pool)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Beta refresh failed: %s", e)
        try:
            await publish_effective_limits(engine, redis)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Could not publish %s: %s", EFFECTIVE_LIMITS_KEY, e)
        try:
            await check_heartbeats(pool, redis, watch, sentiment_stale_s=alert_values[1])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Heartbeat check failed: %s", e)
        try:
            await check_stuck_commands(pool, redis, stuck_alerted)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Stuck command check failed: %s", e)
        await asyncio.sleep(interval_s)
