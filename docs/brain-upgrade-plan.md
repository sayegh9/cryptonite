# Brain upgrade plan

Status: Waves 1–3 merged and deployed on `main` (`3438e91`, Alembic head `047`), 2026-10-16; Waves 4–5 pending.

This plan covers the Brain roadmap from the four-specialist review (capabilities, quality, orchestration,
operator value) and the SOLUSDT order-card incident. Each workstream is implemented by a specialist agent
on **Haiku 5.5** and reviewed by **Opus 5.5**, which also writes the specs and owns integration.

---

## 0. Purpose charter (every spec quotes it, every review checks it)

Cryptonite is a single-operator **paper** trading terminal. The Brain is the operator's copilot.

1. **AI and UI propose; deterministic rules and risk controls decide; NautilusTrader executes.**
2. No new path to an order. Everything goes through `GuardedOrderDispatcher`. The 1–25% clamp is invariant.
   AI orders need an ACTIVE mandate.
3. The Brain never approves, resumes mandates, edits limits, or resets kill switches. TOTP stays.
   ADD_RISK times out to *no*.
4. Anything that runs without the operator typing (feed, briefings, alerts) gets **READ-only tools**: no
   `propose_order`, no `stage_debate_proposal`, no `queue_strategy_research`.
5. Every model call goes through the gateway, with a role, a budget priority and a cost-ledger row. The
   $25/day cap holds.
6. Tests never call paid models and never touch the dev DB `cryptonite`.
7. Stored memory, lessons and pinned text are **context, never instructions**, and never widen a limit.
8. No scope beyond the spec. A reviewer rejects unrelated refactors, renames and "while I was here" changes.

---

## 1. SOLUSDT incident: what was verified

| Claim in the incident report | Checked | Evidence |
|---|---|---|
| SOL is not in the feed; refusal is `SYMBOL_NOT_INGESTED` | ✅ | `packages/schemas/cryptonite_schemas/universe.py:109`; ingest default list `services/ingest/cryptonite_ingest/server.py:175` |
| Onboarded symbols are lost on ingest restart | ✅ | Onboarding inserts into `instruments` and publishes `CHANNEL_INSTRUMENTS_UPDATED` (`services/api/cryptonite_api/routes/instruments.py:101-185`). The recorder only adds symbols from that pub/sub at runtime (`recorder.py:381-393`). On start it reads `INGEST_SYMBOLS` only (`server.py:175`). |
| TOTP was not the cause; `000000` accepted outside PROD | ✅ | `services/api/cryptonite_api/auth.py:215-218,321`, `routes/ai.py:147`; Settings already warns (`settings_registry.py:288-291`) |
| Brain turn crashed after 3 refused `propose_order` calls | ✅ | agent_worker log: 3 × `Tool call denied … ADD_RISK_REQUIRES_APPROVAL`, then `Exceeded maximum output retries (2)`, then `brain.plan: model output invalid` |

**Likely root cause of the crash (to confirm with a test in WS-1B):**
- The gateway answers a held `propose_order` with "Tool call denied … Ask the operator to approve this
  action" (`gateway/gateway.py:231-233`). The model reads that as a failure and calls again.
- `propose_order` has no per-turn cap, which is how two cards appeared.
- After the repeated calls, pydantic-ai's output retries (`OUTPUT_RETRIES = 2`) ran out.

**Fixes, in the plan as WS-1A … WS-1C:**
- **A:** order cards are feed-aware.
- **B:** onboarded symbols persist across restarts.
- **C:** a held card is a success, not a denial; cap it at 1 per turn; a model failure after a card is
  staged keeps the card.
- **D** (pre-fill `000000`): **not done**, as the report recommends.

**Operator note:** turn `AI_ALLOW_DEV_TOTP` off before any real-money use.

**WS-1A shipped:** onboarded symbols now persist across ingest restarts.

