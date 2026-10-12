"""Per-role model routing (ADR 0007, Phase 2): what an operator may change on a role's route, and what may not change.

The gateway reads ``ai_model_routes`` rows over its code defaults (``ROUTES_TTL_S``). This module decides which fields
an operator may set, which models may be set, and which roles are locked. The protected trade roles stay locked:
their fallback and certified primary are the gateway's rule, not an option here. Local (Ollama) models are allowed only
for the roles ADR 0005 moves local: triage, summarize and the embedder.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from cryptonite_agents.gateway.pricing import PRICES
from cryptonite_agents.gateway.routes import PROTECTED_ROLES, RoleRoute, route_from_row

# The fields a route row holds that the editor may set. priority, certified_primary and the shadow fields stay out.
EDITABLE = ("primary_model", "fallback_model", "fallback_policy", "daily_cap_usd", "max_output_tokens", "thinking_budget")
LOCAL_ROLES = frozenset({"triage", "summarize", "embed"})  # ADR 0005: the cheap, checkable roles and the embedder
RETIRED_MODELS = frozenset({"google:text-embedding-004"})  # retired by Google (ADR 0005)
FALLBACK_POLICIES = ("FAIL_CLOSED", "DEGRADE")
LIMITS: dict[str, tuple[Decimal, Decimal]] = {
    "max_output_tokens": (Decimal("1"), Decimal("65536")),
    "thinking_budget": (Decimal("0"), Decimal("32768")),
    "daily_cap_usd": (Decimal("0"), Decimal("500")),
}
_OLLAMA = re.compile(r"^ollama:[a-z0-9][a-z0-9._:/-]{0,79}$")


def known_model(model: str) -> bool:
    """A model the gateway can call: a priced Gemini model, or a local Ollama tag. Retired models are refused."""
    if model in RETIRED_MODELS:
        return False
    return model in PRICES or bool(_OLLAMA.match(model))


def route_violations(role: str, values: dict[str, Any]) -> list[str]:
    """Every reason the requested values may not be stored for ``role``. Empty when they may."""
    found: list[str] = []
    for field in ("primary_model", "fallback_model"):
        model = values.get(field)
        if model is None:
            continue
        if not known_model(model):
            found.append(f"{field}: {model} is not a model the gateway can call (unknown or retired)")
        elif model.startswith("ollama:") and role not in LOCAL_ROLES:
            found.append(f"{field}: local models are only for {', '.join(sorted(LOCAL_ROLES))} (ADR 0005)")
    policy = values.get("fallback_policy")
    if policy is not None and policy not in FALLBACK_POLICIES:
        found.append(f"fallback_policy must be one of {', '.join(FALLBACK_POLICIES)}")
    for field, (low, high) in LIMITS.items():
        value = values.get(field)
        if value is not None and not low <= Decimal(str(value)) <= high:
            found.append(f"{field} must be between {low} and {high}")
    return found


def same_value(a: Any, b: Any) -> bool:
    """Equal as the route stores them: numbers by value (5 and 5.0000 are the same), text exactly."""
    if a is None or b is None:
        return a is None and b is None
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except InvalidOperation:
        return a == b


def _fields(route: RoleRoute) -> dict[str, Any]:
    return {
        "primary_model": route.primary_model,
        "fallback_model": route.fallback_model,
        "fallback_policy": route.fallback_policy.value,
        "daily_cap_usd": None if route.daily_cap_usd is None else str(route.daily_cap_usd),
        "max_output_tokens": route.max_output_tokens,
        "thinking_budget": route.thinking_budget,
    }


def route_view(role: str, base: RoleRoute, row: dict[str, Any] | None) -> dict[str, Any]:
    """One role as the editor shows it: the code default, what the gateway applies now, and what is stored."""
    effective = route_from_row(row, base) if row else base
    stored = None
    if row is not None:
        stored = {field: (None if row.get(field) is None else str(row[field])) for field in EDITABLE}
    return {
        "role": role,
        "protected": role in PROTECTED_ROLES,
        "editable": role not in PROTECTED_ROLES,
        "local_allowed": role in LOCAL_ROLES,
        "default": _fields(base),
        "effective": _fields(effective),
        "stored": stored,
        "overridden": stored is not None and any(v is not None for v in stored.values()),
    }
