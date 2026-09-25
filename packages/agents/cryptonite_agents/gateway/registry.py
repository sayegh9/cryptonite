"""Tool registry: every tool an agent may call, tagged with the risk it can create (blueprint §4.1).

Tools are plain Python callables; the provider backend wraps them. The risk class is what the
permission gate checks before a call runs.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RiskClass(StrEnum):
    READ = "READ"  # reads data or stages work for operator approval; changes no exposure
    REDUCE_RISK = "REDUCE_RISK"  # closes / shrinks exposure
    ADD_RISK = "ADD_RISK"  # opens / grows exposure


@dataclass(frozen=True)
class ToolSpec:
    name: str
    func: Callable[..., Any]
    risk: RiskClass
    description: str
    side_effects: bool = False  # starts paid or external work (e.g. queues a debate); evals strip these

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)


_REGISTRY: dict[str, ToolSpec] = {}


def tool(risk: RiskClass, name: str | None = None, side_effects: bool = False) -> Callable[[Callable[..., Any]], ToolSpec]:
    """Register a function as a gateway tool. The docstring is the description the model sees."""

    def decorate(func: Callable[..., Any]) -> ToolSpec:
        spec = ToolSpec(name or func.__name__, func, risk, (func.__doc__ or "").strip(), side_effects)
        if spec.name in _REGISTRY and _REGISTRY[spec.name].func is not func:
            raise ValueError(f"tool {spec.name!r} is already registered")
        _REGISTRY[spec.name] = spec
        return spec

    return decorate


def get_tool(name: str) -> ToolSpec:
    return _REGISTRY[name]


def registered_tools() -> dict[str, ToolSpec]:
    return dict(_REGISTRY)