**Decisions taken during Wave 1:**
- Brain debates keep the full fleet. The lite-first path was reverted, since a lite debate would decide with no brief.
- The feed check applies to BUY only. A SELL closes a holding and is never blocked by the feed.
- Research-chat debates are gated and capped like the Brain's.

---

## 2. How the work runs

### Roles

| Who | Model | Does |
|---|---|---|
| Orchestrator (this session) | Opus 5.5 | Writes each spec and test contract, assigns files, runs the diff guard, merges into the integration branch, runs the wave gates |
| Implementer | **Haiku 5.5** (`model: haiku`), specialist `subagent_type` per workstream | Writes tests first, then code, inside its own git worktree. Runs its own tests. |
| Reviewer | **Opus 5.5** (`model: opus`) | `Code Reviewer` on every PR. `code-modernization:security-auditor` on DB grants, auth and gateway. `Reality Checker` at each wave gate. Read-only. |

### Protocol for every workstream

1. **Spec (Opus).** The spec lists:
   - goal and the charter
   - **files it may change** (allow-list) and files it must not touch
   - the exact tests that must pass, with names and assertions
   - what is out of scope
   - the pre-assigned Alembic revision, if it needs one
2. **Implement (Haiku).**
   - Works in a worktree on branch `brain/<ws-id>`, from the tip of `feat/brain-upgrade`.
   - Writes tests first.
   - Runs tests on its **own** DB `cryptonite_ci_<ws>` in `ci-pg-budget` and its own Redis DB index in
     `ci-redis-budget`, so parallel agents never share state.
3. **Diff guard (Opus, mechanical).** `git diff --name-only` must be a subset of the allow-list. Any change
   outside it is reverted or the spec is amended deliberately. Ruff runs on the changed files.
4. **Review (Opus).** A correctness and charter checklist. Findings go back to the **same** Haiku agent
   (resumed with `SendMessage`, so its context is kept), with at most 2 rounds. If round 2 fails, Opus fixes
   it or escalates to the operator.
5. **Merge** into the integration branch `feat/brain-upgrade`. Later workstreams rebase onto it.

### Wave gate (before anything goes to `main`)

1. On `feat/brain-upgrade`, run everything:
   - the full `pytest tests -q` and `-m destructive` suite
   - web `pnpm typecheck && pnpm lint`, plus Playwright on :3100
   - the Brain eval set with a stubbed or free model
2. Opus **drift review** of the whole wave diff against the charter (`Reality Checker`).
3. One PR `feat/brain-upgrade` → `main` per wave. Main merges are blocked for Claude, so **the operator
   merges** (Opus hands over the `gh api` command).
4. **The operator deploys** (per HANDOFF), taking a DB backup first when the wave has a migration.

### No code conflicts: one writer per hot file per wave

These files are touched by many items, so in any wave each has **one** owning workstream:
- `brain/agent.py`, `brain/toolset.py`, `brain/memory.py`, `brain/widgets.py`
- `gateway/gateway.py`, `tools.py`
- `services/api/cryptonite_api/routes/brain.py`
- the web `OrderCard` component

Workstreams that share a hot file run **in sequence**; disjoint ones run in parallel.

**Alembic revisions are pre-assigned** (046, 047, …) so there is only ever a single head.

---

## 3. Waves

Effort: S ≈ under a day, M ≈ 1–3 days, L ≈ more. The specialist in brackets is the implementer.

### Wave 1: fixes (SOLUSDT incident + quick fixes)

