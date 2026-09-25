"""The AI Gateway: the only door to any model (AI-first blueprint §4.1).

``await gateway.run("debate.verdict", prompt, output_type=PortfolioDecision)``:
  1. resolve the role's route (defaults, overridden by ``ai_model_routes``)
  2. budget check (critical roles always pass)
  3. call the primary model; on provider failure try the fallback only for DEGRADE roles —
     FAIL_CLOSED roles (trade decisions) raise instead of silently using a weaker model
  4. every tool call passes the permission gate
  5. cost the call, count it against the budget, write ledger + decision trace
"""

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from cryptonite_agents.gateway import pricing
from cryptonite_agents.gateway.budget import BudgetExceeded, BudgetGuard
from cryptonite_agents.gateway.ledger import CallRecord, write_call
from cryptonite_agents.gateway.permission import CallContext, PermissionGate, ToolDenied
from cryptonite_agents.gateway.providers import pydantic_ai_backend as backend
from cryptonite_agents.gateway.registry import ToolSpec
from cryptonite_agents.gateway.routes import (
    DEFAULT_ROUTES,
    FallbackPolicy,
    RoleRoute,
    normalize_model_id,
    route_from_row,
)

logger = logging.getLogger("cryptonite.agents.gateway")

ROUTES_TTL_S = 30.0


class GatewayError(Exception):
    """Base class for gateway failures callers may want to handle as one."""


class ModelUnavailable(GatewayError):
    def __init__(self, role: str, detail: str) -> None:
        super().__init__(f"{role}: no model available ({detail})")
        self.role = role


class InvalidModelOutput(GatewayError):
    def __init__(self, role: str, detail: str) -> None:
        super().__init__(f"{role}: model output invalid ({detail})")
        self.role = role


@dataclass
class GatewayResult:
    output: Any
    role: str
    model: str
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    latency_ms: int
    trace_id: uuid.UUID
    fell_back: bool = False
    tool_calls: list[dict[str, Any]] | None = None
    messages: list[Any] | None = None


