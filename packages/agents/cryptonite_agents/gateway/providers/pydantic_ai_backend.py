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
