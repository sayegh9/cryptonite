# AI-First Program: what is built (Phases 0–7)

The plan is [ai-first-blueprint.md](ai-first-blueprint.md). This page describes what actually runs on `main`
(Alembic head `047`, including Brain upgrade Waves 1–3 as of 2026-10-16). Day-to-day state, open items and evidence
are in [HANDOFF.md](../HANDOFF.md).

The three rules did not change: **AI and UI propose, deterministic rules and risk controls decide,
NautilusTrader executes.** Every order still goes through `GuardedOrderDispatcher`, and the 1–25% equity
clamp is invariant.

## 1. Services

| Service | Image | Profile | What it does |
|---|---|---|---|
| `api` | api | default | REST + WS gateway; AI status, mandates, approvals, research reports |
| `orchestrator` | orchestrator | default | paper engine; mandate risk monitor (5 s), approval sweeper, autonomy gate evaluator (10 min) |
| `agent_worker` | agent_worker | default | Brain copilot and chat personas through the AI gateway |
| `debate_worker` | debate_worker | default | pre-debate gate, triage, lite/full debate, verdict routing by mandate level |
| `learner` | debate_worker | default | single instance: labels decision outcomes, writes reflections (`learner:heartbeat`) |
| `sentiment` | sentiment | default | FinBERT/CryptoBERT on CPU, internal network only, no secrets |
| `research_worker` | agent_worker | `research` | strategy research loop (draft → sandbox backtest → Optuna → OOS guard → review) |
| `strategy_sandbox` | backtester | default | runs model-written strategy code in an isolated container |
| `exit_monitor` | scanner | `monitoring` | live-quote hard and trailing stops for AI-owned positions; **must run** before AI orders |
| `notifier` | notifier | `notifier` | Telegram bot (approvals, TOTP for ADD_RISK) and ntfy for Critical alerts |
| `executor` | executor | `binance_demo` | Binance demo execution; opt-in because it needs a PEM key |

The local model tier is **not** a container: Ollama runs on the host as a systemd service, bound to the
docker bridge only (`172.17.0.1:11434`). Containers reach it as `host.docker.internal` through
`extra_hosts: host-gateway`. It is never exposed on loopback, the LAN or the internet (invariant 8).

## 2. AI gateway (`packages/agents/cryptonite_agents/gateway`, [ADR 0001](adr/0001-ai-provider-layer.md), [ADR 0005](adr/0005-local-tier-and-learning.md))

Every model call goes through one gateway: a route per role (`routes.py`), a price table, a daily budget
guard ($25/day; exits are never blocked), a permission gate on tools (READ / REDUCE / ADD), and a ledger
(`llm_cost_ledger`, `ai_decision_traces`).

- **Routes:** each role has a primary model, an optional fallback, a fallback policy, a priority and
  output/thinking limits. `ai_model_routes` rows override the code defaults at runtime. Trade-decision
  roles (`debate.verdict`, `debate.cro`, `exit.verdict`) are protected and fail closed.
- **Brain routes** (`brain/routes.py`): `brain.plan`, the planner that may propose orders, fails closed and is
  capped at 8000 output tokens and 4096 thinking tokens. `brain.chat`, for lighter turns, is capped at 4000 and
  1024, and is also the read-only degraded answer when the planner's model fails.
- **Fallback policies:** `DEGRADE` switches to the fallback model when the primary fails; `FAIL_CLOSED`
  never switches.
- **Local tier** (`local_tier.py`): probes Ollama `/api/tags`, publishes `local_tier:health` to Redis,
  and skips a down or un-pulled local primary immediately (no timeout stall). `/api/v1/ai/status`
  reports it and the web shows a banner.
- **Thinking off for Ollama:** `thinking_budget=0` is sent as `reasoning_effort: "none"`.
- **Per-turn tool caps:** `stage_debate_proposal` (starts paid work) at most once per turn; `propose_order` (one
  card per turn; the held call is answered "Held for approval", a later one "Refused").
- **Distillation logging:** the prompts of `triage`, `tagging` and `summarize` are stored in
  `ai_decision_traces.input` (migration 029), at most 20,000 characters.

