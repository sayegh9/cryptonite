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
"""

import asyncio
import datetime
import json
import logging
import os
import time
from decimal import Decimal
from typing import Any

from cryptonite_schemas.mandate import ACCOUNT_BACKSTOP_DRAWDOWN_PCT

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


async def heartbeat_states(redis, now_s: float) -> dict[str, bool | None]:
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
        states["sentiment"] = (now_s - float(last_ok)) <= SENTIMENT_STALE_S if last_ok else None
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


async def check_heartbeats(pool, redis, watch: HeartbeatWatch, now_s: float | None = None) -> list[tuple[str, str]]:
    if redis is None:
        return []
    now_s = now_s if now_s is not None else time.time()
    events = watch.step(await heartbeat_states(redis, now_s), now_s)
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


async def ops_monitor(engine, pool, redis, interval_s: float = OPS_EVERY_S) -> None:
    """Start-up publish, then every ``interval_s``: risk-limit overrides (F7) and betas (F4, hourly),
    effective limits and heartbeat alerts."""
    watch = HeartbeatWatch()
    while True:
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
            await check_heartbeats(pool, redis, watch)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Heartbeat check failed: %s", e)
        await asyncio.sleep(interval_s)
