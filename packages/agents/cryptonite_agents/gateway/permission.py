"""Permission gate: checked on every tool call before it runs (blueprint §4.1, §10 invariant 3).

Until mandates exist (Phase 5) no agent may add risk on its own: ADD_RISK tools need an explicit
operator approval id for the call. READ and REDUCE_RISK tools are always allowed.
"""

from dataclasses import dataclass, field

from cryptonite_agents.gateway.registry import RiskClass, ToolSpec


class ToolDenied(Exception):
    def __init__(self, tool: str, reason: str) -> None:
        super().__init__(f"{tool}: {reason}")
        self.tool = tool
        self.reason = reason


@dataclass(frozen=True)
class CallContext:
    role: str
    session_id: str | None = None
    mandate_id: str | None = None
    approved_actions: frozenset[str] = field(default_factory=frozenset)  # tool names the operator approved


class PermissionGate:
    def check(self, spec: ToolSpec, ctx: CallContext) -> None:
        if spec.risk is RiskClass.ADD_RISK and spec.name not in ctx.approved_actions:
            raise ToolDenied(spec.name, "ADD_RISK_REQUIRES_APPROVAL")
