"""Eval harness (AI-first blueprint §8): replay a versioned case set through the gateway and score it.

Case kinds:
- ``chat``: a non-empty answer that mentions at least one ``must_mention`` term
- ``tool_chain``: every ``must_call`` tool was called (and allowed), then a non-empty answer
- ``verdict`` / ``triage``: output validates against the persona's contract (the gateway re-asks on
  invalid output); ``expect`` adds a directional agreement check

Headline metrics: ``structured_valid_rate`` (verdict + triage outputs that validated),
``tool_chain_rate``, ``agreement_rate``, cost and latency.
"""

import json
import statistics
import time
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptonite_agents.debate_fleet import (
    CHIEF_RISK_OFFICER,
    EXIT_MANAGER,
    TRIAGE,
    research_manager,
)
from cryptonite_agents.fleet import EXPLAINER, OPPORTUNITY_HUNTER, RESEARCH
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.persona import Persona

SETS_DIR = Path(__file__).parent / "sets"
STRUCTURED_KINDS = {"verdict", "triage"}

PERSONAS: dict[str, Persona] = {
    "research": RESEARCH,
    "explainer": EXPLAINER,
    "hunter": OPPORTUNITY_HUNTER,
    "pm": research_manager("No previous lessons yet."),
    "cro": CHIEF_RISK_OFFICER,
    "exit": EXIT_MANAGER,
    "triage": TRIAGE,
}


def load_set(name: str) -> list[dict[str, Any]]:
    path = SETS_DIR / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@dataclass
class CaseResult:
    case_id: str
    kind: str
    model: str
    passed: bool
    valid: bool
    checks: dict[str, bool] = field(default_factory=dict)
    output: Any = None
    error: str | None = None
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def _agrees(output: Any, expect: dict[str, Any]) -> bool:
    data = output.model_dump() if hasattr(output, "model_dump") else {}
    for key, rule in expect.items():
        value = data.get(key)
        if isinstance(rule, dict):
            if "max" in rule and not (value is not None and value <= rule["max"]):
                return False
            if "min" in rule and not (value is not None and value >= rule["min"]):
                return False
        elif value not in rule:
            return False
    return True


async def run_case(gateway: Gateway, case: dict[str, Any], model: str) -> CaseResult:
    persona = PERSONAS[case["persona"]]
    # Evals must not start real work (e.g. queue a paid debate on a running stack)
    persona = replace(persona, tools=tuple(t for t in persona.tools if not t.side_effects))
    started = time.monotonic()
    try:
        res = await persona.run(gateway, case["prompt"], config={"model_name": model})
    except Exception as e:  # one broken case must not end the run
        return CaseResult(case["id"], case["kind"], model, False, False, error=str(e),
                          latency_ms=int((time.monotonic() - started) * 1000))

    checks: dict[str, bool] = {}
    kind = case["kind"]
    if kind == "chat":
        text = str(res.output).lower()
        checks["answered"] = bool(text.strip())
        checks["on_topic"] = any(term in text for term in case.get("must_mention", [])) if case.get("must_mention") else True
    elif kind == "tool_chain":
        called = {c["tool"] for c in res.tool_calls or [] if c["status"] == "OK"}
        checks["called_required_tools"] = set(case.get("must_call", [])) <= called
        checks["answered"] = bool(str(res.output).strip())
    elif kind in STRUCTURED_KINDS:
        checks["agrees"] = _agrees(res.output, case.get("expect", {}))
    return CaseResult(
        case["id"], kind, res.model,
        passed=all(checks.values()), valid=True, checks=checks,
        output=res.output.model_dump() if hasattr(res.output, "model_dump") else str(res.output)[:2000],
        cost_usd=res.cost_usd, latency_ms=res.latency_ms, tool_calls=res.tool_calls or [],
    )


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    def rate(items: list[bool]) -> float | None:
        return round(sum(items) / len(items), 4) if items else None

    structured = [r for r in results if r.kind in STRUCTURED_KINDS]
    tool_chains = [r for r in results if r.kind == "tool_chain"]
    latencies = sorted(r.latency_ms for r in results if r.valid)
    return {
        "cases": len(results),
        "pass_rate": rate([r.passed for r in results]),
        "structured_valid_rate": rate([r.valid for r in structured]),
        "tool_chain_rate": rate([r.passed for r in tool_chains]),
        "agreement_rate": rate([r.checks.get("agrees", False) for r in structured if r.valid]),
        "chat_rate": rate([r.passed for r in results if r.kind == "chat"]),
        "errors": sum(1 for r in results if not r.valid),
        "cost_usd": str(sum((r.cost_usd for r in results), Decimal("0"))),
        "latency_p50_ms": int(statistics.median(latencies)) if latencies else None,
        "latency_p95_ms": latencies[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None,
    }


async def run_set(gateway: Gateway, set_name: str, model: str, repeat: int = 1,
                  kinds: set[str] | None = None) -> tuple[list[CaseResult], dict[str, Any]]:
    cases = [c for c in load_set(set_name) if not kinds or c["kind"] in kinds]
    results = []
    for _ in range(repeat):
        for case in cases:
            results.append(await run_case(gateway, case, model))
    return results, summarize(results)


async def store_run(pool, set_name: str, model: str, results: list[CaseResult], metrics: dict[str, Any],
                    passed: bool | None, notes: str | None = None) -> Any:
    async with pool.acquire() as conn:
        async with conn.transaction():
            run_id = await conn.fetchval(
                """INSERT INTO ai_eval_runs (set_version, model, metrics, passed, notes)
                   VALUES ($1, $2, $3::jsonb, $4, $5) RETURNING id""",
                set_name, model, json.dumps(metrics), passed, notes,
            )
            await conn.executemany(
                """INSERT INTO ai_eval_results (run_id, case_id, kind, model, passed, valid, checks, output,
                                               error, cost_usd, latency_ms, tool_calls)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10, $11, $12::jsonb)""",
                [(run_id, r.case_id, r.kind, r.model, r.passed, r.valid, json.dumps(r.checks),
                  json.dumps(r.output, default=str), r.error, r.cost_usd, r.latency_ms,
                  json.dumps(r.tool_calls, default=str)) for r in results],
            )
    return run_id


def result_rows(results: list[CaseResult]) -> list[dict[str, Any]]:
    return [asdict(r) for r in results]