- **Shadow mode (F2, `shadow.py`, migration 036):** a route with `shadow_model` and
  `shadow_sample_rate` > 0 replays that share of its calls on the shadow model *after* the primary
  returned, as a background task. The shadow gets the same prompt, instructions, output contract and
  history, but only READ tools without side effects (never `stage_debate_proposal`, never REDUCE/ADD
  tools); its answer goes to `ai_shadow_results` and is never acted on. It cannot delay, change or fail
  the primary. Protected roles may have a shadow. Pinned calls (evals, operator model overrides) and
  gateways without a telemetry pool are not shadowed; at most 4 shadows run at once per process.
  Budget: shadow calls are ledgered as `shadow:<role>` and count toward the daily cap; the guard skips
  them first, from the 70% threshold on, and while spend is unknown. Local (Ollama) shadows are free;
  paid shadows run only with `system_settings` `AI_SHADOW_ALLOW_PAID = true`. Structured roles record
  agreement on the key field (`worth_debate`, `recommendation`, `conviction_level`).
  `GET /api/v1/ai/shadow?role=&days=7` reports per role and per shadow model: agreement, p50/p95
  latency, errors, skips, cost and, once the setup's decision is labelled (24h, linked through the
  `decision_trace_id` the debate worker now passes as trace parent), outcomes scored with
  `score_decision`. Mission Control → Autonomy & Evals shows it read-only. Routes are edited in SQL:

  ```sql
  INSERT INTO ai_model_routes (role, shadow_model, shadow_sample_rate) VALUES ('triage', 'ollama:qwen3.5:4b', 0.25)
  ON CONFLICT (role) DO UPDATE SET shadow_model = EXCLUDED.shadow_model,
      shadow_sample_rate = EXCLUDED.shadow_sample_rate, updated_at = now();
  -- off: UPDATE ai_model_routes SET shadow_sample_rate = 0 WHERE role = 'triage';
  -- paid shadows (JSONB value): INSERT INTO system_settings (key, value) VALUES ('AI_SHADOW_ALLOW_PAID', 'true')
  --     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
  ```

  Gateways reload routes every 30 s, so no restart is needed.

Production routes on the workstation:

| Role | Primary | Fallback (DEGRADE) |
|---|---|---|
| `triage` | `ollama:gemma4:12b-it-qat` | `google:gemini-2.5-flash-lite` |
| `summarize` | `ollama:gemma4:12b-it-qat` | `google:gemini-2.5-flash` |
| `embed` | `ollama:embeddinggemma` (768-d) | none |
| `tagging` | `google:gemini-2.5-flash-lite` | `google:gemini-2.5-flash` |
| chat, debate, exit, reflection, strategy | Gemini Flash / Pro (code defaults) | per `routes.py` |

The bake-offs behind these choices (triage, summarize, tagging, embeddings) are in HANDOFF.md.

## 3. Mandates, autonomy and approvals (Phase 5, [ADR 0004](adr/0004-mandates-and-autonomy.md))

- **No AI entry executes without an ACTIVE mandate** that allows its instrument; a SELL that closes a holding may run
  in a PAUSED or HALTED mandate (REDUCE_RISK passes the mandate check). Mandates trade under
  their own instance `AI_MANDATE_<id>`; entries are sized as a percentage of the mandate budget, capped
  at the room left under the position limit (what the mandate already holds counts), then pass the
  mandate check, then `GuardedOrderDispatcher`.
- **Hard stops:** mandate equity against its high-water mark. At the stop, the mandate is HALTED at L1
  and its own positions closed; at half the stop it drops one level. A 20% account backstop latches the
  PAPER kill switch and halts every mandate.
- **Autonomy levels:** L1 Copilot (every ADD_RISK needs approval), L2 Supervised (ADD_RISK waits out a
  veto window, only inside active hours), L3 Autonomous (digest only). The system proposes promotions when the gate passes; only the operator confirms them, with
  TOTP. Demotion is automatic.
- **Gates:** closed-trade count, days at level, breaches, and verdict calibration (ECE, needs ≥ 20
  labelled decisions, fails closed below that). `scripts/gate_status.py` prints each mandate's checklist.
- **Approvals:** ADD_RISK times out to *no*; REDUCE_RISK times out to *proceed*. Approving, adding risk
  to a mandate and confirming a promotion need a human operator session with TOTP.
- **Notifier:** Telegram long polling with a chat allow-list; ADD_RISK approval needs the operator's
  TOTP sent to the bot. Critical alerts also go to ntfy. Secrets live in `.env.notifier`, separate from
  the API's `.env`.

## 4. Learning loop (Phase 7, [ADR 0005](adr/0005-local-tier-and-learning.md))

- **Outcomes** (`services/debate_worker/outcomes.py`, migration 028): the learner labels every debate
  decision at 1h, 4h, 24h and 72h with signed return, MFE and MAE from the analytics lake.
- **Reflections** (`reflection.py`): written from the 24h outcome with alpha against BTC, embedded with
  the model recorded per row (migration 027). Lesson recall compares only same-model vectors and ranks
  by outcome, then distance.
- **Calibration** (`packages/execution/cryptonite_execution/calibration.py`): reliability bins and ECE
  for `debate.verdict` confidence; shown as a reliability chart in Mission Control → Autonomy & Evals,
  and used by the L3 gate.