class Gateway:
    def __init__(
        self,
        pool=None,
        redis=None,
        routes: dict[str, RoleRoute] | None = None,
        budget: BudgetGuard | None = None,
        permission: PermissionGate | None = None,
    ) -> None:
        self.pool = pool
        self.budget = budget or BudgetGuard(pool=pool, redis=redis)
        self.permission = permission or PermissionGate()
        self._defaults = dict(routes or DEFAULT_ROUTES)
        self._routes = dict(self._defaults)
        self._routes_loaded_at = float("-inf")

    async def route(self, role: str) -> RoleRoute:
        await self._load_routes()
        if role not in self._routes:
            raise GatewayError(f"unknown role {role!r}")
        return self._routes[role]

    async def _load_routes(self) -> None:
        if self.pool is None or time.monotonic() - self._routes_loaded_at < ROUTES_TTL_S:
            return
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM ai_model_routes")
            routes = dict(self._defaults)
            for row in rows:
                routes[row["role"]] = route_from_row(dict(row), self._defaults.get(row["role"]))
            self._routes = routes
        except Exception as e:
            logger.warning("Could not load ai_model_routes, using defaults: %s", e)
        self._routes_loaded_at = time.monotonic()

    async def run(
        self,
        role: str,
        prompt: str,
        *,
        instructions: str | None = None,
        output_type: Any = str,
        tools: Sequence[ToolSpec] = (),
        model_override: str | None = None,
        temperature: float | None = None,
        message_history: Sequence[Any] | None = None,
        context: CallContext | None = None,
        parent_trace_id: uuid.UUID | None = None,
    ) -> GatewayResult:
        route = await self.route(role)
        if model_override:
            route = replace(route, primary_model=normalize_model_id(model_override))
        ctx = context or CallContext(role=role)

        try:
            await self.budget.check(route)
        except BudgetExceeded as e:
            await write_call(self.pool, CallRecord(role, route.primary_model, "BUDGET_BLOCKED", error=e.reason,
                                                   parent_trace_id=parent_trace_id, session_id=ctx.session_id))
            raise

        def guard(spec: ToolSpec) -> str | None:
            try:
                self.permission.check(spec, ctx)
                return None
            except ToolDenied as denied:
                logger.warning("Tool call denied for %s: %s", role, denied)
                return f"Tool call denied: {denied.reason}. Ask the operator to approve this action."

        candidates = [route.primary_model]
        # An explicitly chosen model (operator override, eval) is pinned: falling back would silently
        # answer with a different model than the one asked for
        if route.fallback_policy is FallbackPolicy.DEGRADE and route.fallback_model and not model_override:
            candidates.append(route.fallback_model)

        last_error = "no candidates"
        for attempt, model in enumerate(candidates):
            started = time.monotonic()
            try:
                res = await backend.run_model(
                    model, prompt, instructions=instructions, output_type=output_type, tools=tools,
                    tool_guard=guard, max_output_tokens=route.max_output_tokens,
                    thinking_budget=route.thinking_budget, temperature=temperature,
                    message_history=message_history, timeout_s=route.timeout_s,
                )
            except backend.BackendError as e:
                latency = int((time.monotonic() - started) * 1000)
                last_error = str(e)
                u = e.usage  # failed runs still cost what the provider billed (incl. output retries)
                cost = pricing.cost_usd(model, u.input_tokens, u.output_tokens, u.cached_input_tokens)
                ok = await write_call(self.pool, CallRecord(
                    role, model, "ERROR", u.input_tokens, u.output_tokens, u.cached_input_tokens, u.thinking_tokens,
                    cost, latency, error=last_error, parent_trace_id=parent_trace_id, session_id=ctx.session_id))
                await self.budget.record(role, cost, persisted=ok)
                logger.warning("Gateway %s attempt %d on %s failed: %s", role, attempt + 1, model, e)
                if isinstance(e, backend.OutputInvalid) and route.fallback_policy is FallbackPolicy.FAIL_CLOSED:
                    raise InvalidModelOutput(role, last_error) from e
                continue

            latency = int((time.monotonic() - started) * 1000)
            cost = pricing.cost_usd(res.model, res.input_tokens, res.output_tokens, res.cached_input_tokens)
            rec = CallRecord(
                role, res.model, "OK", res.input_tokens, res.output_tokens, res.cached_input_tokens,
                res.thinking_tokens, cost, latency, parent_trace_id=parent_trace_id, session_id=ctx.session_id,
                mandate_id=ctx.mandate_id, tool_calls=res.tool_calls, output=res.output,
            )
            ok = await write_call(self.pool, rec)
            await self.budget.record(role, cost, persisted=ok)
            return GatewayResult(
                output=res.output, role=role, model=res.model, cost_usd=cost, input_tokens=res.input_tokens,
                output_tokens=res.output_tokens, thinking_tokens=res.thinking_tokens, latency_ms=latency,
                trace_id=rec.trace_id, fell_back=attempt > 0, tool_calls=res.tool_calls, messages=res.messages,
            )

        raise ModelUnavailable(role, last_error)

    async def embed(self, texts: Sequence[str], role: str = "embed") -> list[list[float]]:
        route = await self.route(role)
        await self.budget.check(route)
        started = time.monotonic()
        try:
            vectors, tokens = await backend.embed(route.primary_model, texts)
        except backend.ProviderUnavailable as e:
            await write_call(self.pool, CallRecord(role, route.primary_model, "ERROR", error=str(e)))
            raise ModelUnavailable(role, str(e)) from e
        cost = pricing.cost_usd(route.primary_model, tokens, 0)
        ok = await write_call(self.pool, CallRecord(role, route.primary_model, "OK", input_tokens=tokens, cost_usd=cost,
                                                    latency_ms=int((time.monotonic() - started) * 1000)))
        await self.budget.record(role, cost, persisted=ok)
        return vectors
