# AI-First Cryptonite: Blueprint

- Status: APPROVED 2026-09-25. D1–D12 (§2.1) agreed in discussion; O1–O4 (§2.2) accepted with the proposed defaults.
- Scope: turn Cryptonite from a manual terminal with AI panels into an AI-first platform for a single operator. The AI Brain discusses the market, does research, builds and tests strategies, and executes, either through conversation or autonomously inside an operator-set mandate. **Paper only.** Live money is out of scope.
- Written against `feat/perpetuals-phase1` @ `eebd747` plus the uncommitted working tree.
- Roles: Product (why/what), Delivery (phases, gates), AI engineering (gateway, brain, evals), UI (surfaces, notifications).

---

## 0. The press release (what "done" looks like)

> You open Cryptonite and talk to one Brain. It already knows your positions, your mandate, and what it concluded yesterday. You ask "what's going on with ETH?". It answers from live data, points to the funding spike, and offers a mean-reversion idea. You say "test it". It writes the strategy, backtests it, tunes the parameters, shows the tearsheet in the chat, and asks whether to paper-deploy it inside your "Alt-momentum" mandate. Overnight, it trades that mandate on its own. When it wants to add risk, your phone buzzes on Telegram with Approve / Veto buttons. When it cuts risk, it just does it and tells you. Every action has a "why" you can open. It costs about $5 a day, and it earned its autonomy with 30 days of measured paper results.

---

## 1. Findings: how the code differs from the goal

| # | Goal needs | Code today | Consequence |
|---|---|---|---|
| F1 | One Brain | Five separate personas that the operator picks in `AgentChatPanel.tsx` (`/agent-api/chat`, `/strategist`, `/explain`, `/cro`, `/onchain`) | Build one orchestrating agent; the personas become tools or sub-agents |
| F2 | Conversation memory | Each request builds a fresh agent and sends only `req.message` (`services/agent_worker/server.py:160-170`, `ChatRequest` has no history) | Server-side sessions plus memory |
| F3 | AI closes its own research loop | The strategy generator has `tools=[]` (`fleet.py`). It can't validate, backtest, or revise | Strategy tools over the existing backtester API |
| F4 | Real research inputs | The sentiment analyst has no news tool; on-chain data is mocked (`debate_fleet.py:43`) | Real sentiment and news sources, or remove those roles |
| F5 | Provider-agnostic AI | Everything goes through `google.antigravity` on Gemini. It has no macOS x86 wheel, which blocks pytest, and every agent runs with `pol.allow_all()` | AI Gateway (§4.1) |
| F6 | Spend control | `llm_cost_ledger` and `LLM_DAILY_BUDGET_USD=18` exist **only in migration 015**. No code writes to the ledger or checks the cap | Budget guard in the gateway |
| F7 | Mandated autonomy | One global `system_settings.DEBATE_EXECUTION_MODE` (`interactive` / `autonomous`). In autonomous mode the debate worker enqueues `SUBMIT_ORDER` with no per-strategy budget, scope, or drawdown limit (`debate_worker/main.py:302-340`) | Mandates enforced in code (§4.4); freeze autonomous mode until then (D11) |
| F8 | Structured, reliable verdicts | Verdict JSON is hand-parsed and falls back to `HOLD / "JSON Parsing failed"` (`debate_worker/main.py:431`) | Validated structured output with retries |
| F9 | Cost-aware triggering | The scanner pushes **every** candidate to `stream:debate_queue` (`scanner_service/worker.py:550`), even when the dispatcher would reject the trade | A pre-LLM gate (§6) |
| F10 | Learning from outcomes | Reflection treats "position closed" as 1 hour of age (`reflection.py:76`); `agent_safe_positions` has 3 incompatible contracts (HANDOFF) | Real outcome signals and one view contract |
| F11 | Auditable "why" | No tracing (Observability = Stub in AUDIT_STATUS) | Decision traces for every AI action |
| F12 | Remote alerts and approvals | Only a Slack webhook in `kill_switch.py:14` | Notifier service (Telegram + ntfy) |
| F13 | Honest paper results to gate autonomy | Limit orders fill immediately at bid/ask; funding missed during downtime is not backfilled (HANDOFF limits) | Fix paper fidelity before autonomy gates count (A0.4) |
| F14 | Kill switch reachable from the terminal | `KillSwitchPanel` still hard-codes `DEMO` (HANDOFF) | Point it at PAPER (A0.5) |

**Strengths to reuse unchanged:** `GuardedOrderDispatcher` with the 1–25% clamp, the latched kill switches, the daily 5% drawdown breaker, perp risk and liquidation, the Nautilus backtester with AST enforcement, the scanner, `StrategistEngine`, pgvector reflections, the `cryptonite_agent_ro` role, and `agent_configs`.

---

## 2. Decisions

### 2.1 Agreed

