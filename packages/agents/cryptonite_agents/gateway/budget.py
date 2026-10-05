"""Daily LLM budget guard (blueprint D12, §4.1).

Spend comes from ``llm_cost_ledger`` (all services), refreshed every few seconds and topped up
locally between refreshes. Spend whose ledger write failed stays counted in this process for the
rest of the UTC day. As the day's spend approaches the cap, roles stop in priority order:

  >= 70%  one alert per UTC day (shared across processes via Redis); shadow calls (F2) skipped first
  >= 80%  OPTIONAL roles refused (research, reflection, digests)
  >= 90%  INTERACTIVE roles refused too (operator chat)
  >= 100% everything refused except CRITICAL roles (exits / risk reduction never stop)

Until the ledger has been read successfully today, spend is unknown: OPTIONAL and INTERACTIVE roles
are refused (debates and exits still run, counted locally). A role's own ``daily_cap_usd`` refuses
that role (unless CRITICAL) once reached.

Shadow calls (F2, ``check_shadow``) are never needed for a decision: they are skipped from the 70%
degrade threshold on, and whenever spend is unknown. Their cost is recorded like any other call
(under the role ``shadow:<role>``), so they count toward the daily cap.
"""

import datetime
import json
import logging
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from cryptonite_agents.gateway.routes import BudgetPriority, RoleRoute

logger = logging.getLogger("cryptonite.agents.gateway.budget")

DEFAULT_DAILY_CAP_USD = Decimal("25.00")
ALERT_CHANNEL = "channel:system_alerts"
ALERT_AT, OPTIONAL_STOP_AT, INTERACTIVE_STOP_AT = Decimal("0.70"), Decimal("0.80"), Decimal("0.90")
SHADOW_STOP_AT = ALERT_AT  # the degrade threshold: shadows are the first thing to go
_NON_CORE = (BudgetPriority.OPTIONAL, BudgetPriority.INTERACTIVE)


class BudgetExceeded(Exception):
    def __init__(self, role: str, reason: str) -> None:
        super().__init__(f"{role}: {reason}")
        self.role = role
        self.reason = reason


def parse_cap(value, default: Decimal) -> Decimal:
    """``system_settings.value`` may hold a bare number or a JSON string ("25.00")."""
    if value is None:
        return default
    try:
        if isinstance(value, str):
            value = json.loads(value) if value.strip().startswith('"') else value
        cap = Decimal(str(value))
        return cap if cap >= 0 else default
    except (InvalidOperation, ValueError, TypeError):
        logger.warning("Unparseable LLM_DAILY_BUDGET_USD %r; using %s", value, default)
        return default


