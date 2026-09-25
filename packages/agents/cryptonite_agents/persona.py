"""An agent persona: a gateway role plus its instructions, tools and output contract.

Personas hold no model or provider; ``Persona.run`` sends everything through the AI Gateway.
Operator overrides from ``agent_configs`` (model_name, temperature, system_instructions) apply
per call.
"""

from dataclasses import dataclass, replace
from typing import Any

from cryptonite_agents.gateway import CallContext, Gateway, GatewayResult, ToolSpec


@dataclass(frozen=True)
class Persona:
    name: str  # agent_configs key / display name
    role: str  # gateway role (routing, budget priority, fallback policy)
    instructions: str
    tools: tuple[ToolSpec, ...] = ()
    output_type: Any = str

    def with_instructions(self, instructions: str) -> "Persona":
        return replace(self, instructions=instructions)

    async def run(
        self,
        gateway: Gateway,
        prompt: str,
        *,
        config: dict[str, Any] | None = None,
        parent_trace_id: Any = None,
        context: CallContext | None = None,
    ) -> GatewayResult:
        cfg = config or {}
        return await gateway.run(
            self.role,
            prompt,
            instructions=cfg.get("system_instructions") or self.instructions,
            output_type=self.output_type,
            tools=self.tools,
            model_override=cfg.get("model_name") or None,
            temperature=cfg.get("temperature"),
            parent_trace_id=parent_trace_id,
            context=context,
        )
