"""The AI Gateway: the only door to any model (AI-first blueprint §4.1).

``await gateway.run("debate.verdict", prompt, output_type=PortfolioDecision)``:
  1. resolve the role's route (defaults, overridden by ``ai_model_routes``)
  2. budget check (critical roles always pass)
  3. call the primary model; on provider failure try the fallback only for DEGRADE roles —
     FAIL_CLOSED roles (trade decisions) raise instead of silently using a weaker model
  4. every tool call passes the permission gate
  5. cost the call, count it against the budget, write ledger + decision trace
  6. shadow mode (F2): a sampled call is replayed on the route's shadow model in the background,
     logged to ``ai_shadow_results`` and never acted on (``shadow.py``)
"""

import asyncio
import logging
import random
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from cryptonite_agents.gateway import pricing
from cryptonite_agents.gateway import shadow as sh
from cryptonite_agents.gateway.budget import BudgetExceeded, BudgetGuard
from cryptonite_agents.gateway.ledger import CallRecord, write_call
from cryptonite_agents.gateway.local_tier import LocalTierHealth, is_local
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
# Small, distillable roles keep their prompt in the trace (A7.4); their inputs are briefs, headlines
# and chat summaries the platform already stores. Other roles never log prompts.
LOG_INPUT_ROLES = frozenset({"triage", "tagging", "summarize"})
MAX_LOGGED_INPUT = 20_000
# Tools that start paid or external work, capped per model turn (LE-8). propose_order is capped at one order
# card per turn: the first call is held for approval, any later one is refused.
PER_TURN_TOOL_LIMITS = {"stage_debate_proposal": 1, "propose_order": 1}
# ADD_RISK tools whose denial is the normal way their result is made: the call is held and staged for the
# operator's approval, so the model gets a "held" answer (not an error) and the per-turn limit applies to it
HELD_FOR_APPROVAL_TOOLS = frozenset({"propose_order"})
# The start of the answer a held call gets. The Brain renders a card only for a tool result that starts with it.
HELD_FOR_APPROVAL_PREFIX = "Held for approval"


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
        local_tier: LocalTierHealth | None = None,
        allow_paid_shadow: bool | None = None,
        shadow_rng: random.Random | None = None,
    ) -> None:
        self.pool = pool
        self.local_tier = local_tier or LocalTierHealth(redis=redis)
        self.budget = budget or BudgetGuard(pool=pool, redis=redis)
        self.permission = permission or PermissionGate()
        self._defaults = dict(routes or DEFAULT_ROUTES)
        self._routes = dict(self._defaults)
        self._routes_loaded_at = float("-inf")
        # Paid shadow models need an explicit opt-in: this argument, else system_settings
        # AI_SHADOW_ALLOW_PAID (read with the routes). Local (Ollama) shadows are free and always allowed.
        self._allow_paid_shadow_arg = allow_paid_shadow
        self._allow_paid_shadow = bool(allow_paid_shadow)
        self._shadow_rng = shadow_rng or random.Random()
        self._shadow_tasks: set[asyncio.Task] = set()

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
                if self._allow_paid_shadow_arg is None:  # same connection: no extra acquire on the call path
                    self._allow_paid_shadow = await self._read_allow_paid(conn)
            routes = dict(self._defaults)
            for row in rows:
                routes[row["role"]] = route_from_row(dict(row), self._defaults.get(row["role"]))
            self._routes = routes
        except Exception as e:
            logger.warning("Could not load ai_model_routes, using defaults: %s", e)
        self._routes_loaded_at = time.monotonic()

    @staticmethod
    async def _read_allow_paid(conn) -> bool:
        try:
            return _truthy(await conn.fetchval("SELECT value FROM system_settings WHERE key = 'AI_SHADOW_ALLOW_PAID'"))
        except Exception as e:  # unknown means no paid shadows
            logger.debug("Could not read AI_SHADOW_ALLOW_PAID: %s", e)
            return False

    # ------------------------------------------------------------------ shadow mode (F2)

    def _maybe_shadow(self, route: RoleRoute, *, prompt: str, instructions: str | None, output_type: Any,
                      tools: Sequence[ToolSpec], temperature: float | None, message_history: Sequence[Any] | None,
                      model_override: str | None, result: "GatewayResult",
                      parent_trace_id: uuid.UUID | None) -> None:
        """Sample this call for the route's shadow and schedule it. Synchronous and cheap: the primary's
        caller gets its result without waiting for, or ever seeing, anything the shadow does."""
        try:
            if (not route.shadow_model or route.shadow_sample_rate <= 0 or model_override or self.pool is None
                    or route.shadow_model == result.model):
                return  # pinned calls (evals, operator overrides) and pool-less gateways are not shadowed
            if self._shadow_rng.random() >= route.shadow_sample_rate:
                return
            rec = sh.ShadowRecord(
                role=route.role, primary_model=result.model, shadow_model=route.shadow_model, status="SKIPPED",
                primary_trace_id=result.trace_id, parent_trace_id=parent_trace_id,
                primary_output=sh.jsonable(result.output), primary_latency_ms=result.latency_ms,
                primary_cost_usd=result.cost_usd,
            )
            if len(self._shadow_tasks) >= sh.MAX_SHADOW_INFLIGHT:
                rec.error = "SHADOW_CONCURRENCY_LIMIT"
                coro = sh.write_shadow(self.pool, rec)
            else:
                coro = self._run_shadow(route, rec, prompt=prompt, instructions=instructions, output_type=output_type,
                                        tools=sh.shadow_tools(tools), temperature=temperature,
                                        message_history=list(message_history) if message_history else None)
            task = asyncio.get_running_loop().create_task(coro)
            self._shadow_tasks.add(task)
            task.add_done_callback(self._shadow_tasks.discard)
        except Exception as e:  # never let the shadow touch the primary path
            logger.warning("Shadow scheduling failed for %s: %s", route.role, e)

    async def _run_shadow(self, route: RoleRoute, rec: "sh.ShadowRecord", *, prompt: str, instructions: str | None,
                          output_type: Any, tools: tuple[ToolSpec, ...], temperature: float | None,
                          message_history: list[Any] | None) -> None:
        model = rec.shadow_model
        try:
            if not pricing.is_free(model) and not self._allow_paid_shadow:
                rec.error = "PAID_SHADOW_NOT_ALLOWED"
                return
            try:
                await self.budget.check_shadow(route)
            except BudgetExceeded as e:
                rec.error = e.reason
                return
            if is_local(model) and await self.local_tier.is_down(model):
                rec.error = "LOCAL_TIER_DOWN"
                return
            started = time.monotonic()
            try:
                res = await backend.run_model(
                    model, prompt, instructions=instructions, output_type=output_type, tools=tools,
                    tool_guard=sh.shadow_guard, max_output_tokens=route.max_output_tokens,
                    thinking_budget=route.thinking_budget, temperature=temperature,
                    message_history=message_history, timeout_s=route.timeout_s,
                )
            except backend.BackendError as e:
                u = e.usage
                rec.status, rec.error = "ERROR", str(e)
                rec.input_tokens, rec.output_tokens = u.input_tokens, u.output_tokens
                rec.cached_input_tokens, rec.thinking_tokens = u.cached_input_tokens, u.thinking_tokens
            else:
                rec.status, rec.shadow_output = "OK", sh.jsonable(res.output)
                rec.input_tokens, rec.output_tokens = res.input_tokens, res.output_tokens
                rec.cached_input_tokens, rec.thinking_tokens = res.cached_input_tokens, res.thinking_tokens
                rec.key_field, rec.agrees = sh.agreement(route.role, rec.primary_output, rec.shadow_output)
            rec.shadow_latency_ms = int((time.monotonic() - started) * 1000)
            rec.shadow_cost_usd = pricing.cost_usd(model, rec.input_tokens, rec.output_tokens, rec.cached_input_tokens)
        except Exception as e:  # anything else is the shadow's own error, logged on its row
            rec.status, rec.error = "ERROR", f"{type(e).__name__}: {e}"
        finally:
            ok = await sh.write_shadow(self.pool, rec)
            if rec.status != "SKIPPED":
                try:
                    await self.budget.record(sh.shadow_role(route.role), rec.shadow_cost_usd, persisted=ok)
                except Exception as e:
                    logger.warning("Shadow budget record failed for %s: %s", route.role, e)

    async def drain_shadows(self, timeout_s: float | None = None) -> None:
        """Wait for scheduled shadow calls (tests, graceful shutdown)."""
        if self._shadow_tasks:
            await asyncio.wait(set(self._shadow_tasks), timeout=timeout_s)

    def _guard(self, role: str, ctx: CallContext):
        """Per-call tool gate: the permission check, plus per-turn caps on tools that start paid work
        (LE-8: one model turn must not queue a burst of debates)."""
        used: dict[str, int] = {}
        # pydantic-ai runs sync tools in worker threads, so two calls of one turn can race here: the read and the
        # increment of a count must be one step, or both calls could take the same slot
        lock = threading.Lock()

        def guard(spec: ToolSpec) -> str | None:
            try:
                self.permission.check(spec, ctx)
            except ToolDenied as denied:
                logger.warning("Tool call denied for %s: %s", role, denied)
                if denied.reason == "ADD_RISK_REQUIRES_APPROVAL" and spec.name in HELD_FOR_APPROVAL_TOOLS:
                    with lock:
                        used[spec.name] = used.get(spec.name, 0) + 1
                        held = used[spec.name] <= PER_TURN_TOOL_LIMITS[spec.name]
                    if held:
                        return (f"{HELD_FOR_APPROVAL_PREFIX}: the order card is staged and shown to the operator with "
                                f"Approve / Reject. This is the expected result, not an error. Do not call "
                                f"{spec.name} again in this turn; tell the operator the card is waiting for their "
                                "approval. Do not call any more tools in this turn; provide your summary to the operator now.")
                    return (f"Refused: only one order card per turn, and the first card is already waiting for the "
                            f"operator. Do not call {spec.name} again; tell the operator the card is waiting.")
                return f"Tool call denied: {denied.reason}. Ask the operator to approve this action."
            limit = PER_TURN_TOOL_LIMITS.get(spec.name)
            if limit is not None:
                with lock:
                    allowed = used.get(spec.name, 0) < limit
                    if allowed:
                        used[spec.name] = used.get(spec.name, 0) + 1
                if not allowed:
                    logger.warning("Tool call denied for %s: %s over its per-turn limit", role, spec.name)
                    return (f"Tool call denied: {spec.name} is limited to {limit} per turn. "
                            "Tell the operator what else you would stage instead of calling it again.")
            return None

        return guard

    async def _candidates(self, route: RoleRoute, model_override: str | None) -> list[str]:
        candidates = [route.primary_model]
        # An explicitly chosen model (operator override, eval) is pinned: falling back would silently
        # answer with a different model than the one asked for
        if route.fallback_policy is FallbackPolicy.DEGRADE and route.fallback_model and not model_override:
            candidates.append(route.fallback_model)
        # A local primary that is down (or not pulled) is skipped, not timed out on every call (A6.1). Only
        # DEGRADE routes have a second candidate, so FAIL_CLOSED roles and pinned models never switch.
        if len(candidates) > 1 and is_local(candidates[0]) and await self.local_tier.is_down(candidates[0]):
            logger.info("Local tier unavailable: %s skips %s", route.role, candidates[0])
            candidates = candidates[1:]
        return candidates

    async def _note_local(self, model: str, error: Exception | None) -> None:
        if not is_local(model):
            return
        if error is None:
            await self.local_tier.mark_up()
        elif isinstance(error, backend.ProviderUnavailable):  # not OutputInvalid: a bad answer is no outage
            await self.local_tier.on_call_failure(model, error)

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

        guard = self._guard(role, ctx)

        candidates = await self._candidates(route, model_override)

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
                    cost, latency, error=last_error, parent_trace_id=parent_trace_id, session_id=ctx.session_id,
                    mandate_id=ctx.mandate_id))
                await self.budget.record(role, cost, persisted=ok)
                logger.warning("Gateway %s attempt %d on %s failed: %s", role, attempt + 1, model, e)
                await self._note_local(model, e)
                if isinstance(e, backend.OutputInvalid) and route.fallback_policy is FallbackPolicy.FAIL_CLOSED:
                    raise InvalidModelOutput(role, last_error) from e
                continue

            await self._note_local(model, None)
            latency = int((time.monotonic() - started) * 1000)
            cost = pricing.cost_usd(res.model, res.input_tokens, res.output_tokens, res.cached_input_tokens)
            rec = CallRecord(
                role, res.model, "OK", res.input_tokens, res.output_tokens, res.cached_input_tokens,
                res.thinking_tokens, cost, latency, parent_trace_id=parent_trace_id, session_id=ctx.session_id,
                mandate_id=ctx.mandate_id, tool_calls=res.tool_calls, output=res.output,
                input=prompt[:MAX_LOGGED_INPUT] if role in LOG_INPUT_ROLES else None,
            )
            ok = await write_call(self.pool, rec)
            await self.budget.record(role, cost, persisted=ok)
            result = GatewayResult(
                output=res.output, role=role, model=res.model, cost_usd=cost, input_tokens=res.input_tokens,
                output_tokens=res.output_tokens, thinking_tokens=res.thinking_tokens, latency_ms=latency,
                trace_id=rec.trace_id, fell_back=model != route.primary_model, tool_calls=res.tool_calls, messages=res.messages,
            )
            self._maybe_shadow(route, prompt=prompt, instructions=instructions, output_type=output_type, tools=tools,
                               temperature=temperature, message_history=message_history,
                               model_override=model_override, result=result, parent_trace_id=parent_trace_id)
            return result

        raise ModelUnavailable(role, last_error)

    async def embed(self, texts: Sequence[str], role: str = "embed", kind: str = "document") -> list[list[float]]:
        return (await self.embed_tagged(texts, role=role, kind=kind))[0]

    async def embed_tagged(self, texts: Sequence[str], role: str = "embed", kind: str = "document",
                           model_override: str | None = None) -> tuple[list[list[float]], str]:
        """Vectors plus the model that produced them. Vectors from different models are not comparable,
        so callers store and filter by that model, and ``embed`` never falls back to another one: a
        down local tier fails fast here, and retrieval degrades to recency instead (A6.4)."""
        route = await self.route(role)
        model = normalize_model_id(model_override) if model_override else route.primary_model
        await self.budget.check(route)
        if is_local(model) and await self.local_tier.is_down(model):
            raise ModelUnavailable(role, f"local tier unavailable for {model}")
        started = time.monotonic()
        try:
            vectors, tokens = await backend.embed(model, texts, kind)
        except backend.ProviderUnavailable as e:
            await write_call(self.pool, CallRecord(role, model, "ERROR", error=str(e)))
            await self._note_local(model, e)
            raise ModelUnavailable(role, str(e)) from e
        cost = pricing.cost_usd(model, tokens, 0)
        ok = await write_call(self.pool, CallRecord(role, model, "OK", input_tokens=tokens, cost_usd=cost,
                                                    latency_ms=int((time.monotonic() - started) * 1000)))
        await self.budget.record(role, cost, persisted=ok)
        return vectors, model

    async def stream(
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
    ) -> "AsyncIterator[GatewayStreamEvent]":
        """``run`` as a stream (blueprint A3.3): same route, budget check, permission gate and ledger.

        Yields ``start`` (carries the trace id the ledger row will use), then ``token`` / ``tool_call`` /
        ``tool_result`` events, then ``done`` with the ``GatewayResult``. Fallback applies only before
        the first event reached the caller (a half-streamed answer is never continued by another
        model). Closing the stream early cancels the model call and writes a CANCELLED ledger row
        costed with what the provider billed so far.
        """
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

        guard = self._guard(role, ctx)

        candidates = await self._candidates(route, model_override)

        trace_id = uuid.uuid4()
        yield GatewayStreamEvent("start", data={"trace_id": str(trace_id), "role": role})

        last_error = "no candidates"
        for attempt, model in enumerate(candidates):
            started = time.monotonic()
            billed = backend.Usage()
            emitted = False
            finished = False
            try:
                async with contextlib.aclosing(backend.run_model_stream(
                    model, prompt, instructions=instructions, output_type=output_type, tools=tools,
                    tool_guard=guard, max_output_tokens=route.max_output_tokens,
                    thinking_budget=route.thinking_budget, temperature=temperature,
                    message_history=message_history, timeout_s=route.timeout_s, usage_out=billed,
                )) as chunks:
                    async for chunk in chunks:
                        if chunk.kind != "result":
                            emitted = True
                            yield GatewayStreamEvent(chunk.kind, text=chunk.text, data=chunk.data)
                            continue
                        res = chunk.result
                        await self._note_local(model, None)
                        latency = int((time.monotonic() - started) * 1000)
                        cost = pricing.cost_usd(res.model, res.input_tokens, res.output_tokens,
                                                res.cached_input_tokens)
                        rec = CallRecord(
                            role, res.model, "OK", res.input_tokens, res.output_tokens, res.cached_input_tokens,
                            res.thinking_tokens, cost, latency, trace_id=trace_id, parent_trace_id=parent_trace_id,
                            session_id=ctx.session_id, mandate_id=ctx.mandate_id, tool_calls=res.tool_calls,
                            output=res.output,
                        )
                        ok = await write_call(self.pool, rec)
                        await self.budget.record(role, cost, persisted=ok)
                        finished = True
                        result = GatewayResult(
                            output=res.output, role=role, model=res.model, cost_usd=cost,
                            input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                            thinking_tokens=res.thinking_tokens, latency_ms=latency, trace_id=trace_id,
                            fell_back=model != route.primary_model, tool_calls=res.tool_calls, messages=res.messages,
                        )
                        self._maybe_shadow(route, prompt=prompt, instructions=instructions, output_type=output_type,
                                           tools=tools, temperature=temperature, message_history=message_history,
                                           model_override=model_override, result=result,
                                           parent_trace_id=parent_trace_id)
                        yield GatewayStreamEvent("done", result=result)
                        return
            except backend.BackendError as e:
                latency = int((time.monotonic() - started) * 1000)
                last_error = str(e)
                u = e.usage
                cost = pricing.cost_usd(model, u.input_tokens, u.output_tokens, u.cached_input_tokens)
                await self._note_local(model, e)
                terminal = emitted or attempt == len(candidates) - 1 or (
                    isinstance(e, backend.OutputInvalid) and route.fallback_policy is FallbackPolicy.FAIL_CLOSED)
                # The client already has trace_id from "start": the attempt that ends the stream owns it,
                # so the Decision Trace of a failed answer resolves; a retried attempt gets its own id
                ok = await write_call(self.pool, CallRecord(
                    role, model, "ERROR", u.input_tokens, u.output_tokens, u.cached_input_tokens, u.thinking_tokens,
                    cost, latency, error=last_error, trace_id=trace_id if terminal else uuid.uuid4(),
                    parent_trace_id=parent_trace_id, session_id=ctx.session_id))
                await self.budget.record(role, cost, persisted=ok)
                logger.warning("Gateway stream %s attempt %d on %s failed: %s", role, attempt + 1, model, e)
                if isinstance(e, backend.OutputInvalid) and route.fallback_policy is FallbackPolicy.FAIL_CLOSED:
                    raise InvalidModelOutput(role, last_error) from e
                if emitted:  # the caller already showed part of this answer
                    raise ModelUnavailable(role, last_error) from e
                continue
            except (GeneratorExit, asyncio.CancelledError):
                if not finished:  # stopped by the caller: cost what was billed, keep the trace
                    cost = pricing.cost_usd(model, billed.input_tokens, billed.output_tokens,
                                            billed.cached_input_tokens)
                    rec = CallRecord(role, model, "CANCELLED", billed.input_tokens, billed.output_tokens,
                                     billed.cached_input_tokens, billed.thinking_tokens, cost,
                                     int((time.monotonic() - started) * 1000), trace_id=trace_id,
                                     parent_trace_id=parent_trace_id, session_id=ctx.session_id,
                                     mandate_id=ctx.mandate_id, error="cancelled by caller")
                    async def settle() -> None:
                        ok = await write_call(self.pool, rec)
                        await self.budget.record(role, cost, persisted=ok)

                    # one shielded task: a re-delivered cancellation cannot skip the budget record
                    await asyncio.shield(settle())
                raise

        raise ModelUnavailable(role, last_error)


# --- Streaming (appended section, blueprint A3.3) ---
import contextlib  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402,F401  (used in the stream annotation)
from dataclasses import field  # noqa: E402


def _truthy(raw: Any) -> bool:
    """``system_settings.value`` as a flag: true / "true" / 1 / "1" / "yes" (JSON strings too)."""
    if raw is None:
        return False
    text = str(raw).strip().strip('"').strip().lower()
    return text in {"true", "1", "yes", "on"}


@dataclass
class GatewayStreamEvent:
    """One event of ``Gateway.stream``: start, token, tool_call, tool_result or done."""

    kind: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    result: GatewayResult | None = None