| WS | What | Owns | Runs | Specialist |
|---|---|---|---|---|
| 1A | **Onboarded symbols survive restarts.** Ingest starts from `INGEST_SYMBOLS` ∪ active onboarded `instruments` rows, subscribes them, and runs gap recovery on them. Universe view and Settings reflect it. | `services/ingest/**`, ingest tests | parallel | Backend Architect |
| 1B | **Brain turn robustness.** A held `propose_order` returns "card staged for the operator; do not call again" instead of "denied". `propose_order` capped at 1 per turn. An `OutputInvalid` after a card is staged keeps the card and ends the turn with a short note, not a crash. Prompt wording fixed (language, length, chart rule, check holding before SELL). | `gateway/gateway.py`, `brain/agent.py`, tests | parallel | AI Engineer |
| 1C | **Feed-aware order cards.** `propose_order` checks `UniverseConfig.refusal()`. For a symbol not in the feed, the card becomes "Onboard SOLUSDT first" with an Onboard action, and no mandate form and no TOTP are shown. | `brain/widgets.py`, `brain/toolset.py`, `routes/brain.py`, web `OrderCard` | after 1B | Backend Architect + Frontend Developer (backend first) |
| 1D | **Eval harness and clock.** Fix `brain-3` (shorting must not pass). Runner gains `history`, `must_mention`/`must_not_mention`, `max_requests`, `expect_args`. Add a `## Now (UTC)` header and per-line times in the transcript. Add cases brain-11…13 plus a stubbed `Brain.turn` test for the SELL-suppression path. | `evals/**`, `brain/memory.py`, tests | parallel | Prompt Engineer |
| 1E | **Brain-staged debates.** New `trigger_source = "brain"` that passes the kill-switch, freshness and cooldown parts of the pre-debate gate, plus a daily cap per session (Redis `SET NX` counter). | `routes/agents.py`, `debate_worker/gate.py`, `stage_debate_proposal` in `tools.py` | parallel | Backend Architect |

### Wave 2: foundation (the Brain can see its own system)

| WS | What | Owns | Runs | Specialist |
|---|---|---|---|---|
| 2A | **Migration 046**: read-only views `agent_safe_mandate_health`, `agent_safe_debate_verdicts`, `agent_safe_open_approvals`, `agent_safe_reflections`, `agent_safe_universe`. No HMAC, TOTP or notifier columns. Grants to `cryptonite_agent_ro` only. Security note included. | `infra/migrations/versions/046_*`, tests | first | Database Optimizer (**+ Opus security review**) |
| 2B | **`get_ai_state()` tool + ≤300-token state header.** Covers mandates, room, gates, kill switches, exit-monitor heartbeat, local tier, feed coverage, recent verdicts. Prompt lists the safe views. | `tools.py`, `brain/toolset.py`, `brain/memory.py`, `brain/agent.py` (prompt) | after 2A | AI Engineer |
| 2C | **Market context.** Wire in `NEWS_SENTIMENT`. Add `get_derivatives` (funding, OI, mark), regime, and the cached scanner brief. Add `as_of` to ticker rows. | `tools.py`, `brain/toolset.py` | after 2B | AI Engineer |
| 2D | **Routing hygiene.** Output and thinking caps on `brain.plan`. On Pro failure, a degraded read-only answer on Flash, with a toolset that has no `propose_order` and no debate tool, labelled "degraded". | `brain/routes.py`, `brain/agent.py` | after 2C | AI Engineer |
| 2E | **Card truth.** A BUY card on a PAUSED or HALTED mandate names the stop and is refused before the TOTP code is spent. A used TOTP code says `TOTP_REUSED` and does not count toward the lockout. A SELL closes a holding in a stopped mandate (`AMBIGUOUS_MANDATE` when several hold it). | `routes/ai.py`, `routes/brain.py`, `tests/test_mandate_card_truth.py` | — | — |
| 2F | **System keys on DB 0.** `agent_worker` reads the ingest universe, heartbeats and perp marks through `SYSTEM_REDIS_URL` (Redis DB 0), so `in_feed` and the card feed notes work. | `infra/docker-compose.yml`, `packages/agents/cryptonite_agents/tools.py` | — | — |

### Decisions taken during Wave 2

- **SELL in a PAUSED or HALTED holder mandate.** The SELL is REDUCE_RISK, sized to the holding, and refused as
  AMBIGUOUS when several stopped mandates hold the instrument. It is safe because it only reduces risk.