| ID | Decision |
|---|---|
| D1 | **Single operator.** No multi-tenancy, no onboarding flows, dense pro UI. |
| D2 | **Gemini first, model-agnostic by design, Ollama as the local tier.** |
| D3 | **AI Gateway we own** (router, tool registry, permission gate, budget guard, traces). **Pydantic AI underneath**, imported in exactly one module. If the A1.1 spike fails, replace it with a thin in-house wrapper around `google-genai` plus an OpenAI-compatible client. No LiteLLM, no LangChain. |
| D4 | **Hybrid tiers:** T0 deterministic → T1 local → T2 Flash-Lite (T1 fallback) → T3 Flash → T4 Pro. Trade decisions **fail closed** and never silently downgrade. |
| D5 | **Local hardware:** Linux workstation, dual Xeon, 64 GB RAM, RTX 3080 12 GB, reachable over Tailscale only. |
| D6 | **Local models (candidates, eval decides):** main model Gemma 4 12B (challenger Qwen3 14B); embeddings EmbeddingGemma 300M (768-d, matches `reflections.embedding vector(768)`); sentiment `ElKulako/cryptobert` (social) plus a FinBERT-family model (news) on CPU; optional Qwen3.6-35B-A3B only in scheduled windows. |
| D7 | **Notifications:** Telegram (two-way, primary) plus ntfy (secondary for critical alerts). |
| D8 | **Risk asymmetry:** risk-reducing actions run immediately and a timeout means *proceed*. Risk-adding actions need approval and a timeout means *no*. Approving new risk from Telegram requires TOTP. |
| D9 | **Autonomy ladder** L1 Copilot → L2 Supervised → L3 Autonomous, earned per mandate (§4.5), with automatic demotion. |
| D10 | **Loss limits:** 20% drawdown = hard stop (flatten + latch). Graduation requires staying well inside it (≤10%). The existing 5% daily breaker stays. |
| D11 | **Freeze `DEBATE_EXECUTION_MODE=autonomous`** until mandates (Phase 5) land. Interactive only until then. |
| D12 | **Budget:** gateway hard cap $25/day (replaces the unenforced $18), Telegram warning at 70%. Target typical spend is $4–6/day after Phase 2. |

### 2.2 Accepted with proposed defaults (2026-09-25)

| ID | Question | Decision |
|---|---|---|
| O1 | Can the AI trade perps? | **Spot-only for AI until a mandate earns L3; perps stay manual.** A mandate can opt in to perps after L3, capped at 5× leverage. |
| O2 | What does the 20% hard stop measure? | **Each mandate's own budget, plus an account-wide 20% backstop** that latches everything. |
| O3 | Gemini 2.5 or 3.x for Pro roles? | **2.5 Pro / 2.5 Flash now.** Move a role to 3.x only if the eval shows a gain worth ~1.5× the cost. 3.x Flash doubles in price on 2027-01-01. |
| O4 | Where does the perps branch land first? | **Merge `feat/perpetuals-phase1` to `main` before starting Phase 1**, so AI work isn't built on a moving branch. |

---

## 3. Goals, metrics, non-goals

**North Star:** risk-adjusted return (Sortino, after fees) of AI-managed paper capital vs BTC buy-and-hold over the same window, read together with the **operator intervention rate**. Both have to improve.

| Metric | Baseline | Target | Phase |
|---|---|---|---|
| LLM spend, typical day | unmeasured (est. ~$19) | ≤ $6 | 2 |
| Valid structured outputs | unknown (parse fallback exists) | ≥ 99% | 1 |
| Tool-chain success (Brain) | n/a | ≥ 95% | 3 |
| Idea → validated backtest, no manual steps | manual, multi-screen | < 10 min | 4 |
| Mandate breaches | n/a | 0 | 5 |
| Conviction calibration (per 10-pt bucket) | unmeasured | within ±10 pp | 7 |
| Triage: debates avoided / good setups missed | n/a | ≥ 60% avoided, ≤ 5% missed | 2 / 6 |
| Operator veto rate at L2 | n/a | < 10% (L3 gate) | 7 |

**Non-goals (and why):**

| Not building | Why | Revisit when |
|---|---|---|
| Live-money autonomy | Paper evidence first | A mandate holds L3 for 60 days |
| More debate personas | Quality comes from data and evals, not more roles | Eval shows a specific gap |
| Local models for final decisions | 12 GB-class models are too weak for tool chains and decisions | A local model passes the decision eval |
| LangChain / LiteLLM | Heavy dependencies on the trading path | 3+ providers needed |
| Mobile app | Telegram covers mobile | Telegram becomes limiting |
| HF "trading signal" LLMs | No verifiable record, risk of training on future data | Never for decisions; eval-only challengers |
| Multi-user / SaaS | D1 | Business decision |

---

## 4. Target architecture

```
                     Operator (web Brain Home · Mission Control · Telegram)
                                           │
                    /agent-api (Next proxy) │ services/notifier (Telegram/ntfy)
                                           ▼
   services/agent_worker ── Brain (session, memory, planner) ──┐
   services/debate_worker ── Debate (triage → lite → full)  ───┤
   overnight research job ── Strategy loop (batch)          ───┤
                                                               ▼
   packages/agents/cryptonite_agents/gateway/   (the only door to any model)
     router.py      role → primary, fallback, certified?, tier, budgets
     registry.py    typed tools tagged READ | REDUCE_RISK | ADD_RISK
     permission.py  mandate + autonomy check per tool call
     budget.py      $/day cap, per-role caps, 70% alert, degrade order
     trace.py       decision_trace + llm_cost_ledger rows, OTel spans
     providers/     pydantic_ai_backend.py (Gemini, Ollama) — only import site
                                                               │
             ┌───────────────── tools ─────────────────────────┤
             ▼                                                 ▼
  market brief · DuckDB · agent_ro SQL · scanner      propose_order / deploy_paper
  backtester API · AST validator · optimizer          → execution_commands (PAPER)
  sentiment svc · news · memory (pgvector)            → orchestrator → GuardedOrderDispatcher
```

Dependency rules (CLAUDE.md) are unchanged: services → packages, and the web reaches the backend only via `/api/v1` and `/agent-api`. Nothing reaches the dispatcher except through `execution_commands`.

### 4.1 AI Gateway (`packages/agents/cryptonite_agents/gateway/`)

