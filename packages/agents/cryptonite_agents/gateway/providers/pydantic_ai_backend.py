"""The only module that imports pydantic-ai or a model provider SDK (blueprint D3).

Everything above the gateway talks to ``run_model`` / ``embed``; swapping pydantic-ai for another
backend means replacing this file only.

Model ids: ``google:<gemini model>`` (GEMINI_API_KEY), ``ollama:<model>`` (OLLAMA_BASE_URL,
OpenAI-compatible), or any id registered with ``override_model`` (tests).
"""

import contextlib
import functools
import inspect
import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from cryptonite_agents.gateway.registry import ToolSpec
from pydantic_ai import Agent, Tool, capture_run_messages
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.settings import ModelSettings

OUTPUT_RETRIES = 2  # re-ask the model when its structured output fails validation
PRO_MIN_THINKING_BUDGET = 128  # Gemini 2.5 Pro cannot disable thinking


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    thinking_tokens: int = 0


class BackendError(Exception):
    """A failed run; ``usage`` is what the provider billed before it failed (retries included)."""

    def __init__(self, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage or Usage()


class ProviderUnavailable(BackendError):
    """The provider (or a tool, or the transport) failed; fallback may apply."""


class OutputInvalid(BackendError):
    """The model never produced output that validates against the requested type."""


def _usage_of(messages: Sequence[Any]) -> Usage:
    usage = Usage()
    for m in messages:
        if isinstance(m, ModelResponse) and m.usage is not None:
            usage.input_tokens += m.usage.input_tokens or 0
            usage.output_tokens += m.usage.output_tokens or 0
            usage.cached_input_tokens += m.usage.cache_read_tokens or 0
            usage.thinking_tokens += int((m.usage.details or {}).get("thoughts_tokens", 0))
    return usage


@dataclass
class BackendResult:
    output: Any
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    thinking_tokens: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    messages: list[Any] = field(default_factory=list)  # opaque history for follow-up turns


_OVERRIDES: dict[str, Model] = {}
_MODELS: dict[str, Model] = {}


@contextlib.contextmanager
def override_model(model_id: str, model: Model) -> Iterator[None]:
    """Serve ``model_id`` from ``model`` (e.g. pydantic-ai's TestModel/FunctionModel) inside the block."""
    previous = _OVERRIDES.get(model_id)
    _OVERRIDES[model_id] = model
    try:
        yield
    finally:
        if previous is None:
            _OVERRIDES.pop(model_id, None)
        else:
            _OVERRIDES[model_id] = previous


def _build_model(model_id: str) -> Model:
    if model_id in _OVERRIDES:
        return _OVERRIDES[model_id]
    if model_id in _MODELS:
        return _MODELS[model_id]
    provider, _, name = model_id.partition(":")
    if provider == "google":
        model: Model = GoogleModel(name, provider=GoogleProvider(api_key=os.getenv("GEMINI_API_KEY")))
    elif provider == "ollama":
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        model = OpenAIChatModel(name, provider=OllamaProvider(base_url=base_url))
    else:
        raise ProviderUnavailable(f"unknown model provider in {model_id!r}")
    _MODELS[model_id] = model
    return model


def _settings(model_id: str, max_output_tokens: int | None, thinking_budget: int | None,
              temperature: float | None, timeout_s: float | None) -> ModelSettings:
    base: dict[str, Any] = {}
    if timeout_s is not None:
        base["timeout"] = timeout_s
    if max_output_tokens is not None:
        base["max_tokens"] = max_output_tokens
    if temperature is not None:
        base["temperature"] = temperature
    if model_id.startswith("google:"):
        if thinking_budget is not None:
            budget = max(thinking_budget, PRO_MIN_THINKING_BUDGET) if "-pro" in model_id else thinking_budget
            base["google_thinking_config"] = {"thinking_budget": budget}
        return GoogleModelSettings(**base)
    return ModelSettings(**base)


def _as_tool(spec: ToolSpec, guard: Callable[[ToolSpec], str | None], calls: list[dict[str, Any]]) -> Tool:
    """Wrap a registry tool: permission guard first, and every call recorded for the trace."""
    func = spec.func

    def record(kwargs: dict[str, Any], status: str) -> None:
        calls.append({"tool": spec.name, "risk": spec.risk.value, "args": kwargs, "status": status})

    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if (denied := guard(spec)) is not None:
                record(kwargs, "DENIED")
                return denied
            record(kwargs, "OK")
            return await func(*args, **kwargs)
    else:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if (denied := guard(spec)) is not None:
                record(kwargs, "DENIED")
                return denied
            record(kwargs, "OK")
            return func(*args, **kwargs)

    return Tool(wrapper, takes_ctx=False, name=spec.name, description=spec.description or None)


async def run_model(
    model_id: str,
    prompt: str,
    *,
    instructions: str | None = None,
    output_type: Any = str,
    tools: Sequence[ToolSpec] = (),
    tool_guard: Callable[[ToolSpec], str | None] = lambda spec: None,
    max_output_tokens: int | None = None,
    thinking_budget: int | None = None,
    temperature: float | None = None,
    message_history: Sequence[Any] | None = None,
    timeout_s: float | None = None,
) -> BackendResult:
    calls: list[dict[str, Any]] = []
    with capture_run_messages() as run_messages:
        try:
            agent = Agent(  # inside the try: provider setup errors (e.g. a missing API key) map too
                _build_model(model_id),
                output_type=output_type,
                instructions=instructions,
                tools=[_as_tool(t, tool_guard, calls) for t in tools],
                retries=OUTPUT_RETRIES,
            )
            result = await agent.run(
                prompt,
                message_history=list(message_history) if message_history else None,
                model_settings=_settings(model_id, max_output_tokens, thinking_budget, temperature, timeout_s),
            )
        except (UnexpectedModelBehavior, UsageLimitExceeded) as e:
            raise OutputInvalid(f"{model_id}: {e}", _usage_of(run_messages)) from e
        except (TimeoutError, ModelAPIError) as e:
            raise ProviderUnavailable(f"{model_id}: {e}", _usage_of(run_messages)) from e
        except Exception as e:  # transport errors, tool exceptions, provider setup: never escape raw
            raise ProviderUnavailable(f"{model_id}: {type(e).__name__}: {e}", _usage_of(run_messages)) from e
    usage = result.usage
    details = dict(usage.details or {})
    return BackendResult(
        output=result.output,
        model=model_id,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,  # Gemini: includes thinking tokens
        cached_input_tokens=usage.cache_read_tokens or 0,
        thinking_tokens=int(details.get("thoughts_tokens", 0)),
        tool_calls=calls,
        messages=list(result.all_messages()),
    )


async def embed(model_id: str, texts: Sequence[str]) -> tuple[list[list[float]], int]:
    """Embed ``texts``; returns (vectors, approximate input tokens)."""
    if model_id in _OVERRIDES and hasattr(_OVERRIDES[model_id], "embed_texts"):
        return await _OVERRIDES[model_id].embed_texts(texts)  # test double
    provider, _, name = model_id.partition(":")
    if provider != "google":
        raise ProviderUnavailable(f"no embedding backend for {model_id!r}")
    from google import genai  # provider SDK, confined to this module

    try:
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        resp = await client.aio.models.embed_content(model=name, contents=list(texts))
    except Exception as e:
        raise ProviderUnavailable(f"{model_id}: {e}") from e
    vectors = [list(e.values) for e in resp.embeddings]
    return vectors, sum(len(t) for t in texts) // 4


# --- Streaming (AI-first blueprint A3.3) --------------------------------------------------------
# Appended as its own section so the non-streaming path above stays untouched.

import asyncio  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402

from pydantic_ai.messages import (  # noqa: E402
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)


@dataclass
class StreamChunk:
    """One event of a streamed run: ``token`` (text delta), ``tool_call``, ``tool_result`` or ``result``."""

    kind: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    result: BackendResult | None = None


def _fill_usage(target: Usage | None, messages: Sequence[Any]) -> None:
    if target is None:
        return
    billed = _usage_of(messages)
    target.input_tokens, target.output_tokens = billed.input_tokens, billed.output_tokens
    target.cached_input_tokens, target.thinking_tokens = billed.cached_input_tokens, billed.thinking_tokens


async def run_model_stream(
    model_id: str,
    prompt: str,
    *,
    instructions: str | None = None,
    output_type: Any = str,
    tools: Sequence[ToolSpec] = (),
    tool_guard: Callable[[ToolSpec], str | None] = lambda spec: None,
    max_output_tokens: int | None = None,
    thinking_budget: int | None = None,
    temperature: float | None = None,
    message_history: Sequence[Any] | None = None,
    timeout_s: float | None = None,
    usage_out: Usage | None = None,
) -> AsyncIterator[StreamChunk]:
    """Like ``run_model`` but yields text deltas and tool events as they happen, then one ``result``.

    Closing the generator early (operator pressed stop, client went away) cancels the model run.
    ``usage_out`` receives what the provider billed so far, also when the run fails or is cancelled,
    so the caller can cost partial runs. Every tool call passes ``tool_guard`` exactly as in
    ``run_model``.
    """
    calls: list[dict[str, Any]] = []
    queue: asyncio.Queue[StreamChunk] = asyncio.Queue()
    reported: set[int] = set()  # indexes into ``calls`` already attached to a tool_result

    def call_status(tool_name: str) -> str:
        for i, c in enumerate(calls):
            if i not in reported and c["tool"] == tool_name:
                reported.add(i)
                return c["status"]
        return "UNKNOWN"

    async def on_events(ctx: Any, events: Any) -> None:
        async for ev in events:
            if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart) and ev.part.content:
                queue.put_nowait(StreamChunk("token", text=ev.part.content))
            elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta) and ev.delta.content_delta:
                queue.put_nowait(StreamChunk("token", text=ev.delta.content_delta))
            elif isinstance(ev, FunctionToolCallEvent):
                queue.put_nowait(StreamChunk("tool_call", data={
                    "tool": ev.part.tool_name, "tool_call_id": ev.part.tool_call_id,
                    "args": ev.part.args_as_dict()}))
            elif isinstance(ev, FunctionToolResultEvent):
                name = getattr(ev.part, "tool_name", None) or ""
                content = getattr(ev.part, "content", None)
                queue.put_nowait(StreamChunk("tool_result", data={
                    "tool": name, "tool_call_id": ev.part.tool_call_id, "status": call_status(name),
                    "content": content if isinstance(content, str) else str(content)}))

    async def run() -> BackendResult:
        with capture_run_messages() as run_messages:
            try:
                agent = Agent(
                    _build_model(model_id),
                    output_type=output_type,
                    instructions=instructions,
                    tools=[_as_tool(t, tool_guard, calls) for t in tools],
                    retries=OUTPUT_RETRIES,
                )
                result = await agent.run(
                    prompt,
                    message_history=list(message_history) if message_history else None,
                    model_settings=_settings(model_id, max_output_tokens, thinking_budget, temperature, timeout_s),
                    event_stream_handler=on_events,
                )
            except (UnexpectedModelBehavior, UsageLimitExceeded) as e:
                raise OutputInvalid(f"{model_id}: {e}", _usage_of(run_messages)) from e
            except (TimeoutError, ModelAPIError) as e:
                raise ProviderUnavailable(f"{model_id}: {e}", _usage_of(run_messages)) from e
            except asyncio.CancelledError:
                raise
            except Exception as e:  # transport errors, tool exceptions, provider setup: never escape raw
                raise ProviderUnavailable(f"{model_id}: {type(e).__name__}: {e}", _usage_of(run_messages)) from e
            finally:
                _fill_usage(usage_out, run_messages)
        usage = result.usage
        details = dict(usage.details or {})
        return BackendResult(
            output=result.output,
            model=model_id,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,  # Gemini: includes thinking tokens
            cached_input_tokens=usage.cache_read_tokens or 0,
            thinking_tokens=int(details.get("thoughts_tokens", 0)),
            tool_calls=calls,
            messages=list(result.all_messages()),
        )

    task = asyncio.create_task(run())
    getter: asyncio.Future[StreamChunk] | None = None
    try:
        while True:
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield getter.result()
                continue
            getter.cancel()
            while not queue.empty():
                yield queue.get_nowait()
            yield StreamChunk("result", result=task.result())  # re-raises the run's BackendError
            return
    finally:
        if getter is not None and not getter.done():
            getter.cancel()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BackendError):
                await task
