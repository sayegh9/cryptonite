# ADR 0001: AI provider layer — own gateway on top of Pydantic AI

- Status: Accepted (2026-09-25), spike A1.1 passed
- Context: [AI-first blueprint](../ai-first-blueprint.md) decision D3

## Decision

Every model call goes through `cryptonite_agents.gateway` (routing by role, budget guard, permission gate, fallback policy, cost ledger, decision traces). Under it, **pydantic-ai-slim 2.50** (`[google,openai]`) talks to the models. Only `gateway/providers/pydantic_ai_backend.py` imports pydantic-ai or a provider SDK; `tests/test_ai_gateway.py::test_only_the_backend_imports_model_sdks` enforces it. `google-antigravity` is removed.

## Why

- The agents used `google.antigravity`: Gemini-only, no local models, `allow_all` tool policies, no usage data, no wheel for macOS x86_64 (which blocked pytest on the dev Mac).
- Pydantic AI speaks Pydantic (our contract language), validates structured output and re-asks on failure, supports Gemini and Ollama, reports token usage, and ships test models so the suite runs offline and free.
- Owning the gateway keeps the parts that decide safety — which model, which tools, how much spend, what trace — out of any framework. If Pydantic AI disappoints, only the backend module changes.

## Spike evidence (A1.1, eval set `v1`, stored in `ai_eval_runs`)

| | Flash (×2 repeats) | Pro | antigravity baseline (Flash, ×2) |
|---|---|---|---|
| Cases | 40 | 15 (verdict, triage, tool) | 30 (chat, verdict, triage) |
| Tool-chain success (≥ 95%) | 100% | 100% | — |
| Structured output valid (≥ 99%) | 100% | 100% | 100% (regex JSON parsing) |
| Directional agreement | 100% | 100% | — |
| Latency p50 / p95 (chat+verdict+triage) | 1.2 s / 7.3 s | verdict p50 11 s | 4.2 s / 7.5 s |
| Cost | $0.050 total | $0.147 total; verdict ≈ $0.012/call | not reported by antigravity |

Caveats: 65 live cases is a small sample; the verdict and triage expectations are hand-written. The set grows with recorded scanner candidates (blueprint §8).

## Findings recorded during the spike

- `AgentRunResult.usage` is a property in 2.50 (the docs show `usage()`).
- Gemini `output_tokens` include thinking tokens (a one-line answer used 415 thinking tokens of 454 output), so thinking is billed at the output rate; per-role `thinking_budget` is a real cost lever (Phase 2).
- An explicitly chosen model (`model_override`, evals) is pinned: DEGRADE roles do not fall back from it, otherwise an eval could silently measure a different model.
- Tests unset `GEMINI_API_KEY` (autouse fixture) so no test can make a paid call.