- **Roles** (seeded into `ai_model_routes`, an extension of `agent_configs`): `brain.plan`, `brain.chat`, `strategy.draft`, `strategy.review`, `debate.lite`, `debate.side`, `debate.verdict`, `exit.verdict`, `triage`, `tagging`, `summarize`, `embed`, `digest`.
- **Route row:** `role, tier, primary_model, fallback_model, fallback_policy (FAIL_CLOSED | DEGRADE), certified_primary bool, max_output_tokens, thinking_budget, daily_cap_usd`.
- **Fallback policy:** `debate.verdict`, `exit.verdict`, `strategy.review` and `brain.plan` are FAIL_CLOSED. `triage`, `tagging`, `summarize`, `embed` and `digest` can DEGRADE (local → Flash-Lite).
- **Certification:** a model can only be `primary` for a role after a passing eval run is recorded for that pair (§8).
- **Budget degrade order when approaching the cap:** research → digests → chat on Pro (drops to Flash) → new debates. **Exits, the kill switch and risk-reducing actions never degrade.**

### 4.2 Brain

- One agent with a planner prompt and a tool registry. The old personas become tools (`explain_backtest`, `run_debate`, `onchain_snapshot`…).
- **Sessions:** `ai_sessions` / `ai_messages`. The local `summarize` role compacts history, so the Pro context holds the summary plus the last N turns plus relevant memory.
- **Memory:** (a) conversation, (b) a working state (positions, mandates, open theses) injected from the market brief, (c) long-term lessons in pgvector, ranked by **measured** outcome.
- **Streaming:** SSE via `/agent-api/brain/stream`. Tool calls and widgets are streamed as typed events.

### 4.3 Market brief (T0, deterministic)

Built once per closed bar per instrument by the scanner service from Polars indicators, `StrategistEngine` levels, funding/OI, positions and mandate usage. It's ~1–2k tokens, stored in Redis `brief:{instrument}:{tf}`, and is the shared, cacheable prefix for every agent. Agents stop pulling raw rows for routine context.

### 4.4 Mandates (enforced in code)

`ai_mandates`: `id, name, capital_budget_usd, instruments[], max_position_pct (≤25), max_drawdown_pct (default 20), allow_perps (O1), max_leverage, strategies[], autonomy_level, status`.

- **Enforcement point:** the orchestrator resolves `mandate_id` on every AI-originated command. `GuardedOrderDispatcher` gains a **mandate check** that runs after the existing invariants and never replaces them. New reject codes: `MANDATE_INSTRUMENT_NOT_ALLOWED`, `MANDATE_BUDGET_EXCEEDED`, `MANDATE_PERPS_NOT_ALLOWED`, `MANDATE_LEVERAGE_EXCEEDED`, `MANDATE_HALTED`.
- **Hard stop:** the orchestrator's risk monitor (the existing 1 s loop) tracks mandate equity vs its high-water mark. At ≥ `max_drawdown_pct` it flattens with reduce-only orders, sets `status=HALTED`, alerts at Critical level, and demotes the mandate to L1.

### 4.5 Autonomy ladder

| Level | AI may | Promotion gate (all required) |
|---|---|---|
| L1 Copilot | Propose; every risk-adding action needs approval | default |
| L2 Supervised | Act inside the mandate; ADD_RISK waits out a veto window (default 10 min, timeout = **no** outside operator-set active hours); REDUCE_RISK is immediate | ≥ 30 days **and** ≥ 30 closed trades at L1 on honest paper (A0.4 done); max DD ≤ 10%; 0 mandate breaches; Sortino > BTC hold after fees |
| L3 Autonomous | Act inside the mandate, no veto window; digest only | ≥ 60 days at L2; calibration within ±10 pp; veto rate < 10% |
| Demotion | Down one level automatically | Any breach, or drawdown ≥ ½ the hard stop; hard stop → L1 |

Promotion is **proposed by the system and confirmed by the operator** (TOTP). It never happens silently.

### 4.6 Notifier (`services/notifier`, new deployable)

- Telegram bot using **long polling**, so no public webhook or inbound port is needed. It accepts only the configured `chat_id`; anything else is dropped and logged.
- Tiers: **Info** (daily digest), **Action** (approval card with countdown and Approve / Veto / Details buttons), **Critical** (kill switch, hard stop, stale feed, budget exhausted, workstation down; also sent to ntfy).
- Button presses → HMAC-signed call to `POST /api/v1/ai/approvals/{id}`. ADD_RISK approval needs a TOTP reply. REDUCE_RISK and veto are one tap.
- `kill_switch.send_alert` (Slack) is routed through the notifier.

### 4.7 Local tier (workstation)

Ollama (`OLLAMA_KEEP_ALIVE=-1`, `NUM_PARALLEL=2`, `num_ctx` 8192) on Tailscale; `services/sentiment` (transformers/ONNX, CPU, `numactl`-pinned) serves cryptobert + FinBERT. The gateway health-checks both. When either is down, affected roles degrade to Flash-Lite and trade decisions fail closed.

**Embedding switch caveat:** existing `reflections.embedding` rows come from Gemini `text-embedding-004`. Vectors from different models can't be compared. Switching `embed` to EmbeddingGemma requires re-embedding all rows (A6.4), even though the dimension (768) matches.

---

## 5. Data model (migrations `022`+; `021` is the A0.3 view contract)

| Migration | Tables / changes |
|---|---|
| `022_ai_gateway` | `ai_model_routes`; `llm_cost_ledger` + `role, provider, cached_input_tokens, thinking_tokens, latency_ms, trace_id, status`; `ai_decision_traces (id, parent_id, role, session_id, mandate_id, inputs_ref, tool_calls jsonb, output jsonb, model, cost_usd, created_at)` |
| `023_ai_evals` | `ai_eval_runs (set_version, model, metrics jsonb, passed)`, `ai_eval_results`; case sets are versioned files in `cryptonite_agents/evals/sets/` |
| `024_ai_sessions` | `ai_sessions`, `ai_messages (session_id, role, content, widgets jsonb, trace_id)`, `ai_session_summaries` |
| `025_ai_mandates` | `ai_mandates`, `ai_mandate_equity (mandate_id, ts, equity, hwm)`, `ai_autonomy_events`; `execution_commands.mandate_id` |
| `026_ai_approvals` | `ai_approvals (id, mandate_id, action_class, payload, expires_at, timeout_policy, status, decided_via, decided_at)`, `notifications_outbox` |
| `027_ai_outcomes` | `ai_decision_outcomes (decision_id, horizon, return_pct, mfe, mae, labelled_at)` |

