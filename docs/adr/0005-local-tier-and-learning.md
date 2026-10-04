# ADR 0005: The local model tier and the learning loop (Phases 6–7)

- Status: Accepted (2026-10-04)
- Context: [AI-first blueprint](../ai-first-blueprint.md) Phases 6–7 (A6.1–A6.4, A7.1–A7.4), backlog LE-1…LE-10
- Branch: `feat/ai-first-phase6`, PR to `feat/ai-first-phase0`

## Decisions

1. **Ollama runs on the host, not in Docker.** This host has no NVIDIA container runtime. A single
   systemd service binds to the docker bridge (`172.17.0.1:11434`) only; containers reach it through
   `host-gateway`. It is never reachable on loopback, the LAN or the internet (invariant 8).
2. **A local model is just another route target.** Roles name `ollama:<model>` like any Gemini model. A
   health probe (`local_tier:health` in Redis) lets `DEGRADE` routes skip a down or un-pulled local
   primary at once instead of timing out. `FAIL_CLOSED` and protected routes (trade decisions) never
   switch, so a local outage can never put a different model on a trade verdict.
3. **Only cheap, checkable roles go local:** `triage` and `summarize` on Gemma 4 12B (QAT), chosen by
   bake-off (triage 90.4% vs Flash-Lite 82.7% excluding stale cases; summaries 100% vs 62.5%). Debates,
   verdicts and strategy work stay on Gemini Flash / Pro.
4. **Thinking is off for local roles.** `thinking_budget=0` maps to `reasoning_effort: "none"`; Ollama
   ignored the budget and Qwen reasoned before every one-word answer (19 s → 0.7 s).
5. **Embeddings are local and tagged with their model.** Google retired `text-embedding-004`, which had
   silently broken reflection recall. `embeddinggemma` (768-d, matches the column) matched
   `gemini-embedding-001` on our recall eval. Each vector records `embedding_model`; retrieval compares
   only same-model vectors; changing the model re-embeds every row in one transaction. The `embed` role
   has no fallback, because a vector from another model is useless for retrieval.
6. **News sentiment uses FinBERT on CPU**, in its own service with no secrets and no external network.
   CryptoBERT scored far lower; Flash-Lite tagged best but costs a call per headline. The `tagging` route
   stays on Flash-Lite for anything that needs it.
7. **Outcomes, not opinions, drive learning.** The learner (one instance) labels each debate decision at
   1h/4h/24h/72h with signed return, MFE and MAE from the lake; reflections are written from the 24h
   outcome with alpha against BTC; recall ranks lessons by outcome magnitude, then distance.
8. **Verdict confidence must be calibrated before L3.** ECE in percentage points over the labelled
   decisions; below 20 samples it is unmeasured and the gate fails closed.
9. **A distilled model replaces the base only if certified.** The student must beat its own base model on
   production-relevant agreement on every holdout (a later-in-time real-history holdout and the
   synthetic set), with a valid-output rate no lower. Training happens outside Docker on the host GPU;
   inputs of distillable roles are logged in `ai_decision_traces.input` for future data.

## Evidence

Bake-off tables, the outage drill and the distillation result are in [HANDOFF.md](../../HANDOFF.md)
(Phases 6–7). Tests: `test_ai_local_tier.py`, `test_ai_embeddings.py`, `test_ai_outcomes.py`,
`test_ai_calibration.py`, plus gateway and eval additions.

## Consequences

- The GPU is shared between serving and training; training means moving local roles to Gemini for its
  duration (runbook: [local_ai_tier.md](../runbooks/local_ai_tier.md)).
- Local roles cost nothing per call but add 1–3 s of latency compared with Flash-Lite; fine for triage and
  summaries, not for anything interactive.