- **Descoped from 2B:** local-tier health and per-mandate room in `get_ai_state`. Room is checked at approval and
  moves to 3A.
- **Descoped from 2C:** the cached scanner brief.
- **Security hotfix #93 (DuckDB sandbox) merged into Wave 2.** It caps the model-callable DuckDB query in time,
  memory, threads and result size.

### Wave 3: cards and the debate loop

| WS | What | Owns | Runs | Specialist |
|---|---|---|---|---|
| 3A | **Card pre-flight.** A read-only API dry run (`GET …/cards/{widget_id}/preflight`) that plans the card the way approval does, through one shared `_plan_card`: the exit monitor, feed, mandate, holding, duplicate and room checks. The card shows "Would size to …", "Would sell …", "Would be refused: …" (then "Approve anyway…") or the needs-a-mandate line. It has no kill-switch check, so approval still refuses under the kill switch. The approval re-check stays the authority. | `routes/brain.py` (`_plan_card`), web `OrderCard` | first | Backend Architect + Frontend Developer |
| 3B | **Lessons on cards.** `brain/lessons.py` reads the card's instrument from `agent_safe_reflections`: at most three, from the last 90 days, largest \|alpha\| first. It is deterministic, not semantic. The card shows them under "Past lessons (n)". A read error shows no lessons, and no "no lessons" line. Lessons never enter the prompt. | `brain/lessons.py` (new), `brain/agent.py` hook, `brain/widgets.py`, web `OrderCard` | after 3A | AI Engineer |
| 3C | **Debate follow-through.** The `debate_summary` card follows its run to the verdict and the approval state (not the outcome). The session gets one "Debate for …" line per final state (#86 pattern). A Brain debate that the safety gate refuses is recorded as `GATE_REFUSED`. Trace-id linking was descoped. | `services/debate_worker/main.py`, `brain/widgets.py`, `brain_outcomes.py`, `routes/brain.py`, web debate card, Action Inbox | after 3B | Multi-Agent Systems Architect |
| 3D | **Structured rationale.** `OrderProposal` gains thesis, evidence (tool, field, value, as_of), invalidation, horizon and confidence. The card renders them. | `brain/widgets.py`, `brain/toolset.py`, web card | after 3C | AI Engineer + Frontend Developer |
| 3E | **Trusted system lines.** `<system_event>` wrapping for server-written lines, escaping of lookalikes, and a prompt rule that tool text is data. The summarizer keeps order outcomes only from those blocks. | `brain/memory.py`, `brain/agent.py` (rule 6), tests | parallel to 3A | Prompt Engineer (**+ Opus security review**) |
| 3F | **Trade post-mortem.** Migration 047 `agent_safe_trade_timeline`: one row per AI-mandate position of the last 90 days, with the entry (card or debate, and its summary), the debate verdict, the exit price, how it closed (hard or trailing stop, sell card, exit debate, mandate halt, or manual), realized P&L and return, the 24h and 72h outcome, and the reflection. A `get_trade_timeline` tool, and the `/postmortem [SYMBOL]` composer command. | `047_*`, `tools.py`, `brain/toolset.py`, `brain/agent.py` (rule 1), web composer | after 3D | Data Engineer + Frontend Developer |

### Decisions taken during Wave 3

- **The card pre-flight has no kill-switch check.** It covers the sizing, mandate, feed, exit-monitor and holding checks.
  Approval still refuses under the kill switch.
- **Lessons are deterministic, not semantic.** They come from the same instrument, from the last 90 days, largest
  |alpha| first. They are never in the Brain's prompt.
- **Trace-id linking between a Brain turn and its debate is descoped.**
- **A debate card follows the debate to its verdict and approval, not to its outcome.** The 24h and 72h outcome is in
  the trade timeline (3F).
- **Historic exit-monitor blind time is descoped.** It is not stored per position, so the timeline does not show it.
- **Per-mandate room is covered by the card's dry run,** not by `get_ai_state` (see 2B).
- **3D was built in an isolated worktree as the cloud pilot.** The remote option ran locally.
- **`/postmortem` is a composer command,** not a chip.

### Wave 4: measuring quality

| WS | What | Specialist |
|---|---|---|
| 4A | The learner labels **approved Brain cards** (forward return against BTC, stop or target hit first, NOT EXECUTED by reason). Migration 048 if a column is needed. | Data Engineer |
| 4B | Free **grounding check**: every number in an answer or card must trace to a tool result or history. Also flag "filled/placed" with no matching system line. Runs in evals and as a production metric. | Prompt Engineer |
| 4C | **Shadow for the Brain**: an inert `propose_order` stub, with comparison of the tools each model intended to call (card or not, side, allocation bucket). | AI Engineer |
| 4D | Mission Control → Autonomy & Evals: Brain card scores, grounding rate, shadow agreement. | Frontend Developer |
| 4E | Offline **LLM-judge** script (paid; the operator runs it weekly under the budget guard). Calibrate on about 30 hand-labelled turns first. Never in tests. | Prompt Engineer |

### Wave 5: proactive and new features (needs **ADR 0008** approved first)

ADR 0008 "Brain proactive turns" is written by Opus and **approved by the operator** before any code. It
covers:
- the READ-only persona
- the `brain.narrate` role and its cap of about $1/day
- the circuit breaker
- web-feed-only delivery, with no Telegram pings

| WS | What | Specialist |
|---|---|---|
| 5A | **Operator feed.** An outbox consumer with its own cursor, plus an outbox insert for exit-monitor stops. Templates first; narration only for NOT EXECUTED, halts and verdicts. Goes to a pinned "Operator feed" session. | Multi-Agent Systems Architect |
| 5B | **Morning and evening briefings.** Built from the state views; skipped if nothing changed; a Redis lock per date. | AI Engineer |
| 5C | **Alerts set by chat.** An alerts table, a deterministic evaluator (stale-quote aware) and the notifier. A trigger can at most open a normal order card. | Backend Architect |
| 5D | **Memory across conversations.** A `brain_memory` table: explicit operator preferences, editable in the UI, plus embedded session summaries (local embeddinggemma, top 3). | RAG Pipeline Engineer |
| 5E | **Intent routing.** Local triage of each turn; lookups go to Flash, analysis and trades stay on Pro. Shadow-measured before switching. | AI Engineer |
| 5F | **Backlog**, ranked by value: saved-prompt chips, Telegram Brain chat (read-only), strategy comparison widget, pinned insights. **Voice** is deferred. | per item |

---

## 4. Checkpoints that need the operator

1. Approve this plan (and the Haiku/Opus split).
2. At the end of each wave: merge the PR to `main` with the `gh api` command, then deploy (DB backup first for
   Waves 2, 3 and 4).
3. Approve ADR 0008 before Wave 5.
4. Live checks that call a paid model: one Brain chat per wave, plus the weekly LLM judge.
5. Before any real money: turn off `AI_ALLOW_DEV_TOTP` and `ALLOW_DEV_TOTP`.

## 5. Risks and how the plan handles them

| Risk | Handling |
|---|---|
| Haiku on safety-relevant code (gateway, DB grants, order path) | Small specs with **tests written into the spec by Opus**; a strict file allow-list; Opus review plus a security audit on 1B, 2A, 3A, 3E; 2 review rounds, then Opus takes over |
| Parallel agents colliding | One writer per hot file per wave; pre-assigned migrations; separate worktrees, CI databases and Redis DB indexes; integration branch with rebases |
| Drift from the purpose | The charter in every spec; diff guard; a drift review per wave; proactive work blocked behind an ADR |
| CI not running on GitHub | Local wave gate (full pytest, destructive, Playwright, evals) on the integration branch |
| Token and model cost | Implementation on Haiku; Opus only for specs and review; tests never call paid models |