Grants: the agent role keeps read-only access through `agent_safe_*` views. Only the gateway service role writes ledger and trace rows.

---

## 6. Cost controls (Phase 2 unless noted)

1. **Pre-LLM gate** on `stream:debate_queue`: the kill switch, mandate budget, position cap, stale data, and a cooldown (same instrument+tf within 30 min → reuse the last verdict) are all checked **before** any model call.
2. **Triage** (`triage` role: Flash-Lite → local in Phase 6) decides whether a candidate is worth a debate.
3. **Stakes-sized debate:** allocation < 5% or conviction outside 0.35–0.65 → `debate.lite` (one Flash call, structured bull/bear/verdict). Otherwise the full fleet with the **merged risk panel** (one Pro call replaces Aggressive + Conservative + Neutral + CRO). Merge only after the eval shows no quality loss.
4. **Market brief** as a shared cached prefix; **context caching** on system prompts and debate transcripts.
5. **Thinking and output limits** per role.
6. **Deterministic exits** (trailing stop / targets) by default; an exit debate runs only on event triggers (funding spike, volatility regime shift, thesis-breaking news tag).
7. **Batch API** for overnight research and eval replays (Phase 4).
8. **LLM proposes the structure, the optimizer tunes the numbers** (Optuna over the backtester) (Phase 4).
9. **Lazy explanations:** narratives are generated when the operator opens the trace.

Expected typical day after Phase 2: ~$4–6 (details in the discussion estimate). The ledger replaces the estimate within a week of A1.3.

---

## 7. UI specification

**Principles:** the conversation is the primary surface and the terminal is the drill-down. Every AI action is one tap away from its "why". Risk-adding and risk-reducing actions look different (colour **and** label, never colour alone). Keep the terminal's density, show no hype copy, and state uncertainty.

### 7.1 Surfaces

| Surface | Replaces / extends | Content |
|---|---|---|
| **Brain Home** (`/` default for the operator) | `AgentChatPanel.tsx` persona picker, `AIBrainWorkspace.tsx` | One conversation; typed inline widgets (price chart, tearsheet, order card, strategy diff, debate summary); context rail showing positions, mandates, spend today; slash commands (`/test`, `/deploy`, `/why`) |
| **Mission Control** | `ActionInbox.tsx`, `OrchestratorCanvas.tsx` | Per-mandate card: budget used, equity curve vs BTC, drawdown gauge vs hard stop, level and next gate progress; open theses; approval queue with countdowns; one PAPER kill switch |
| **Decision Trace drawer** | new | Timeline: trigger → brief → tool calls → model outputs → dispatcher result, with cost and latency per step and the model used |
| **Mandate editor** | new | Form for the §4.4 fields; changes that add risk require TOTP; shows how the change would affect the current gates |
| **Autonomy & Evals** | new | Earned vs requested level, gate checklist with live numbers, calibration chart, eval results per role/model |
| **Spend panel** | new | Today vs cap, split by role and tier, cost per decision, cost per $ PnL |

**Stores** (persisted Zustand, `apps/web/src/lib/stores/`): `useBrainStore` (session id, draft, rail layout), `useMandateStore`, `useAutonomyStore`, `useSpendStore`. **Events:** `cryptonite:brain-open-trace`, `cryptonite:brain-ask` (send context from any workspace into the Brain, e.g. "ask about this chart"), `cryptonite:mandate-updated`.

**States to design for each surface:** loading (streaming tokens / tool running), gateway degraded (banner naming which roles fell back), budget near or at cap, workstation offline, kill switch latched, mandate halted.

**Accessibility:** meters use `role=meter` with text values (as in the existing `MarginRiskHUD`), countdowns announce at 60 s and 10 s, and everything is keyboard-reachable.

### 7.2 Telegram message templates

```
🟡 ACTION · Alt-momentum (L2)          ⏱ 10:00
Open LONG ETHUSDT · 6% of mandate ($1,200)
Conviction 0.71 · R:R 2.1 · stop 2,412
Why: funding reset + 4H reclaim of 2,450 (trace #a91f)
[Approve 🔐] [Veto] [Details]
```
```
🔴 CRITICAL · Hard stop hit — Alt-momentum
Drawdown 20.1% from HWM. Flattened 2 positions (reduce-only).
Mandate HALTED → L1. [Open Mission Control]
```

---

## 8. Evaluation harness

