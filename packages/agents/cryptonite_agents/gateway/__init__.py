"""AI Gateway: routing, budget, permissions, fallback and tracing for every model call."""

from cryptonite_agents.gateway.budget import BudgetExceeded, BudgetGuard
from cryptonite_agents.gateway.gateway import (
    Gateway,
    GatewayError,
    GatewayResult,
    InvalidModelOutput,
    ModelUnavailable,
)
from cryptonite_agents.gateway.permission import CallContext, PermissionGate, ToolDenied
from cryptonite_agents.gateway.registry import RiskClass, ToolSpec, get_tool, registered_tools, tool
from cryptonite_agents.gateway.routes import (
    DEFAULT_ROUTES,
    BudgetPriority,
    FallbackPolicy,
    RoleRoute,
)

__all__ = [
    "DEFAULT_ROUTES",
    "BudgetExceeded",
    "BudgetGuard",
    "BudgetPriority",
    "CallContext",
    "FallbackPolicy",
    "Gateway",
    "GatewayError",
    "GatewayResult",
    "InvalidModelOutput",
    "ModelUnavailable",
    "PermissionGate",
    "RiskClass",
    "RoleRoute",
    "ToolDenied",
    "ToolSpec",
    "get_tool",
    "registered_tools",
    "tool",
]
