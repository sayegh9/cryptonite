"""Role → model routing (AI-first blueprint §4.1).

Every model call names a *role*, never a model. The route decides the model, its fallback, how a
failure is handled, output/thinking limits and budget priority. Defaults live here; rows in
``ai_model_routes`` override them per role without a deploy.
"""

import logging
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any

logger = logging.getLogger("cryptonite.agents.gateway.routes")


class FallbackPolicy(StrEnum):
    FAIL_CLOSED = "FAIL_CLOSED"  # no answer rather than a weaker model (trade decisions)
    DEGRADE = "DEGRADE"  # fall back to ``fallback_model`` (triage, summaries, embeddings)


class BudgetPriority(StrEnum):
    """Order in which roles stop as the daily budget runs out (first to last)."""

    OPTIONAL = "optional"  # research loops, reflection, digests
    INTERACTIVE = "interactive"  # operator chat
    DEBATE = "debate"  # entry debates and triage
    CRITICAL = "critical"  # exits / risk reduction: never blocked by budget


FLASH_LITE = "google:gemini-2.5-flash-lite"
FLASH = "google:gemini-2.5-flash"
PRO = "google:gemini-2.5-pro"
EMBED = "google:text-embedding-004"  # existing reflections.embedding vectors come from this model


@dataclass(frozen=True)
class RoleRoute:
    role: str
    primary_model: str
    fallback_model: str | None = None
    fallback_policy: FallbackPolicy = FallbackPolicy.FAIL_CLOSED
    priority: BudgetPriority = BudgetPriority.INTERACTIVE
    max_output_tokens: int | None = None
    thinking_budget: int | None = None  # None = provider default; 0 disables thinking where allowed
    daily_cap_usd: Decimal | None = None
    certified_primary: bool = False  # set once an eval run certifies primary_model for this role
    timeout_s: float = 90.0  # per model request; a hung call must not stall the worker


_O, _I, _D, _C = BudgetPriority.OPTIONAL, BudgetPriority.INTERACTIVE, BudgetPriority.DEBATE, BudgetPriority.CRITICAL
_DEGRADE = FallbackPolicy.DEGRADE

DEFAULT_ROUTES: dict[str, RoleRoute] = {
    r.role: r
    for r in [
        # Operator chat personas (Phase 3 folds them into the Brain)
        RoleRoute("chat.research", FLASH, priority=_I),
        RoleRoute("chat.explain", FLASH, priority=_I),
        RoleRoute("chat.strategist", FLASH, priority=_I),
        RoleRoute("chat.onchain", FLASH, priority=_I),
        RoleRoute("chat.cro", PRO, priority=_I),
        RoleRoute("chat.opportunity", FLASH, priority=_I),
        # Strategy research
        RoleRoute("strategy.draft", FLASH, priority=_O),
        RoleRoute("strategy.review", PRO, priority=_O),
        # Entry debate
        RoleRoute("triage", FLASH_LITE, FLASH, _DEGRADE, _D, max_output_tokens=512, thinking_budget=0),
        # Thinking is billed as output: side roles get small budgets, verdicts keep room to reason (A2.5)
        RoleRoute("debate.lite", FLASH, priority=_D, max_output_tokens=3000, thinking_budget=1024),
        RoleRoute("debate.analyst", FLASH, priority=_D, max_output_tokens=1500, thinking_budget=512),
        RoleRoute("debate.side", FLASH, priority=_D, max_output_tokens=1500, thinking_budget=512),
        RoleRoute("debate.risk", FLASH, priority=_D, max_output_tokens=1200, thinking_budget=512),
        RoleRoute("debate.verdict", PRO, priority=_D, max_output_tokens=4000, thinking_budget=2048),
        RoleRoute("debate.cro", PRO, priority=_D, max_output_tokens=4000, thinking_budget=2048),
        # Exit debate: risk reduction, never stopped by budget
        RoleRoute("exit.side", FLASH, priority=_C, max_output_tokens=1500, thinking_budget=512),
        RoleRoute("exit.verdict", PRO, priority=_C, max_output_tokens=4000, thinking_budget=2048),
        # Learning and housekeeping
        RoleRoute("reflection", FLASH, priority=_O),
        RoleRoute("summarize", FLASH_LITE, FLASH, _DEGRADE, _O, thinking_budget=0),
        RoleRoute("embed", EMBED, fallback_policy=_DEGRADE, priority=_D),
    ]
}


# Trade decisions: a database row may never weaken these routes (fallback, budget priority), and may
# only change their model to one an eval certified (certified_primary = true on the row).
PROTECTED_ROLES = frozenset({"debate.verdict", "debate.cro", "exit.verdict"})


def normalize_model_id(model: str) -> str:
    """``gemini-2.5-flash`` (agent_configs style) → ``google:gemini-2.5-flash``."""
    return model if ":" in model else f"google:{model}"


def route_from_row(row: dict[str, Any], base: RoleRoute | None) -> RoleRoute:
    """Apply an ``ai_model_routes`` row over the default route (NULL columns keep the default)."""
    base = base or RoleRoute(row["role"], normalize_model_id(row["primary_model"]))
    fields = {
        "primary_model": normalize_model_id(row["primary_model"]) if row.get("primary_model") else None,
        "fallback_model": normalize_model_id(row["fallback_model"]) if row.get("fallback_model") else None,
        "fallback_policy": FallbackPolicy(row["fallback_policy"]) if row.get("fallback_policy") else None,
        "priority": BudgetPriority(row["priority"]) if row.get("priority") else None,
        "max_output_tokens": row.get("max_output_tokens"),
        "thinking_budget": row.get("thinking_budget"),
        "daily_cap_usd": Decimal(str(row["daily_cap_usd"])) if row.get("daily_cap_usd") is not None else None,
        "certified_primary": row.get("certified_primary"),
    }
    changes = {k: v for k, v in fields.items() if v is not None}
    if base.role in PROTECTED_ROLES:
        ignored = {k for k in changes if k in ("fallback_model", "fallback_policy", "priority")}
        if changes.get("primary_model") and not row.get("certified_primary"):
            ignored.add("primary_model")
        if ignored:
            logger.warning("Ignoring %s for protected role %s", sorted(ignored), base.role)
        changes = {k: v for k, v in changes.items() if k not in ignored}
    return replace(base, **changes)