- **Location:** `packages/agents/cryptonite_agents/evals/` with a CLI `python -m cryptonite_agents.evals run --role triage --model <id>`. Results go to `ai_eval_*`.
- **Seed set:** the ~20-prompt A1.1 spike set (5 chat, 5 tool chains, 5 verdicts, 5 triage), grown to ≥200 triage and ≥50 verdict cases from recorded scanner candidates.
- **Time discipline:** cases are replayed **only with data available at their timestamp** (the brief is rebuilt as-of). Model comparisons use a holdout that is **later** than any data used for prompts or fine-tuning.
- **Metrics per role:** triage precision/recall against realized outcomes; verdict direction hit-rate and calibration; structured-output validity; tool-chain completion; cost and latency.
- **Gates:** certify a model for a role only if it beats or matches the current primary on quality within cost limits. Re-run on any prompt or model change (CI job on `packages/agents/**`, run offline with Pydantic AI's test models plus recorded fixtures).

---

## 9. Delivery roadmap

Assumes one operator plus AI pair-programming, about 15 weeks. Each item is an atomic commit (or a small series) with acceptance criteria. Week numbers are estimates and get re-planned at every phase gate.

### Phase 0: Foundation (wk 1–2)

| ID | Item | Acceptance |
|---|---|---|
| A0.1 | Merge `feat/perpetuals-phase1` → `main` (O4); triage uncommitted agent/scanner/api work into owned commits | Clean tree on `main`; HANDOFF updated |
| A0.2 | Dev DB rebuilt from migrations to head (fixes 013 + hand-made 014–016) | `alembic upgrade head` on dev DB ✅ |
| A0.3 | One `agent_safe_positions` contract (size, weight_pct, unrealized_pnl_pct) for `exit_monitor` + `debate_worker` — migration `021` | Contract test passes for both consumers |
| A0.4 | Paper fidelity: limit orders respect the limit price (rest until crossed); funding backfill via `/fapi/v1/fundingRate` | Tests: limit not filled until crossed; gap backfilled idempotently |
| A0.5 | `KillSwitchPanel` → PAPER | Playwright: terminal trip latches PAPER |
| A0.6 | Enforce D11: the debate worker ignores `DEBATE_EXECUTION_MODE=autonomous` until mandates exist (no API writes the setting; it is DB-only) | Test: every setting resolves to interactive |
| A0.7 | pytest on a clean checkout (no hang); fix stale `research_lab.spec.ts` | Full gates green |

**Gate 0:** all gates green on `main`, dev DB at head, autonomous mode frozen.

### Phase 1: AI Gateway + evals v1 (wk 2–4)

| ID | Item | Acceptance |
|---|---|---|
| A1.1 | **Spike (2 days):** Pydantic AI on Gemini 2.5 Flash/Pro with the 20-prompt set | ≥ 95% tool chains, ≥ 99% valid structured output, latency/cost ≤ today. Pass → Pydantic AI; fail → thin wrapper (D3) |
| A1.2 | `gateway/` skeleton: router, provider backend, registry, trace; migration `022` | Unit tests with test models; no `google.antigravity` import outside `providers/` |
| A1.3 | Budget guard: ledger writes on every call, $25 cap, 70% alert, degrade order | Test: cap reached → debates refused, exits still run |
| A1.4 | Port `fleet.py`, `debate_fleet.py`, `agent_worker` and `debate_worker` to the gateway; verdicts as Pydantic models (fixes F8) | Old endpoints behave the same; parse-fallback path deleted; pytest runs without API keys |
| A1.5 | Tool registry with READ / REDUCE_RISK / ADD_RISK tags; permission gate (mandate stub = deny ADD_RISK) | Test: an ADD_RISK tool call without approval is refused and traced |
| A1.6 | Evals v1: CLI, `023` tables, spike set as `v1` | `evals run` produces a stored, comparable report |
| A1.7 | Remove `google.antigravity` dependency | `uv lock` without it; macOS x86 pytest works |

**Gate 1:** every model call goes through the gateway and is costed and traced; the spend panel shows real numbers.

### Phase 2: Cost controls (wk 4–5)

| ID | Item | Acceptance |
|---|---|---|
| A2.1 | Pre-LLM gate + cooldown on the debate queue | Test: candidates the dispatcher would reject cost $0 |
| A2.2 | Market brief builder (scanner service) + Redis cache | Brief ≤ 2k tokens; built once per bar; used by all debate roles |
| A2.3 | Triage role (Flash-Lite) | Eval: ≥ 60% avoided, ≤ 5% good setups missed |
| A2.4 | `debate.lite` + stakes routing; merged risk panel (eval-gated) | Verdict eval not worse than baseline |
| A2.5 | Context caching + thinking/output limits per role | Cached-token share visible in the ledger |
| A2.6 | Deterministic exits by default; event-triggered exit debates | Exit debate count drops; no missed hard stops in replay |

**Gate 2:** typical day ≤ $6 measured over 7 days, with verdict quality not worse on the eval.

### Phase 3: One Brain (wk 5–8)

| ID | Item | Acceptance |
|---|---|---|
| A3.1 | Migration `024`; session + memory service; summary compaction | Brain recalls earlier turns across a reload |
| A3.2 | Brain agent with the planner + tool set (market, research, debate, explain, propose_order) | "Analyze BTC → propose → paper order (approved)" in one conversation |
| A3.3 | SSE stream endpoint + typed widget events | Tokens and widgets stream; cancel works |
| A3.4 | **Brain Home** UI (replaces the persona picker), inline widgets, context rail | Playwright spec with mocked SSE; no hydration errors |
| A3.5 | Decision Trace drawer + `cryptonite:brain-open-trace` | Every order card links to its trace |
| A3.6 | Real sentiment/news source for research (or delete the sentiment and on-chain roles, F4) | No role argues about data it doesn't have |

**Gate 3:** ≥ 95% tool-chain success on the Brain eval; the operator uses Brain Home as the default for a week.

### Phase 4: Closed research loop (wk 8–10)

| ID | Item | Acceptance |
|---|---|---|
| A4.1 | Tools: `draft_strategy` (Flash), `validate_ast`, `run_backtest` (async job + poll), `read_tearsheet`, `save_strategy_version` | Brain completes idea → validated backtest without manual steps |
| A4.2 | `optimize_params` (Optuna over the backtester, walk-forward split) | The LLM never guesses numeric params in the loop |
| A4.3 | `strategy.review` (Pro) only for drafts that pass AST + backtest | Pro calls per strategy ≤ 2 |
| A4.4 | Overnight research queue on the Batch API + morning digest | Digest lands at the configured time with results |
| A4.5 | Overfitting guard: out-of-sample and walk-forward metrics required on every tearsheet the Brain presents | Tearsheet widget shows IS vs OOS |

**Gate 4:** idea → validated backtest in < 10 min.

### Phase 5: Mandates, autonomy, Mission Control, Telegram (wk 10–13)

| ID | Item | Acceptance |
|---|---|---|
| A5.1 | Migration `025`; mandate check in the dispatcher path; new reject codes | Property tests: no AI order escapes its mandate; 1–25% clamp untouched |
| A5.2 | Mandate equity + HWM tracker; 20% hard stop → flatten, HALT, demote (O2) | Replay test: hard stop triggers, flattens, and latches |
| A5.3 | Migration `026`; approvals with expiry and timeout policy (D8) | Tests: ADD_RISK timeout = no; REDUCE_RISK = proceed |
| A5.4 | `services/notifier`: Telegram long polling, chat_id allow-list, TOTP for ADD_RISK, ntfy for Critical; route `send_alert` | Live test with a test bot; foreign chat ignored |
| A5.5 | Autonomy state machine + gate evaluator (promotion proposal, operator TOTP confirm, auto demotion) | Unit tests for every transition in §4.5 |
| A5.6 | **Mission Control**, Mandate editor, Autonomy & Evals UI | Playwright: approve/veto flow, halted state, degraded banner |
| A5.7 | Lift D11: autonomous mode exists only as mandate L2/L3 (global flag removed) | `DEBATE_EXECUTION_MODE` deleted |

**Gate 5:** 14 days of L1 paper trading on one mandate with 0 breaches and working Telegram approvals.

### Phase 6: Local tier (wk 13–15; can start after Gate 1, in parallel)

| ID | Item | Acceptance |
|---|---|---|
| A6.1 | Workstation: Ollama + Tailscale + health endpoint; Ollama provider in the gateway | Gateway lists the local tier healthy/unhealthy; fallback verified by pulling the plug |
| A6.2 | Eval Gemma 4 12B vs Qwen3 14B on `triage`, `tagging`, `summarize` | Certified winner, or stay on Flash-Lite |
| A6.3 | `services/sentiment` (cryptobert + FinBERT, CPU) + `tagging` tool | Eval on labelled crypto headlines/posts |
| A6.4 | EmbeddingGemma for `embed`; re-embed all `reflections` rows in one migration job | Recall eval not worse; no mixed embedding spaces |
| A6.5 | Optional: Qwen3.6-35B-A3B via llama.cpp in scheduled windows for `debate.side` | Only if it certifies |

**Gate 6:** local tier serves triage/tagging/summarize/embed with automatic fallback; cloud spend drops further or stays flat at higher volume.

### Phase 7: Earned autonomy and distillation (wk 15+, continuous)

| ID | Item | Acceptance |
|---|---|---|
| A7.1 | Migration `027`; outcome labeller (returns at fixed horizons, MFE/MAE) replaces the 1h reflection heuristic (F10) | Reflections are ranked by outcome |
| A7.2 | Calibration dashboard (reliability chart per role) | Visible in Autonomy & Evals |
| A7.3 | 30-day L2 candidacy run on one mandate | §4.5 gate evaluator reports pass/fail |
| A7.4 | Triage distillation: QLoRA (Unsloth) on Gemma 4 12B / Qwen3 14B from logged triage + outcomes; later-data holdout | Certifies only if it beats the base local model |

**Gate 7:** one mandate earns **L2** (the §13 program goal, ≈ wk 18). L3 needs ≥ 60 more days at L2 (≈ wk 26) and is the follow-on goal, not part of this program.

---

## 10. Safety invariants (never broken, tested in CI)

1. Every AI order goes `execution_commands → orchestrator → GuardedOrderDispatcher`. No tool can reach a venue or the dispatcher directly.
2. The 1–25% equity clamp, kill switches, 5% daily breaker and perp guards are unchanged. Mandate checks are **additive**.
3. ADD_RISK needs a valid approval or an earned L2/L3 mandate. REDUCE_RISK is always allowed, including when over budget, when the kill switch is latched (reduce-only), and when the gateway is degraded.
4. Trade-decision roles fail closed; no silent model downgrade.
5. Agents read the DB only through `cryptonite_agent_ro` / `agent_safe_*` views.
6. Autonomy promotion needs operator TOTP; demotion is automatic.
7. The notifier accepts only the configured `chat_id`.
8. Ollama and the sentiment service are never exposed beyond Tailscale.

---

## 11. Pre-mortem: "It's week 15 and this failed. Why?"

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Built the Brain on a moving branch / dirty tree | High | High | Gate 0 first (O4, A0.1) |
| Paper results flattered by the fill model → false autonomy promotion | Med | High | A0.4 before any gate counts |
| Pydantic AI tool calling is unreliable on Gemini | Med | Med | A1.1 spike with a documented fallback (D3) |
| LLM overfits strategies (looks great in backtest) | High | High | Optimizer + walk-forward + OOS required (A4.2, A4.5) |
| Cost savings silently degrade decision quality | Med | High | Every downgrade is eval-certified (§8) |
| Local workstation flaky | Med | Low | DEGRADE roles fall back; decisions never depend on it |
| Operator ignores Telegram → stuck approvals | Med | Med | Timeout = no for new risk; daily digest lists expired approvals |
| Scope creep (more personas, live money) | High | Med | Non-goals table; changes go through this doc |
| Embedding-space mix corrupts memory | Low | Med | A6.4 re-embed job |

---

## 12. Test plan (summary)

- **Unit:** router/fallback policy, budget guard, permission gate, mandate checks (property tests), autonomy transitions, approval timeouts, brief builder.
- **Contract:** Pydantic verdict/tool schemas, `agent_safe_*` views, SSE event schema, Telegram callback HMAC.
- **Integration (migrated DB helper `tests/migrated_db.py`):** debate → approval → command → dispatcher; hard stop flatten; budget cap.
- **Evals:** §8, in CI on `packages/agents/**` with recorded fixtures (no network).
- **E2E (Playwright, mocked API/WS/SSE):** Brain Home conversation with widgets, Trace drawer, Mission Control approve/veto, halted and degraded states.

---

## 13. Program goal (one goal for the whole plan)

> **By 2027-02-15, Cryptonite's single Brain runs the full research → test → paper-execution loop through the AI Gateway, for about $6/day, and one BTC/ETH spot mandate has *earned* L2 Supervised autonomy on honest paper results, with zero mandate breaches.**

Start date is Monday 2026-09-28. Phases 0–6 finish at ≈ wk 15 (2027-01-10). The L2 gate needs ≥ 30 days of L1 trading, which starts during Phase 5, so earliest promotion is ≈ wk 18. The goal date includes about 2 weeks of buffer.

**Done means all of these are true, each checked with evidence:**

| # | Criterion | Evidence |
|---|---|---|
| G1 | Every model call goes through the gateway; `google.antigravity` is gone | `uv lock` diff; grep shows no provider imports outside `gateway/providers/` |
| G2 | The operator goes idea → backtest → paper order in one Brain conversation, with no manual steps | Recorded session + trace; Playwright spec |
| G3 | Typical spend ≤ $6/day (7-day average), hard cap $25 enforced | `llm_cost_ledger` report; budget-cap test |
| G4 | One mandate promoted to L2 by the gate evaluator and confirmed by operator TOTP | `ai_autonomy_events` row + gate report (≥ 30 d, ≥ 30 trades, DD ≤ 10%, 0 breaches, Sortino > BTC hold) |
| G5 | Every §10 safety invariant is covered by a CI test; 0 mandate breaches in the ledger | CI run; `ai_approvals` / reject-code audit |
| G6 | The local tier serves triage with automatic fallback | Gateway health log; fallback drill |

**Out of this goal:** L3, perps under AI control (O1), live money.

---

## 14. Agent and skill assignments

### 14.1 How each roadmap item runs (the Dev ↔ QA loop)

```
1. Orient   ripwire-orient / ripwire-before-you-build   (graphify first, per CLAUDE.md)
2. Plan     agent-skills:plan → acceptance criteria copied from §9
3. Build    lead skill (14.3) + agent-skills:incremental-implementation + test-driven-development
4. Verify   agent-skills:test-engineer (agent) · UI: agency-test-automation-engineer + browser preview screenshots
5. Review   agent-skills:code-reviewer (agent) · ripwire-quality-bar · ripwire-change-check
            RED items add: agent-skills:security-auditor (agent) + agent-skills:doubt-driven-development
6. Decide   PASS → commit (agent-skills:git-workflow-and-versioning) → next item
            FAIL → back to Build with findings; max 3 attempts, then escalate to the operator
7. Gate     end of phase: agent-skills:ship (go/no-go fan-out) + operator sign-off
```

**RED items** (risk path: dispatcher, mandates, approvals, kill switch, budget): A0.4, A0.5, A0.6, A1.3, A1.5, A2.1, A5.1, A5.2, A5.3, A5.4, A5.5, A5.7, A7.3. They always get the security and doubt-driven review, plus a property or replay test.

### 14.2 Roster: available agents and skills vs the orchestrator template

The orchestrator template names agents that aren't installed here. Substitutes:

| Template role | Use instead (installed) | Kind |
|---|---|---|
| project-manager-senior | `agency-senior-project-manager` + `agent-skills:plan` | skill |
| ArchitectUX | `agency-software-architect` (backend) + `agency-ux-architect` (web) | skill |
| engineering-ai-engineer | `agency-ai-engineer` + `agency-prompt-engineer` | skill |
| Backend Architect | `agency-backend-architect` | skill |
| Frontend Developer | `agency-frontend-developer` + `agent-skills:frontend-ui-engineering` | skill |
| DevOps Automator | `agency-devops-automator` + `agent-skills:ci-cd-and-automation` | skill |
| EvidenceQA | `agent-skills:test-engineer` (agent) + `agency-test-automation-engineer` + `run` / browser preview screenshots | agent + skill |
| testing-reality-checker | `agent-skills:ship` (go/no-go) + `agency-code-reviewer` | skill |
| API Tester | `agency-api-platform-engineer` + `agent-skills:api-and-interface-design` | skill |
| Performance Benchmarker | `agent-skills:performance-optimization` / `ripwire-perf-target` | skill |
| Legal Compliance Checker | not needed (single operator, paper only; D1) | — |

Also used: `agency-database-optimizer` (migrations 021–027), `agency-application-security-engineer` (notifier, approvals, Tailscale), `agency-ui-designer` + `agency-ui-finish-gate-reviewer` (surfaces), `agency-minimal-change-engineer` (Phase 0 fixes), `agent-skills:source-driven-development` (Pydantic AI, Gemini, Ollama, Telegram Bot API, Optuna docs), `agent-skills:deprecation-and-migration` (antigravity removal, `DEBATE_EXECUTION_MODE` removal, re-embedding), `agent-skills:observability-and-instrumentation` (traces, ledger), `agent-skills:documentation-and-adrs` (ADR per decision D3/O1–O4), `ripwire-layers` (arch rules: gateway is the only provider import site), `ripwire-write-tests`, `agency-project-shepherd` (weekly status + HANDOFF), `code-review` (pre-merge on each phase branch).

### 14.3 Assignments per phase

| Phase | Lead | Supporting | QA / review | Operator (human-only) |
|---|---|---|---|---|
| **0 Foundation** | `agency-minimal-change-engineer` | `agency-database-optimizer` (A0.2, A0.3), `agency-backend-architect` (A0.4), `agency-frontend-developer` (A0.5), `agent-skills:git-workflow-and-versioning` (A0.1) | test-engineer, code-reviewer, `ripwire-change-check`; A0.4–A0.6 RED | Decide O4; **own the triage of the other author's uncommitted work (A0.1)**; Gate 0 sign-off |
| **1 Gateway + evals** | `agency-ai-engineer` | `agency-software-architect` (A1.2 design + ADR), `source-driven-development` (A1.1), `agency-database-optimizer` (022, 023), `deprecation-and-migration` (A1.7), `observability-and-instrumentation` (A1.2 traces) | test-engineer, code-reviewer, `ripwire-layers` rule; A1.3, A1.5 RED | **A1.1 spike pass/fail call**; Gate 1 sign-off |
| **2 Cost controls** | `agency-ai-engineer` | `agency-prompt-engineer` (A2.4, A2.5), `agency-backend-architect` (A2.1, A2.2), `performance-optimization` (brief size / latency) | test-engineer, eval runs (§8); A2.1 RED | Accept the 7-day spend reading; Gate 2 |
| **3 One Brain** | `agency-ai-engineer` (A3.1–A3.3, A3.6) + `agency-frontend-developer` (A3.4, A3.5) | `agency-ux-architect` + `agency-ui-designer` (Brain Home, widgets, Trace drawer), `agency-prompt-engineer` (planner prompt), `api-and-interface-design` (SSE event schema) | `agency-test-automation-engineer` (Playwright + mocked SSE), `agency-ui-finish-gate-reviewer`, `design:accessibility-review` | Use Brain Home daily for a week (Gate 3) |
| **4 Research loop** | `agency-ai-engineer` | `agency-backend-architect` (async backtest jobs, Optuna), `agency-investment-researcher` (walk-forward / OOS design, A4.5), `agency-prompt-engineer` | test-engineer, code-reviewer | Judge the first 3 AI-built strategies |
| **5 Mandates + autonomy + Telegram** | `agency-backend-architect` (A5.1–A5.3, A5.5, A5.7) + `agency-frontend-developer` (A5.6) | `agency-database-optimizer` (025, 026), `agency-application-security-engineer` (A5.4 HMAC, chat_id, TOTP), `agency-devops-automator` (notifier service), `agency-ui-designer` (Mission Control, Telegram cards) | **all RED**: security-auditor + `doubt-driven-development` + property/replay tests; `agent-skills:ship` at gate | **Create the Telegram bot (BotFather) and ntfy topic; hold TOTP**; decide O1, O2; Gate 5 |
| **6 Local tier** | `agency-ai-engineer` | `agency-devops-automator` (Ollama, Tailscale, `services/sentiment` scripts), `agency-application-security-engineer` (exposure review), `source-driven-development` (Ollama / llama.cpp) | eval certification runs (§8), fallback drill | **Install on the workstation** (Claude supplies scripts; it has no access to that machine) |
| **7 Earned autonomy** | `agency-ai-engineer` | `agency-investment-researcher` (outcome labels, calibration), `dataviz` (calibration chart), `agency-project-shepherd` (30-day run tracking) | test-engineer; A7.3 RED | **Confirm L2 promotion with TOTP** (G4) |

**Cross-cutting, every phase:** `agency-project-shepherd` updates HANDOFF.md weekly; `documentation-and-adrs` records every decision; `graphify update .` after each commit (CLAUDE.md).

### 14.4 Rules for running the pipeline

- One roadmap item at a time. The next item starts only after PASS, and the next phase only after the gate.
- Subagents get the item's row from §9, the relevant §10 invariants, and the file pointers from §1. They never get "the whole blueprint, go".
- A FAIL on the third attempt stops the pipeline and escalates to the operator with the QA findings. The pipeline doesn't skip ahead.
- Nothing in this pipeline pushes, merges to `main`, or changes `system_settings` in the dev DB without operator approval.

### 14.5 Parallel execution: Phase 2 (local) and Phase 3 (cloud)

Phase 3 depends only on the Phase 1 gateway, so it runs in a cloud session in parallel with Phase 2. File ownership prevents merge conflicts:

| Area | Phase 3 — cloud, branch `feat/ai-first-phase3-brain` | Phase 2 — local, branch `feat/ai-first-phase0` |
|---|---|---|
| Brain | **owns** new `packages/agents/cryptonite_agents/brain/**` (incl. its own `BRAIN_ROUTES`, merged into the gateway's routes by the caller) | — |
| Gateway | **append-only**: a new `run_model_stream` at the end of `providers/pydantic_ai_backend.py`, a new `Gateway.stream` method at the end of the class | owns `routes.py`, `budget.py`, `pricing.py`, the body of `run_model` (caching, thinking budgets) |
| Workers | **additive** `/api/v1/brain/*` endpoints in `services/agent_worker/server.py`; existing endpoints unchanged | owns `services/debate_worker/**`, `services/scanner/**`, `debate_fleet.py`, `tools.py` |
| Database | owns migration `024_ai_sessions` | no migrations in Phase 2 |
| Web | owns `apps/web/**` (Brain Home, Trace drawer, stores, SSE proxy, e2e) | no web changes in Phase 2 |
| Evals | adds `evals/sets/brain_v1.jsonl` | adds `evals/sets/v2.jsonl` (triage, debate.lite); `v1.jsonl` frozen |
| Out of scope for cloud | A3.6 (news/sentiment sources; touches the debate roles) → local after Phase 2 | — |

Merge order: Phase 2 lands on `feat/ai-first-phase0`; the cloud branch merges `feat/ai-first-phase0` before its PR is reviewed; the Phase 3 PR targets `feat/ai-first-phase0`. Nothing merges to `main` without the operator.