- **Distillation** (A7.4): `scripts/build_distill_triage.py` builds training data,
  `scripts/distill_triage_train.py` trains a QLoRA adapter for Gemma 4 12B on the host GPU, and
  `scripts/distill_triage_eval.py` certifies it against the base model on held-out data. Training runs
  outside Docker in `~/cryptonite-train` (no NVIDIA container runtime on this host). A student only
  replaces the base model if it is certified. `scripts/distill_triage_export.py` merges a certified adapter and imports it into
  Ollama. Status (2026-10-04): the first triage student is certified (100% on both holdouts against the
  base's 98.6% / 96.2%), but Ollama 0.33.2 cannot import Gemma 4 12B's unified architecture, so
  production still runs base Gemma 4 until the Ollama upgrade.

## 5. Market context

- **News sentiment:** the debate's Sentiment & News Analyst calls `get_news_sentiment` (RSS headlines
  scored by FinBERT in the `sentiment` service). Before Phase 6 it had no news source.
- **Market brief:** built once per candidate by the scanner, cached in Redis and shared by every debate
  role; the pre-debate gate (kill switch, daily loss, headroom, freshness, cooldown) runs before any
  model call.
- **History:** 120 days of BTCUSDT and ETHUSDT 1m and 1h bars in the Nautilus catalog and the lake
  (`scripts/backfill_history.py`; repairs with `scripts/repair_catalog_series.py`).

## 6. Research loop (Phase 4, [ADR 0003](adr/0003-strategy-research-loop.md))

Ideas are queued on `research:queue`. The worker drafts code (Flash), validates it against an allow-list,
backtests it in `strategy_sandbox`, optimizes parameters with Optuna on the in-sample window only, rejects
overfitting on out-of-sample before paying for a Pro review, and stores the report in `research_reports`
(migration 031) through `POST /api/v1/research/reports`. The daily digest reads that table.

## 7. Security hardening (backlog LE-1 … LE-10)

- **Redis requires a password** (`REDIS_PASSWORD` in `.env`); every client URL carries it. Host tools
  need `redis-cli -a`.
- **Database roles:** `cryptonite_agent_ro` (read-only, model-callable SQL), `cryptonite_ai_telemetry`
  (ledger and traces), `cryptonite_api`, `cryptonite_notifier`, and `cryptonite_backtester` (migration
  030, only the backtest tables).
- **Images** install from the lockfile (`uv sync --frozen`), so deployed versions match the tests.
- **Tests** never call paid models (the test config unsets the Gemini key) and run against a throwaway
  database, never the dev DB.

## 8. Database tables added by the program

| Migration | Tables / columns |
|---|---|
| 022–024 | `ai_model_routes`, `llm_cost_ledger`, `ai_decision_traces`, eval runs, Brain sessions |
| 025 | `ai_mandates`, `ai_mandate_equity`, `ai_autonomy_events`, `execution_commands.mandate_id` |
| 026 | `ai_approvals`, `notifications_outbox`, `debate_runs.mandate_id` |
| 027 | `reflections.embedding_model` |
| 028 | `ai_decision_outcomes` |
| 029 | `ai_decision_traces.input` |
| 030 | role `cryptonite_backtester` |
| 031 | `research_reports` |
| 036 | `ai_model_routes.shadow_model`, `ai_model_routes.shadow_sample_rate`, `ai_shadow_results` (F2) |
| 046 | `agent_safe_mandate_health`, `agent_safe_debate_verdicts`, `agent_safe_open_approvals`, `agent_safe_reflections`, `agent_safe_risk_latches`, `agent_safe_daily_loss` (read-only views for the Brain, `security_barrier`) |
| 047 | `agent_safe_trade_timeline` (read-only view for the Brain, `security_barrier`: one row per AI-mandate position opened in the last 90 days; the entry, its debate, the close and the outcome). Indexes `ix_execution_commands_target_created` and `ix_paper_orders_instance_instr_created`. `debate_runs.status` gains the value `GATE_REFUSED` (no schema change) |

## 9. Redis keys the program added

| Key | Writer | Meaning |
|---|---|---|
| `local_tier:health` | gateway probe | Ollama up/down and pulled models |
| `learner:heartbeat` | learner | learner alive |
| `exit_monitor:heartbeat` | exit monitor | stops are protected; Brain order cards are refused without it |
| `research:queue` | Brain tool / API | pending research ideas |
| `brief:*` | scanner | cached brief per candidate |

`agent_worker` reads the system keys (ingest universe, heartbeats, perp marks) from DB 0 through
`SYSTEM_REDIS_URL`, because its own `REDIS_URL` points at DB 1 (sessions and rate limits).
