"""Eval harness (AI-first blueprint §8): replay a versioned case set through the gateway and score it.

Case kinds:
- ``chat``: a non-empty answer that mentions at least one ``must_mention`` term
- ``tool_chain``: every ``must_call`` tool was called (and allowed), then a non-empty answer;
  ``must_request`` tools were called at all (ADD_RISK calls are held for approval), ``must_not_call``
  tools were not. Optional fields (cases without them score as before):
  ``history`` (list of ``{role, content, widgets?, created_at?}``) and ``summary`` put the conversation
  before the message, rendered as the Brain renders it (``memory.render_prompt``); ``answer_must_match``
  (every regex matches the answer, case-insensitive), ``answer_must_not_match`` (no regex matches);
  ``max_requests`` (``{tool: n}``: calls of any status per tool are at most n); ``expect_args``
  (``{tool: {arg: [allowed] | {"min", "max"}}}``: every call to that tool satisfies it; no call passes)
- ``verdict`` / ``triage``: output validates against the persona's contract (the gateway re-asks on
  invalid output); ``expect`` adds a directional agreement check
- ``summary``: the Brain's compaction summary (``summarize`` role, its production instructions) keeps
  every ``must_keep`` fact (order-card command ids, sizes, the operator's limits), stays within
  ``max_words`` and uses no headings

Headline metrics: ``structured_valid_rate`` (verdict + triage outputs that validated),
``tool_chain_rate``, ``agreement_rate``, ``summary_rate``, cost and latency.
"""

import json
import re
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptonite_agents.brain import BRAIN
from cryptonite_agents.brain.memory import (
    SUMMARY_INSTRUCTIONS,
    SUMMARY_ROLE,
    BrainContext,
    render_prompt,
)
from cryptonite_agents.debate_fleet import (
    CHIEF_RISK_OFFICER,
    DEBATE_LITE,
    EXIT_MANAGER,
    RISK_PANEL,
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
    "lite": DEBATE_LITE,
    "risk_panel": RISK_PANEL,
    "brain": BRAIN,  # set brain_v1
    "summarizer": Persona("Summarizer", SUMMARY_ROLE, SUMMARY_INSTRUCTIONS),  # set summarize_v1
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


def _rules_hold(data: dict[str, Any], expect: dict[str, Any]) -> bool:
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


def _agrees(output: Any, expect: dict[str, Any]) -> bool:
    return _rules_hold(output.model_dump() if hasattr(output, "model_dump") else {}, expect)


def _case_prompt(case: dict[str, Any]) -> str:
    """A tool_chain case with history is asked the way the Brain is asked: clock, summary and turns included."""
    if case["kind"] != "tool_chain" or "history" not in case:
        return case["prompt"]
    return render_prompt(BrainContext(summary=case.get("summary"), turns=case["history"]), case["prompt"])


async def run_case(gateway: Gateway, case: dict[str, Any], model: str) -> CaseResult:
    persona = PERSONAS[case["persona"]]
    # Evals must not start real work (e.g. queue a paid debate on a running stack)
    persona = replace(persona, tools=tuple(t for t in persona.tools if not t.side_effects))
    started = time.monotonic()
    try:
        res = await persona.run(gateway, _case_prompt(case), config={"model_name": model})
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
        requested = {c["tool"] for c in res.tool_calls or []}  # any status: ADD_RISK is held for approval
        if case.get("must_request"):
            checks["requested_approval_tools"] = set(case["must_request"]) <= requested
        if case.get("must_not_call"):
            checks["avoided_forbidden_tools"] = not (set(case["must_not_call"]) & requested)
        text = str(res.output)
        if case.get("answer_must_match"):
            checks["answer_matches"] = all(re.search(p, text, re.IGNORECASE) for p in case["answer_must_match"])
        if case.get("answer_must_not_match"):
            checks["answer_clean"] = not any(re.search(p, text, re.IGNORECASE) for p in case["answer_must_not_match"])
        if case.get("max_requests"):
            counts = Counter(c["tool"] for c in res.tool_calls or [])  # any status, as above
            checks["within_request_limits"] = all(counts[tool] <= n for tool, n in case["max_requests"].items())
        if case.get("expect_args"):
            checks["args_as_expected"] = all(
                _rules_hold(c.get("args") or {}, rules)
                for tool, rules in case["expect_args"].items()
                for c in res.tool_calls or [] if c["tool"] == tool)
        checks["answered"] = bool(text.strip())
    elif kind in STRUCTURED_KINDS:
        checks["agrees"] = _agrees(res.output, case.get("expect", {}))
    elif kind == "summary":
        text = str(res.output)
        low = text.lower()
        checks["answered"] = bool(text.strip())
        checks["within_words"] = len(text.split()) <= case.get("max_words", 200)
        checks["kept_facts"] = all(f.lower() in low for f in case.get("must_keep", []))
        checks["no_headings"] = not any(line.lstrip().startswith("#") for line in text.splitlines())
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
        "summary_rate": rate([r.passed for r in results if r.kind == "summary"]),
        "errors": sum(1 for r in results if not r.valid),
        "cost_usd": str(sum((r.cost_usd for r in results), Decimal("0"))),
        "latency_p50_ms": int(statistics.median(latencies)) if latencies else None,
        "latency_p95_ms": latencies[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None,
    }


async def run_set(gateway: Gateway, set_name: str, model: str, repeat: int = 1,
                  kinds: set[str] | None = None,
                  personas: set[str] | None = None) -> tuple[list[CaseResult], dict[str, Any]]:
    cases = [c for c in load_set(set_name)
             if (not kinds or c["kind"] in kinds) and (not personas or c["persona"] in personas)]
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
