"""Brain roles (blueprint §4.1, A3.2).

Kept here instead of ``gateway/routes.py`` so the Brain can land without touching the gateway's
route table; callers build the gateway with ``routes={**DEFAULT_ROUTES, **BRAIN_ROUTES}``.
Rows in ``ai_model_routes`` still override these per role.
"""

from cryptonite_agents.gateway.routes import (
    FLASH,
    FLASH_LITE,
    PRO,
    BudgetPriority,
    FallbackPolicy,
    RoleRoute,
)

BRAIN_PLAN = "brain.plan"  # the planner: one conversation, tools, may propose orders
BRAIN_CHAT = "brain.chat"  # lighter chat turns (budget degrade target)

BRAIN_ROUTES: dict[str, RoleRoute] = {
    r.role: r
    for r in [
        # brain.plan can propose an order, so it fails closed: no silent downgrade to a weaker model. The caps bound
        # one turn's cost (thinking is billed as output, as for debate.verdict)
        RoleRoute(BRAIN_PLAN, PRO, fallback_policy=FallbackPolicy.FAIL_CLOSED, priority=BudgetPriority.INTERACTIVE,
                  max_output_tokens=8000, thinking_budget=4096),
        # brain.chat also answers the degraded read-only turn, so it is capped the same way (smaller: no order plans)
        RoleRoute(BRAIN_CHAT, FLASH, FLASH_LITE, FallbackPolicy.DEGRADE, BudgetPriority.INTERACTIVE,
                  max_output_tokens=4000, thinking_budget=1024),
    ]
}


def brain_gateway_routes() -> dict[str, RoleRoute]:
    """The default gateway routes plus the Brain's."""
    from cryptonite_agents.gateway.routes import DEFAULT_ROUTES

    return {**DEFAULT_ROUTES, **BRAIN_ROUTES}