class BudgetGuard:
    def __init__(
        self,
        pool=None,
        redis=None,
        default_cap_usd: Decimal = DEFAULT_DAILY_CAP_USD,
        refresh_s: float = 10.0,
        clock: Callable[[], float] = time.time,
        spend_unknown: bool = False,  # no ledger configured where one is expected: never trust local counts
    ) -> None:
        self.pool = pool
        self.redis = redis
        self.default_cap_usd = Decimal(default_cap_usd)
        self.refresh_s = refresh_s
        self.clock = clock
        self._day: datetime.date | None = None
        self._ledger_by_role: dict[str, Decimal] = {}
        self._pending_by_role: dict[str, Decimal] = {}  # recorded since the last refresh
        self._unpersisted_by_role: dict[str, Decimal] = {}  # ledger write failed: never in the DB
        self._cap = self.default_cap_usd
        self._refreshed_at = float("-inf")
        self._spend_unknown = spend_unknown
        self._known = pool is None and not spend_unknown  # without a pool, local counting is all there is
        self._alerted_day: datetime.date | None = None

    def _today(self) -> datetime.date:
        return datetime.datetime.fromtimestamp(self.clock(), datetime.UTC).date()

    @property
    def _by_role(self) -> dict[str, Decimal]:
        roles = set(self._ledger_by_role) | set(self._pending_by_role) | set(self._unpersisted_by_role)
        zero = Decimal("0")
        return {r: self._ledger_by_role.get(r, zero) + self._pending_by_role.get(r, zero)
                + self._unpersisted_by_role.get(r, zero) for r in roles}

    @property
    def _spent(self) -> Decimal:
        return sum(self._by_role.values(), Decimal("0"))

    async def _refresh(self) -> None:
        today = self._today()
        if today != self._day:  # new UTC day
            self._day = today
            self._ledger_by_role, self._pending_by_role, self._unpersisted_by_role = {}, {}, {}
            self._refreshed_at = float("-inf")
            self._known = self.pool is None and not self._spend_unknown
        if self.pool is None or self.clock() - self._refreshed_at < self.refresh_s:
            return
        self._refreshed_at = self.clock()  # also on failure: retry after refresh_s, not on every call
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT COALESCE(role, agent_name) AS role, SUM(total_cost_usd) AS spent
                    FROM llm_cost_ledger
                    WHERE created_at >= $1
                    GROUP BY 1
                    """,
                    datetime.datetime.combine(today, datetime.time(), datetime.UTC),
                )
                cap = await conn.fetchval("SELECT value FROM system_settings WHERE key = 'LLM_DAILY_BUDGET_USD'")
            self._ledger_by_role = {r["role"]: Decimal(str(r["spent"])) for r in rows}
            self._pending_by_role = {}  # now included in the ledger totals
            self._cap = parse_cap(cap, self.default_cap_usd)
            self._known = True
        except Exception as e:  # keep the last known numbers; never block exits on a telemetry read
            logger.warning("Budget refresh failed (spend known=%s, last %s): %s", self._known, self._spent, e)

    async def snapshot(self) -> dict[str, Decimal]:
        await self._refresh()
        return {"spent_usd": self._spent, "cap_usd": self._cap}

    async def check(self, route: RoleRoute) -> None:
        """Raise BudgetExceeded if ``route`` may not call a model now."""
        await self._refresh()
        if route.priority is BudgetPriority.CRITICAL:
            return
        if not self._known and route.priority in _NON_CORE:
            raise BudgetExceeded(route.role, "DAILY_SPEND_UNKNOWN")
        used = self._spent / self._cap if self._cap > 0 else Decimal("1")
        if used >= 1:
            raise BudgetExceeded(route.role, "DAILY_BUDGET_EXHAUSTED")
        if used >= INTERACTIVE_STOP_AT and route.priority in _NON_CORE:
            raise BudgetExceeded(route.role, "DAILY_BUDGET_RESERVED_FOR_DEBATES_AND_EXITS")
        if used >= OPTIONAL_STOP_AT and route.priority is BudgetPriority.OPTIONAL:
            raise BudgetExceeded(route.role, "DAILY_BUDGET_RESERVED_FOR_CORE_ROLES")
        if route.daily_cap_usd is not None and self._by_role.get(route.role, Decimal("0")) >= route.daily_cap_usd:
            raise BudgetExceeded(route.role, "ROLE_DAILY_CAP_REACHED")

    async def check_shadow(self, route: RoleRoute) -> None:
        """Raise BudgetExceeded if a shadow call for ``route`` may not run now (F2). Stricter than
        ``check``: no priority (not even CRITICAL) keeps a shadow running past the degrade threshold."""
        await self._refresh()
        if not self._known:
            raise BudgetExceeded(route.role, "SHADOW_SPEND_UNKNOWN")
        used = self._spent / self._cap if self._cap > 0 else Decimal("1")
        if used >= SHADOW_STOP_AT:
            raise BudgetExceeded(route.role, "SHADOW_BUDGET_DEGRADED")

    async def record(self, role: str, cost_usd: Decimal, persisted: bool = True) -> None:
        """Count a call's cost now. ``persisted=False`` (its ledger write failed) keeps it counted all day."""
        await self._refresh()
        if cost_usd <= 0:
            return
        target = self._pending_by_role if persisted and self.pool is not None else self._unpersisted_by_role
        target[role] = target.get(role, Decimal("0")) + cost_usd
        spent = self._spent
        if self._cap > 0 and spent / self._cap >= ALERT_AT and self._alerted_day != self._day:
            self._alerted_day = self._day
            await self._alert(spent)

    async def _alert(self, spent: Decimal) -> None:
        message = f"LLM spend ${spent:.2f} reached {spent / self._cap:.0%} of the ${self._cap:.2f} daily cap"
        if self.redis is None:
            logger.warning(message)
            return
        try:
            first = await self.redis.set(f"llm_budget_alert:{self._day}", "1", nx=True, ex=26 * 3600)
            if first:
                logger.warning(message)
                await self.redis.publish(ALERT_CHANNEL, json.dumps({
                    "type": "LLM_BUDGET_WARNING", "severity": "WARNING", "message": message,
                    "spent_usd": str(spent), "cap_usd": str(self._cap),
                }))
        except Exception as e:
            logger.warning("Budget alert publish failed: %s", e)
