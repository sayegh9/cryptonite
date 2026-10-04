# HANDOFF

## ▶ Resume here (2026-10-04, Linux workstation)

**Where things are.** The stack runs on the Linux workstation (the Mac move below is done). Work is on
`feat/ai-first-phase6`, which holds Phases 6–7 and backlog LE-1…LE-10 and includes the ingest fix.
Alembic head is `031`. What the system now is: [docs/ai-first-program.md](docs/ai-first-program.md).
Local model operations: [docs/runbooks/local_ai_tier.md](docs/runbooks/local_ai_tier.md).

**Pull requests** (`main` merges stay with the operator):
- `sayegh9/cryptonite#6` `feat/ai-first-phase6` → `feat/ai-first-phase0`: Phases 6–7, LE-1…LE-10, the `scan_runs` fix.
- `sayegh9/cryptonite#5` `fix/ingest-recovery-open-kline` → `feat/ai-first-phase0`: already contained in #6.
- `sayegh9/cryptonite#4` `feat/ai-first-phase0` → `main`: Phase 5 (and, once #6 merges, Phases 6–7).

**Production AI routes:** `triage` and `summarize` on `ollama:gemma4:12b-it-qat` (DEGRADE fallback
Flash-Lite / Flash), `embed` on `ollama:embeddinggemma`, everything else on Gemini. Models on disk:
`gemma4:12b-it-qat`, `qwen3.5:9b` (bake-off runner-up), `embeddinggemma`; `qwen3.5:4b` was deleted.

**Gates last run (2026-10-04):** pytest 804 passed / 0 failed on the throwaway DB; Playwright 26/26.
The throwaway-DB test command (Phase 0 section below) needs the Redis password in the shell first:
`REDIS_PASSWORD=$(grep '^REDIS_PASSWORD=' .env | cut -d= -f2-)`. Run Playwright with the live API stopped.

**Waiting on time, not work:**
- Gate 5: 14 days of L1 paper trading with 0 breaches and working approvals (earliest about 2026-10-15).
- L2 candidacy of `BTC-ETH spot L1`: earliest 2026-10-31, needs 30 closed trades
  (`scripts/gate_status.py`).

**Fixed 2026-10-04 (branch `fix/mandate-headroom-sizing`), deploy before approving more entries:**
- **Approved entries were refused as mandate breaches.** The operator approved two debate entries on Telegram and one Brain card on the web (all BTC buys). The `BTC-ETH spot L1` mandate already held about 8% of its budget in BTC with a 10% limit, and an entry was sized at its full allocation (5%) without counting the existing position, so the mandate check refused each one as `MANDATE_POSITION_PCT_EXCEEDED` and recorded a `BREACH` (3 events, 11:01, 11:31, 11:51 UTC). Now the orchestrator sizes an entry down to the room left under the position limit and the budget (`max_entry_quantity`, same worst-case notional as the check, 1% margin); with less than 1% of the budget left it refuses with `MANDATE_POSITION_LIMIT_REACHED`, which is not a breach. The debate router asks only for the room that is left (no approval at all when there is none), and a Brain card approval at the limit gets a 409 with the reason.
- **The 3 recorded breaches stay** (audit rows). They fail the L2 gate's "0 breaches" check and Gate 5 until the operator decides otherwise.
- **Order cards** no longer show the model's quantity (a card read "BUY 0.06 BTC" next to "5% of mandate"); the size shown is the allocation that is approved. `propose_order` no longer takes a quantity.
- **Backtest equity:** the spot account report has one row per currency; every row's total was used as equity, so runs ended on the BTC row and showed -100% return and 100% drawdown. Equity is now USDT plus the base asset at the last fill price. The two old `ema-cross` runs keep their stored (wrong) metrics until re-run.
- Deploy: rebuild and restart `orchestrator`, `debate_worker` (and `learner`, same image), `api` and `agent_worker`.

**Live checks done by the operator (2026-10-04):** Telegram approve works (3 approvals), and an L1 approval expired as *no* (07:01 UTC).

**Waiting on the operator:**
- Telegram live checks still open: foreign chat ignored; veto; kill-switch Critical alert on Telegram and
  ntfy (steps under Phase 5 below).
- Merges: #6, #5 into phase0; #4 into `main`.
- Sudo items: `sudo apt install python3.12-dev` (Triton compiles during training); the Ollama v0.35.1
  install (`~/Downloads/ollama-v0.35.1/README-resume.txt`; also unblocks deploying the distilled triage model, A7.4); optional Tailscale; optional NVIDIA Container
  Toolkit (only if training should move into Docker).

## Moving to the Linux workstation (2026-09-30, done 2026-10-01; kept for reference)

**Git is the source of truth.** Every branch is pushed; nothing lives only on the MacBook. On the workstation, **clone fresh**; don't copy the Mac folder: `node_modules`, `.next` and the venvs are macOS builds.

1. **Tools:** Docker Engine + compose plugin (later the NVIDIA Container Toolkit for Phase 6), git, `gh` (`gh auth login`), Node + pnpm@10, uv, graphify, and Claude Code. Copy `~/.claude/skills/` (agency-* skills) and re-install the plugins (agent-skills etc.).
2. **Clone:** `git clone https://github.com/sayegh9/cryptonite && git checkout feat/ai-first-phase0`.
3. **Copy these from the Mac over SSH/Tailscale, never through cloud storage or chat** (they are git-ignored secrets and state):
   - `.env`, which holds the Gemini/Binance keys, the TOTP AES key and `CRYPTONITE_AGENT_API_TOKEN`;
   - `infra/security/binance_demo_private_key.pem`;
   - `data/`, about 100 MB: `nautilus_catalog`, `analytics_lake`, `backtest_artifacts`. Alternatively re-backfill it (LE-1).
   - the dev database dump: `docker exec infra-postgres-1 pg_dump -U cryptonite -Fc cryptonite > cryptonite.dump`, restored with `pg_restore -U cryptonite -d cryptonite --clean`. It holds operator users, TOTP enrollment, paper history, the machine-token hash and the AI ledger. Redis is disposable.
4. **Bring up and verify:**
   1. `docker compose -f infra/docker-compose.yml build`. This is the ~1–1.5 GB dependency download, now on the workstation.
   2. `up -d`.
   3. `alembic upgrade head`.
   4. Run the **merged-tree suite on the throwaway DB** (the command is in PR #2); it has not run since PR #1 merged.
   5. In `apps/web`: `pnpm install`, `npx playwright install chromium`, `pnpm build`, then `npx playwright test`.
   6. `graphify update .`
5. **Claude context:** the session transcript and memory are keyed to the Mac path, so they won't follow. Everything needed is in this file, `AUDIT_STATUS.md` (backlog LE-1…LE-10), `docs/ai-first-blueprint.md` and ADRs 0001–0003. To keep the memory note, copy `~/.claude/projects/<mac-slug>/memory/` into the new project's slug dir.
6. **Starter prompt:** "Read HANDOFF.md 'Resume here' and verify steps 1–4 on this workstation, then continue: PR #2 merge readiness, the local half of Phase 5 when its cloud PR lands, LE-2 (exit monitor), and Phase 6 on this GPU."

**Open items when the move happens:**
- PR #2 (`feat/ai-first-phase0` → `main`) is open and needs the merged-tree suite before merging.
- Phase 5 runs in a cloud session on `feat/ai-first-phase5`, with a PR back to `feat/ai-first-phase0`.
- Branch `claude/elated-shirley-5a992d` (one ingest commit from an earlier session, never merged) is now pushed; decide whether to merge or drop it.

> **Deferred backlog (LE-1 … LE-10)**, agreed for later: see [AUDIT_STATUS.md](AUDIT_STATUS.md#deferred-backlog-marked-for-later-2026-09-29).

## AI-First Program — Phases 6–7 (workstation, 2026-10-03 … 10-04): local tier and learning

Branch `feat/ai-first-phase6` (from `feat/ai-first-phase0`, includes the ingest fix of PR #5).

**Workstation setup (done by the operator, needs sudo):**
- One Ollama: the snap install was removed. The systemd service runs `/usr/local/bin/ollama` **0.33.2**.
- `/etc/systemd/system/ollama.service.d/cryptonite.conf`: `OLLAMA_HOST=172.17.0.1:11434` (docker bridge only), `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_NUM_PARALLEL=2`, `OLLAMA_CONTEXT_LENGTH=8192`, `After=docker.service`.
- Verified: reachable from containers on `infra_default` and `infra_agents_net`; refused on loopback and the LAN address (invariant 8).
- On the host, the CLI needs `OLLAMA_HOST=172.17.0.1:11434 ollama ...`.
- Upgrade to v0.35.1 is **paused**: GitHub downloads crawled. The partial file and resume/install steps are in `~/Downloads/ollama-v0.35.1/README-resume.txt`. Not blocking: every model below runs on 0.33.2.

**Models pulled** (operator-approved): `qwen3.5:9b` 6.6 GB, `gemma4:12b-it-qat` 7.2 GB, `embeddinggemma` 0.62 GB (768-d). `qwen3.5:4b` (3.4 GB) was pulled for the bake-off and deleted on 2026-10-04 (weakest and slowest). Sentiment weights (CryptoBERT, FinBERT) are in `~/models/hf`, outside the repo; FinBERT ships only as a pickle `.bin` with no stated license.

**A6.1 (done):**
- `gateway/local_tier.py`: health probe of Ollama `/api/tags`, published to Redis `local_tier:health`.
- Fast fallback for DEGRADE routes whose local primary is down or not pulled. FAIL_CLOSED, protected and pinned models never switch.
- `/ai/status` gains a `local_tier` field and banner.
- Compose: `OLLAMA_BASE_URL` plus host-gateway on api, agent_worker, debate_worker and research_worker.
- Fix: Ollama ignored `thinking_budget=0`, so Qwen3.5 reasoned before every triage answer. Now `reasoning_effort: "none"` (19 s → 0.7 s for a one-word answer).
- **Drill** (`scripts/local_tier_drill.py`, 2026-10-03, primary `qwen3.5:4b`, fallback Flash-Lite):
  - Healthy: 120/120 local, ~110–135 ms.
  - `systemctl stop ollama`: `tier=DOWN`, every call answered by Flash-Lite in 0.6–1.7 s, no timeout stalls, health published as down (probe error only).
  - `systemctl start ollama`: back to local on the next call (~10 s; first call 5.6 s while the model reloads).

**A6.2 bake-off (first pass, not a certification):** existing triage cases `v1` + `v2` (10 cases) × 3 repeats.

| Model | Agreement v1 / v2 | Structured valid | p50 latency | Cost |
|---|---|---|---|---|
| `google:gemini-2.5-flash-lite` (current) | 100% / 80% | 100% | ~0.8 s | $0.0018 total |
| `ollama:gemma4:12b-it-qat` | 100% / 86.7% | 100% | 0.96–1.5 s | 0 |
| `ollama:qwen3.5:9b` | 100% / 86.7% | 100% | ~1.5 s | 0 |
| `ollama:qwen3.5:4b` | 100% / 86.7% | 100% | ~4 s | 0 |

All four models miss `triage-8` (an EMA cross with reward-to-risk 2.46, labelled not worth a debate; the label is arguable). **Next:** a larger labelled triage set plus `tagging` and `summarize` eval kinds before certifying a winner and routing production traffic locally.

**A6.2 bake-off v2 (2026-10-03).**

New eval sets:
- `triage_v3`: 60 cases, 30 clean / 30 rejects across the 4 hard rules. Generated by `scripts/build_triage_eval_set.py` with the production `build_brief`; labels are recomputed from the rendered text with 0 mismatches.
- `summarize_v1`: 24 cases, the exact `brain.memory.compact` prompt, generated by `scripts/build_summary_eval_set.py`. Scored on keeping every order card's size, status and command id, the operator's limit and the symbols; at most 220 words; no headings.

Runs were 2 repeats each. Flash-Lite cost $0.013 for everything.

| Model | Triage (all) | Triage excl. stale | R:R<1.5 | Both TFs against | Chop | Stale | Summarize | p50 triage / summarize |
|---|---|---|---|---|---|---|---|---|
| `gemini-2.5-flash-lite` (current) | 75.8% | 82.7% | 16/16 | 5/14 | 9/14 | 5/16 | 62.5% | 0.8 s / 0.9 s |
| **`gemma4:12b-it-qat`** | **81.7%** | 90.4% | 16/16 | **12/14** | **13/14** | 4/16 | **100%** | 1.5 s / 2.9 s |
| `qwen3.5:9b` | 80.0% | **91.3%** | 16/16 | 9/14 | 12/14 | 1/16 | 83.3% | 1.5 s / 2.9 s |
| `qwen3.5:4b` | 70.8% | 78.8% | 16/16 | 4/14 | 8/14 | 3/16 | 75.0% | 4.5 s / 7.1 s |

- **Stale:** every model fails the stale rule; it needs "older than 2 bars of 15m" arithmetic. Production never sends stale candidates to triage: `services/debate_worker/gate.py` rejects them first (`STALE_CANDIDATE`, same 2-bar threshold). So "excl. stale" is the production-relevant triage score.
- **Flash-Lite summaries:** they drop order sizes and command ids, and in one case attached an old command id to a different, pending card (a misattribution).
- **Recommendation:** Gemma 4 12B for `triage` and `summarize`, with Flash-Lite as the DEGRADE fallback. It is best or tied on every production-relevant column and the only model with perfect summaries. Its latency is fine for both roles. **Operator decision 2026-10-04: switched.** `triage` and `summarize` run on Gemma 4 12B with DEGRADE fallbacks (`ai_model_routes`). Note that `qwen3.5:4b` is both the weakest and the slowest model here.
- **Tagging:** not evaluated yet; the role arrives with the A6.3 sentiment service.
- **Real-history triage cases:** `--source lake` exists but needs the LE-1 backfill. The lake currently has about 9.5k of the expected 23k 1m bars over 16 days, so the strict resampler rejects most windows.

**A6.3 sentiment (2026-10-04).** `services/sentiment` (CryptoBERT + FinBERT, CPU, internal network only, no secrets; weights in `~/models/hf`, mounted read-only) and the `get_news_sentiment` tool for the debate's Sentiment & News Analyst. Before this, the analyst had **no news source at all**. Tagging eval (`scripts/eval_sentiment.py`; 180 balanced financial-news items + 36 hand-labelled crypto items, macro-F1):

| Tagger | News | Crypto | Latency |
|---|---|---|---|
| FinBERT (service) | 0.727 | 0.80 | 26 ms |
| CryptoBERT (service) | 0.311 | 0.53 | 24 ms |
| `gemma4:12b-it-qat` | 0.761 (3 invalid) | 0.97 (2 invalid) | 0.64 s |
| `gemini-2.5-flash-lite` | **0.828** | **1.00** | 0.77 s |

The news tool uses FinBERT, which is fast and free for 12 headlines per call. CryptoBERT is not used. The `tagging` route stays on Flash-Lite.

**A6.4 embeddings.** Google retired `text-embedding-004` (404), so production reflection embeds and semantic lesson lookup had silently failed. The fix: local `ollama:embeddinggemma` (768-d, matches the column) with model-tagged vectors (migration 027), retrieval that only compares same-model vectors, and `scripts/reembed_reflections.py`, which switches all rows and the route in one transaction. Recall eval (`scripts/eval_embeddings.py`): embeddinggemma hit@1 1.0 and recall@3 1.0, the same as `gemini-embedding-001` (3072-d).

**Phase 7.**
- **A7.1:** migration 028 `ai_decision_outcomes`; the learner service (one instance) labels 1h/4h/24h/72h returns plus MFE/MAE from the lake and writes reflections from the 24h outcome. The old reflection engine was never started. Lessons are ranked by outcome.
- **A7.2:** verdict calibration (ECE in pp) feeds the L3 gate (None below 20 labelled decisions, so it fails closed); there is a reliability chart in Autonomy & Evals.
- **A7.3:** the L2 candidacy is the `BTC-ETH spot L1` mandate at L1 since 2026-10-01 09:15 (earliest L2 2026-10-31; it needs 30 closed trades). Check it with `scripts/gate_status.py`.
- **A7.4:** distillation tooling (data builder, Unsloth QLoRA trainer, certification eval), trained on the host GPU in `~/cryptonite-train`. The trace inputs of distillable roles are now logged (migration 029).
  - **Run 2026-10-04:** QLoRA r=16 on `google/gemma-4-12B-it` (4-bit), 900 balanced examples (lake history older than 14 days + synthetic, holdouts excluded: 0 exact overlaps), 2 epochs, lr 2e-4, 29 min on the RTX 3080 Ti, train loss 0.104.
  - **Certification (greedy, production TRIAGE prompt, `scripts/distill_triage_eval.py`): CERTIFIED.**

    | Holdout | Base agreement (excl. stale) | Student agreement (excl. stale) | Student strict JSON |
    |---|---|---|---|
    | `holdout_lake` (80 real cases, last 14 days) | 98.75% (98.6%) | **100% (100%)** | 100% (base 0%) |
    | `triage_v3` (60 synthetic) | 83.3% (96.2%) | **100% (100%)** | 100% (base 0%) |

    The student also gets the stale-data rule right (8/8 on each set), which every model in the bake-off failed. Caveat: `triage_v3` comes from the same generator as the synthetic training half (different seed), so the real-history holdout is the stronger evidence.
  - Unconstrained, the base answers in `key: value` lines rather than JSON; the eval scores the decision either way (production decodes under Ollama's JSON schema) and reports strict JSON separately.
  - **Deployment:** Ollama 0.33 cannot load this LoRA as an `ADAPTER` for Gemma 4, so `scripts/distill_triage_export.py` merges it into 16-bit weights and imports them with `ollama create -q q4_K_M`. The 12 GB GPU holds one 7 GB model, so the student must also serve `summarize` to replace base Gemma; it is re-certified through Ollama (both eval sets) before any route changes.
  - **Deployment blocked (2026-10-04):** the merge works, but Ollama 0.33.2 refuses to import the merged weights: `unsupported architecture "Gemma4UnifiedForConditionalGeneration"` (the HF 12B checkpoint is the unified audio/vision variant; Ollama only serves it from its own registry build). **Production keeps base `gemma4:12b-it-qat`.** Next try after the Ollama v0.35.1 upgrade: re-run `scripts/distill_triage_export.py` (about 20 min, needs about 48 GB free disk while importing), then run `python -m cryptonite_agents.evals run --set triage_lake_v1` / `triage_v3` / `summarize_v1` against the imported model before routing. A llama.cpp GGUF export would need `build-essential` and `cmake` (sudo) and its own Gemma 4 support check.
  - A failed import leaves its uploaded weights as an orphan blob in `/usr/share/ollama/.ollama/models/blobs` (23.9 GB here). Ollama 0.33's startup prune does **not** remove it; delete it by hand once no manifest references it (done 2026-10-04).
  - The real-history holdout is now a permanent eval set: `triage_lake_v1` (80 cases, 40 worth a debate).

**Operations notes.**
- **Migrate before deploying:** the new orchestrator and API read tables from migrations 027–031.
- **Redis password:** Redis requires `REDIS_PASSWORD` (from `.env`), so host tools need `redis-cli -a`.
- **Backtester role:** the backtester connects as `cryptonite_backtester` (migration 030).
- **Scanner storage:** `scan_runs` stores one digest per date partition, after it had grown to 21 GB.

**Build (LE-7, fixed):** images install from the lockfile (`uv sync --frozen`), so deployed versions match the tests; details in [docs/run-and-deployment-changes.md](docs/run-and-deployment-changes.md#8-update-log-october-2026-linux-workstation-ai-first-phases-57).

## AI-First Program — Phase 5 (cloud session, 2026-09-30): mandates, autonomy, approvals, Telegram

Branch `feat/ai-first-phase5` → PR to `feat/ai-first-phase0` (not `main`). Decisions, evidence and deviations: [ADR 0004](docs/adr/0004-mandates-and-autonomy.md).

| Item | What landed |
|---|---|
| A5.1 mandates | Migration `025` (`ai_mandates`, `ai_mandate_equity`, `ai_autonomy_events`, `execution_commands.mandate_id`). Every AI order (SUBMIT_ORDER, AI target, `ai:` requester) passes the mandate check **before** `GuardedOrderDispatcher`; no mandate = `MANDATE_REQUIRED`. Mandates trade under their own instance `AI_MANDATE_<id>`; orders are sized as % of the mandate budget. Property tests (hypothesis). |
| A5.2 hard stops | Orchestrator risk monitor (5 s): mandate equity vs HWM; ≥ stop → HALTED at L1 + `CLOSE_POSITION` of its own quantity + Critical alert; ≥ ½ stop → one level down; account 20% backstop → PAPER kill switch latched, all mandates halted, AI positions closed. |
| A5.3 approvals | Migration `026` (`ai_approvals`, `notifications_outbox`, `debate_runs.mandate_id`, deletes the `DEBATE_EXECUTION_MODE` setting). ADD_RISK timeout = no; REDUCE_RISK = proceed; L2 veto window proceeds only inside active hours. Sweeper in the orchestrator. Brain order cards and debate approvals record an approval bound to one command. |
| A5.4 notifier | `services/notifier`: Telegram long polling (httpx), chat allow-list, TOTP for ADD_RISK, ntfy for Critical, `send_alert` routed through it. Compose profile `notifier`. |
| A5.5 autonomy | Pure state machine + gate evaluator; system proposals, operator TOTP confirmation, automatic demotion (breach, ½ stop, stop, backstop). |
| A5.6 web | Mission Control tab (mandate cards, approval queue with countdowns, halted/degraded banners, PAPER kill switch), Mandate editor (TOTP when adding risk), Autonomy & Evals. Stores `useMandateStore`, `useAutonomyStore`; event `cryptonite:mandate-updated`. |
| A5.7 | `DEBATE_EXECUTION_MODE` deleted; the debate worker routes verdicts by mandate level (`services/debate_worker/mandate_routing.py`). |

**Behaviour change to know before deploying:** after this lands, **no AI order executes without an ACTIVE mandate** that allows its instrument, including an operator-approved Brain order card (409 `NO_MANDATE`). Create a mandate in Mission Control → Mandate editor first (TOTP). New mandates start at L1 Copilot.

**Also:** approvals, mandate edits that add risk, and promotions need a **human operator session**; the workers' operator-scoped machine token gets 403, and its `/paper/orders` are treated as AI orders (mandate required). `/api/v1/ai` never accepts the dev TOTP `000000` unless `AI_ALLOW_DEV_TOTP=true` (tests only).

**New environment variables** (there is no `.env.example`; never commit real values):
- in `.env` (api): `TELEGRAM_ALLOWED_CHAT_IDS` (the API re-checks it), `NOTIFIER_HMAC_SECRET` (random, ≥ 32 bytes: `python -c "import secrets; print(secrets.token_hex(32))"`), `NOTIFIER_OPERATOR_USERNAME` (the operator whose TOTP approves ADD_RISK from Telegram); optionally `NOTIFIER_DB_PASSWORD` before running migration 026 outside dev (role `cryptonite_notifier`, dev password `notifier_dev_pass`);
- in a **new** `.env.notifier` (the notifier's own file, so it never sees the Gemini/Binance/TOTP secrets): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`, `NOTIFIER_HMAC_SECRET` (same value), `NTFY_URL` (default `https://ntfy.sh`), `NTFY_TOPIC` (a long random topic name), optional `NTFY_TOKEN`. `.env.notifier` is git-ignored.

### Local steps for the operator (not verifiable in the cloud session)

1. **Migrations on a throwaway DB first**, then the dev DB:
   ```
   docker exec infra-postgres-1 psql -U cryptonite -d cryptonite -c "DROP DATABASE IF EXISTS cryptonite_ci" -c "CREATE DATABASE cryptonite_ci"
   docker compose -f infra/docker-compose.yml run --rm -e DATABASE_URL=postgresql://cryptonite:cryptonite_dev_pass@postgres:5432/cryptonite_ci -e ASYNC_DATABASE_URL=postgresql+asyncpg://cryptonite:cryptonite_dev_pass@postgres:5432/cryptonite_ci -e REDIS_URL="redis://:${REDIS_PASSWORD}@redis:6379/15" test sh -c "uv run python -m alembic -c infra/migrations/alembic.ini upgrade head && uv run pytest tests -q"
   ```
   Expected: everything passes except the two known ones (`test_instruments_endpoint`, `test_duckdb_query_p95_latency`). Then `alembic -c infra/migrations/alembic.ini upgrade head` on the dev DB (024 → 026).
2. **Web gates:** in `apps/web`: `pnpm install && pnpm typecheck && pnpm lint && pnpm build && npx playwright install chromium && npx playwright test` (25 specs).
3. **Rebuild images:** `docker compose -f infra/docker-compose.yml build api orchestrator debate_worker exit_monitor notifier`, then `up -d api orchestrator debate_worker`. The orchestrator now runs the mandate monitor and the approval sweeper; the exit monitor protects `AI_MANDATE_*` positions.
4. **Exit monitor (LE-2) must run** before any AI order: `docker compose -f infra/docker-compose.yml --profile monitoring up -d exit_monitor`; check `redis-cli GET exit_monitor:heartbeat`.
5. **Create the first mandate** in Mission Control → Mandate editor (e.g. BTC/ETH spot, budget 10,000, max position 10%, hard stop 20%, active hours `08:00-22:00`); confirm it shows at L1 Copilot.
6. **Live Telegram test (BotFather):**
   1. In Telegram, talk to `@BotFather`: `/newbot`, pick a name and a username ending in `bot`; copy the token into `.env.notifier` as `TELEGRAM_BOT_TOKEN`.
   2. Send `/start` to your new bot from your own account, then read your chat id: `curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates" | python -m json.tool` → `message.chat.id`. Put it in `TELEGRAM_ALLOWED_CHAT_IDS` in both `.env` and `.env.notifier`. (Stop any other `getUpdates` consumer first; long polling allows one.)
   3. Set `NOTIFIER_HMAC_SECRET` (same value in both files), `NOTIFIER_OPERATOR_USERNAME` (`.env`) and `NTFY_TOPIC` (`.env.notifier`); subscribe to the topic in the ntfy app.
   4. `docker compose -f infra/docker-compose.yml up -d api` (re-reads `.env`), then `docker compose -f infra/docker-compose.yml --profile notifier up -d notifier`; `docker compose -f infra/docker-compose.yml --profile notifier logs -f notifier` should show `Notifier running: telegram=True chats=1 ntfy=True`, and Mission Control's "Notifier down" banner disappears.
   5. **Foreign chat ignored:** from a second Telegram account (or a group) message the bot; it must not answer and the notifier logs `Dropped a message from a chat that is not allowed`.
   6. **Approval flow:** create a pending ADD_RISK approval (trigger a debate on an instrument of your L1 mandate, or approve nothing and wait for one), tap **Approve 🔐**, send your 6-digit TOTP; the code message disappears and the bot replies `✅ APPROVED · command … queued`. Repeat with **Veto** on another one. Let a third one expire: it must show EXPIRED (no order).
   7. **Critical:** trip the PAPER kill switch from Mission Control (then reset it); Telegram and ntfy both get the Critical alert.
7. `graphify update .`
8. **Gate 5** starts now: 14 days of L1 paper trading on one mandate with 0 breaches and working Telegram approvals (check `ai_autonomy_events` for `BREACH` rows and `ai_approvals` outcomes).

## AI-First Program — Phase 3 merged (2026-09-29)

"One Brain" (A3.1–A3.5), PR #1 merged into `feat/ai-first-phase0` as `eae3989`:
- **Brain copilot:** Gateway streaming, persistent sessions (migration 024, telemetry role, REVOKEd from `agent_ro`), Brain Home and the Copilot drawer, inline widgets, and the Decision Trace drawer.
- **Order cards:** approval happens only server-side. `POST /api/v1/agents/brain/sessions/{sid}/cards/{id}/approve`:
  - claims the card first;
  - queues one AI-owned `SUBMIT_ORDER`, idempotency key `brain-card:<id>`, sized by the card's allocation;
  - is **refused while the exit monitor heartbeat (`exit_monitor:heartbeat`) is missing**. The exit monitor's hard and trailing stops protect the position; the card's own stop price is advisory.
- **Review:** FAIL. High: an approved card was a bare manual order with no stop and its allocation ignored. Medium: the approval was client-side and replayable. Both fixed in `02753c6`, and the Lows too; see the PR #1 comment.
- **Deferred:** a per-turn cap on staging paid debates, and per-operator trace scoping (backlog LE-8).
- **Deploy:** rebuild `api`, `agent_worker`, `scanner`/`exit_monitor` and `web`, then run the exit monitor (`--profile monitoring`, LE-2) before approving any Brain order card.
- **Gates on the branch:** pytest 557 passed / 2 known failed; Brain, API and exit-monitor suites 44/44; Playwright 20/20. The merged tree has not been tested yet because Docker was stopped. Run the throwaway-DB suite before deploying.

## AI-First Program — Phase 4 complete (2026-09-25)

| Item | Status |
|---|---|
| A4.1 model-written strategies can be backtested | allow-list validator (API 422 on save, 409 vs core slugs) + `strategy_sandbox` container; previously `python_code` was silently ignored |
| A4.2 drafter | Flash drafts code + parameter space; validator/import/sandbox errors fed back (≤ 3 attempts); live: 4/6 ideas load after the import guide |
| A4.3 optimizer | Optuna TPE on the in-sample window only; the model never picks numbers |
| A4.4 overfitting guard + review | OOS trades < 10, OOS return ≤ 0, or OOS Sharpe < 50 % of IS → reject before paying for a Pro review |
| A4.5 queue + digest | `research:queue`, opt-in `research_worker` (`--profile research`), Brain tool `queue_strategy_research`, daily digest on `channel:system_alerts`; web IS-vs-OOS tearsheet deferred until Phase 3 merges |

- Evidence and decisions: [ADR 0003](docs/adr/0003-strategy-research-loop.md). Security audits: the subprocess sandbox FAILED (critical: the child read the worker's secrets, had network, could hang the worker). Replaced by a container. The re-audit FAILED (high: a FIFO froze the worker; medium: the child could stop the server, the queue wait was unbounded, symlinks on the shared volume). All fixed, and the fixes were verified in a real container probe (ADR table).
- **Deploy steps:**
  1. `docker compose -f infra/docker-compose.yml build backtester strategy_sandbox api`, then `up -d strategy_sandbox backtester api`. The sandbox shares the backtester image. The `sandbox_jobs` volume is a 4 GB RAM tmpfs.
  2. Research needs `CRYPTONITE_AGENT_API_TOKEN`, an operator-scoped machine token. Run it with `docker compose --profile research up -d research_worker`.
- **Deployed 2026-09-26** (this Mac): `strategy_sandbox`, `backtester`, `api` and `research_worker` are running on fresh images. The image builds took about 45 min because of slow PyPI downloads, not a hang. Live check: a model-written EMA strategy saved through the API with the research token was backtested in the real sandbox (uid 1001, 9 s, artifacts promoted, manifest `sandboxed: true`, scratch dirs empty). A run over 2026-09-10 fails with `DataGapError`: the 1-minute catalog has gaps there. With about 19 days of BTC/ETH data, the research defaults (90 d in-sample + 30 d out-of-sample) cannot run yet. Use shorter windows or backfill first.
- Research token: `research-worker` in `machine_api_tokens` (scope `operator`, expires 2026-12-24); the plaintext is only in `.env` (`CRYPTONITE_AGENT_API_TOKEN`).
- redis-py 8.1 times out a `BLPOP` at exactly its block time. The research worker now uses `socket_timeout=30` and retries on Redis errors. `debate_worker` blocks 5 s on `XREADGROUP` with the same library; watch its logs for `Timeout reading` when it next starts.
- Gates: pytest **594 passed / 1 failed** on the throwaway `cryptonite_ci` DB (the known `test_instruments_endpoint` reference-data failure). Phase 4 suites: 72/72.
- Not done: web IS-vs-OOS tearsheet (after the Phase 3 merge); a research-report table (reports are JSON files in `data/research_reports/`); Redis `requirepass` and a least-privilege DB role for the backtester, which still connects as `cryptonite`.
- **Rotate** `GEMINI_API_KEY`, `BINANCE_DEMO_API_KEY` and `CRYPTONITE_TOTP_AES_KEY`: the first audit's probe printed partial values into a transcript.

## AI-First Program — Phase 2 complete (2026-09-25)

Phase 3 runs in parallel in a cloud session on `feat/ai-first-phase3-brain` (ownership contract: blueprint §14.5); its PR targets `feat/ai-first-phase0`.

| Item | Status | Commits |
|---|---|---|
| A2.1 pre-debate gate | kill switch / daily loss / headroom / freshness / brief / cooldown before any model call | `72d7c17` |
| A2.2 market brief | built per candidate by the scanner, cached in Redis, shared by all debate roles | `db5de14` |
| A2.3 triage (Flash-Lite) | 20 cases ×2: 0 good setups missed; 5/6 bad avoided by the model, 6/6 with the code staleness check | `72d7c17`, `87b3cca` |
| A2.4 lite debate + merged risk panel | ≥ 5% allocations always get the full review; merged panel = 4/4 like the 4-call panel, 27% cheaper | `72d7c17` |
| A2.5 thinking/output limits | per role in `routes.py`; explicit context caching not done (implicit only) | `72d7c17` |
| A2.6 deterministic exits | live-quote hard + trailing stops (AI-owned instruments only), event-triggered exit debates; replay: 0 missed stops, −81% exit debates | `e02cb3d` |

- Evidence and decisions: [ADR 0002](docs/adr/0002-debate-cost-controls.md). Independent review: FAIL (3 critical: low-conviction large allocations skipped review, stale model stops could auto-close, stops read a price that never updates) → fixed → PASS.
- **Deploy step (databases already at 021, e.g. any other dev DB):** `GRANT SELECT ON agent_safe_positions TO cryptonite_api;` — the debate worker runs as that role and could not read its context, so real debates failed. Applied to this dev DB. Fresh databases get it from 021.
- The exit monitor is opt-in (`--profile monitoring`), runs as `cryptonite_api`, and alerts loudly when it has no fresh quote.
- Gates: pytest **545 passed / 2 failed** on an isolated DB (instruments reference data; `test_duckdb_query_p95_latency` wall-clock flake). Scoped AI/debate suites 93/93.
- Gate 2 (typical day ≤ $6 over 7 days) can only be measured once the stack runs Phase 2 code: restart `debate_worker` (and `scanner`), then read `llm_cost_ledger`.
- Known, deferred: `stage_debate_proposal` bypasses the gate (budget-bounded); price stops ignore perp leverage; the close lookup uses `LIKE` on `idempotency_key` (table scan when a stop is active).
- Next: Phase 4 (research loop). Found while scoping it: **model-written strategies cannot be backtested today** — the runner only loads registry strategies by slug and ignores `strategy_versions.python_code`.

## AI-First Program — Phase 1 complete (2026-09-25)

| Item | Status | Commits |
|---|---|---|
| A1.1 Pydantic AI spike | PASS — Flash 40/40, Pro 15/15; 100% tool chains, 100% valid structured output; Flash p50 1.2 s vs 4.2 s antigravity baseline; $0.20 total. [ADR 0001](docs/adr/0001-ai-provider-layer.md) | `a5896ae` |
| A1.2 gateway + migration 022 | `cryptonite_agents.gateway` (routes, pricing, registry, permission, budget, ledger, pydantic-ai backend) | `8dd3399` |
| A1.3 budget guard | $25/day (D12), priority stop order, exits never blocked, unknown spend blocks optional/interactive | `8dd3399` |
| A1.4 port | chat personas + debate fleet + reflection on the gateway; typed verdicts, invalid → FAILED | `6893c38` |
| A1.5 tool registry + permission gate | tools tagged READ/REDUCE/ADD; SQL tool read-only, single statement, NullPool | `8dd3399` |
| A1.6 evals v1 | `python -m cryptonite_agents.evals run --set v1 --model …`; migration 023 | `dcb968b` |
| A1.7 remove antigravity | removed from pyproject, uv.lock and both worker images (rebuilt; import check passes) | `8dd3399` + next commit |

- Independent review: FAIL → fixed → PASS. The critical finding: the model-callable SQL tool could write the cost ledger (disabling the budget cap) and, in the debate worker, reroute trade-decision models. Fixed with the `cryptonite_ai_telemetry` role, a read-only `cryptonite_agent_ro` (`default_transaction_read_only=on`), and protected routes.
- Gates: pytest **409 passed / 1 failed** on an isolated DB (same `test_instruments_endpoint` reference-data case); AI suites 53/53; no test can call Gemini (conftest unsets the key).
- Dev DB at `023`; LLM cap now 25.00; roles verified (agent_ro cannot write telemetry or read traces).
- **Deploy notes:** set `AI_TELEMETRY_DB_PASSWORD` before running migration 022 outside dev; agent_worker/debate_worker need `AI_TELEMETRY_DATABASE_URL` + `AGENT_TOOLS_DATABASE_URL` (compose has dev values). Without the telemetry URL, spend counts as unknown and only debates/exits run.
- Known, deliberate: spend can be ~10 s stale across processes (no reservation); debate worker handles entry and exit queues in one consumer (bounded by the 90 s model timeout); `exit.verdict` fails closed.
- Next: Gate 1 sign-off, then Phase 2 (cost controls).

## AI-First Program — Phase 0 complete (2026-09-25)

Plan: [docs/ai-first-blueprint.md](docs/ai-first-blueprint.md) (approved; goal §13, assignments §14). Branch: `feat/ai-first-phase0` (from `main`, **not pushed**; `main` fast-forwarded locally to `eebd747`, not pushed).

| Item | Status | Commits |
|---|---|---|
| A0.1 triage of unowned work | Done. Committed in topic groups; the debate approval was fixed (side mapping, refuses HOLD, paper equity, 1–25% bound, atomic) and the new endpoints got operator auth + CSRF; fake scanner presets dropped; scratch files → ignored `scratch/`; `base.py` kwargs change reverted (a no-op on Nautilus rc4, which takes config kwargs in `__new__`) | `f4748f0`..`f0cf84e` |
| A0.2 dev DB at head | Already done on 2026-09-24 (was at 020) | — |
| A0.3 `agent_safe_positions` contract | Migration `021`: open positions only, signed `size`, `unrealized_pnl_pct`/`weight_pct` in percent; applied to the dev DB | `4d87961` |
| A0.4 paper fidelity | Working orders matched against live quotes (limits respected, rested limits fill at their limit, no quote = no fill, strategy orders now fill); latch/exit/retry safety; funding history REST backfill. Independent review: PASS after 4 rounds | `a4eea77`, `26ec837` |
| A0.5 kill switch → PAPER | Operator consoles default to PAPER (+ explicit DEMO panel) | `4063e74` |
| A0.6 freeze autonomous mode | Debate worker ignores `DEBATE_EXECUTION_MODE=autonomous` | `598b4ee` |
| A0.7 gates | pytest **370 passed / 1 failed** on an isolated migrated DB (`test_instruments_endpoint` needs reference instruments a fresh DB lacks); no hang. Web typecheck + lint ✅, Playwright **16/16** ✅ (Research Lab spec fixed `3e53632`) | — |

**Important for anyone running tests:** the compose `test` service points at the **dev DB**, and `test_alembic_roundtrip_downgrade_and_upgrade` downgrades that DB to `base`, wiping operator users and reference data. Run the full suite against a throwaway DB instead:
```
docker exec infra-postgres-1 psql -U cryptonite -d cryptonite -c "CREATE DATABASE cryptonite_ci"
docker compose -f infra/docker-compose.yml run --rm -e DATABASE_URL=postgresql://cryptonite:cryptonite_dev_pass@postgres:5432/cryptonite_ci -e ASYNC_DATABASE_URL=postgresql+asyncpg://cryptonite:cryptonite_dev_pass@postgres:5432/cryptonite_ci -e REDIS_URL="redis://:${REDIS_PASSWORD}@redis:6379/15" test sh -c "uv run python -m alembic -c infra/migrations/alembic.ini upgrade head && uv run pytest tests -q"
```

- **Running stack:** the orchestrator and derivatives_feed containers still run the code from before A0.4 until they are restarted (`docker compose -f infra/docker-compose.yml restart orchestrator derivatives_feed`). On restart, working orders from before are reconciled to CANCELED.
- **Still uncommitted:** `exit_monitor.py` (threshold now in percent to match migration 021; still needs `debate_runs.type` or a switch to `trigger_source`), `.agents/`, the `graphify-out/*.json` ignore rule, graphify cache housekeeping.
- Paper-matching limitations: see AUDIT_STATUS.md.
- **Next: Gate 0 sign-off by the operator, then Phase 1** (A1.1 Pydantic AI spike).

## Paper Trading Readiness — SIGNED OFF (2026-09-24, with the limits below)

Architecture reference: [docs/perpetuals-architecture.md](docs/perpetuals-architecture.md). Branch `feat/perpetuals-phase1` pushed to `origin` for review (PR pending); `main` itself is still unpushed.

**Status of the pre-flight streams**
1. Daily drawdown breaker + UTC rollover — done (`0b536a3`). The orchestrator risk monitor (1 s) trips + persists the PAPER kill switch on a ≥5% drawdown from the day's high-water mark, writes `paper_risk_events`, and alerts on `channel:system_alerts`. The base is re-set at 00:00 UTC.
2. Trade-sidebar kill switch → PAPER — done (`350b249`, `0b536a3`). `/control` trip for PAPER latches the engine via DB sync (≤1 s); `/control` reset (step-up auth) is claimed by the paper orchestrator (expiry + generation checks, refused while exposure is open). Reduce-only perp orders pass the latches; entries do not.
3. Quote coalescing — done (`3738305`, spot follow-up committed right after). Perp and spot quotes are capped at 50 Hz/symbol. Measured live: ~8–10 quotes/s per instrument, newest < 100 ms old, 0 WS reconnects. Unthrottled spot (~160/s) had starved ingest (perp ~2/s, reconnects every ~20 s).
4. Single-owner perps — done (`0b536a3`). Another strategy instance cannot trade a perp position it does not own (`PERP_POSITION_OWNED_BY_OTHER_INSTANCE`).
5. Certification round trip — PASSED on the live stack (postgres, redis, ingest, derivatives_feed, orchestrator, api), BTCUSDT-PERP, 10x ISOLATED:
   - 0.01 BTC rejected by the approved 1% floor (844 < 1,000 USDT); 0.02 BTC short filled → row SHORT / 0.02 / 10 / ISOLATED, liq 92,479.10 > entry 84,408.20.
   - Trade-sidebar trip (the exact SQL `/control` runs) → engine latched → entries `KILL_SWITCH_ENGAGED`; reset refused with the position open (`RESET_REJECTED: open orders=0, open positions=1`).
   - A reduce-only BUY under the latch closed it (row FLAT / 0 / closed_at / realized −1.69); `/control` reset then succeeded and the PAPER row unlatched.
   - Reopened the short, restarted the orchestrator → `Rehydrated 1 open perp positions`. An oversize reduce-only was rejected; a reduce-only BUY 0.02 filled at the live ask (0.0000% drift) → FLAT.
   - Ledger reconciled to the cent: cash 100,000 + gross 0.264 − fees 6.752392 = 99,993.511608 = DB cash = realized; equity = cash.
   - Live gateway check: `/ws/stream` subscribe `perpetuals` → snapshot + live `perp_mark_tick`.
   - Afterwards `PAPER-ACCOUNT-001` was reset to 100,000 via `packages/database/seeds/seed_paper_trading.sql`; certification rows are kept as history.

**Operating limits for the paper test (known, accepted)**
- Perps: BTCUSDT / ETHUSDT only (reference margin tiers, not live Binance brackets). Leverage 1–20; notional ≥ 1% and ≤ 25% of equity per position.
- Perps are manual-only per symbol: one owning instance per position (don't let the AI debate worker trade the same perp).
- Fees: taker 0.1% for perps too (Binance USD-M ≈ 0.05%, so perp PnL is understated). Liquidation clearance 0.5% (> BTC tier-1 MMR 0.4%), so isolated liquidations lose the full margin.
- Fills: MARKET and LIMIT fill immediately at the current bid/ask (limit price not honoured). Hardcoded 60,000 fallbacks apply if a quote is missing (quotes are live now).
- Funding settles only while the feed/orchestrator are up; missed settlements aren't backfilled. Cumulative funding isn't a separate DB column.
- The daily high-water mark resets on orchestrator restart (the kill-switch latch persists).
- The orchestrator bind-mounts the working tree, so it also runs other authors' uncommitted `packages/execution` edits (`demo_pipeline.py`).
- Don't run pytest while the stack runs: outbox relay tests share the DB/Redis and fail (5 failures live; 331/331 with services stopped).
- Ingest after a long outage replays gap recovery in 1-minute REST steps, and closed-bar commits run inline in the WS reader. Expect ~1–2 min of degraded quotes after a restart with a data gap.

**How to run it**: `docker compose -f infra/docker-compose.yml up -d postgres redis ingest derivatives_feed orchestrator api`, then `cd apps/web && pnpm dev` (or `pnpm build && pnpm start`), log in as an operator (users restored from the pre-rebuild backup), open **Trade** in the terminal Panels bar.
- Dev DB rebuilt from migrations at `020` (reference data restored: operator_users, instruments, strategies, strategy_versions). The pre-rebuild backup `cryptonite_pre_rebuild_20260924_182038.dump` (898 MB) is in the session scratchpad under `/private/tmp` — move it somewhere permanent if you want to keep it.

## Current State
- Active branch: `feat/perpetuals-phase1` (perpetual futures Phase 1, paper). Plan: `docs/perpetuals-phase1-blueprint.md`. Roadmap C0–C8 done (user numbering; C4 = dispatcher perp guards / blueprint C8, C5 = perp fill accounting + direction-aware controls / blueprint C7+C9, C6 = perp-aware bridge persistence, C7 = liquidation engine / blueprint C10 minus funding, C8 = orchestrator mark/param/liquidation wiring, C9 = `derivatives_feed` compose service, C10 = funding settlement, C11 = WebSocket perp relay, C12 = web store/HUD/ticket). `aae1b96` (ingest futures stream split) cherry-picked onto the branch. C12.5 fixed web-order routing to the paper orchestrator; C13 added the Playwright spec. Next: decide the `/paper/*` default-environment question (Active Work), bring the dev DB to head, and fix the stale Research Lab e2e spec
- `main` @ `c63576e`, 14 commits ahead of `origin/main`, NOT pushed. Working tree NOT clean: 28 modified, 212 deleted, 20 untracked (backend packages/services, docs, graphify cache, `exit_monitor.py`, 015's LLM budget change 10→18 USD)
- Graph: built at `1100fa2`; run `graphify update .` after committing
- Gates (2026-09-24, Claude Code):
  - Web at C13: typecheck ✅ · lint ✅ 0 errors / 0 warnings · `pnpm build` ✅ · Playwright (`cd apps/web && npx playwright test`, needs `pnpm build` first because `webServer` runs `pnpm start`): 13 passed / 1 failed. `perpetuals-cockpit.spec.ts` 2/2, 3/3 repeat runs green. The failure is pre-existing: `research_lab.spec.ts` expects "RSI Period:", which no longer exists anywhere in `apps/web/src`. Dev-server smoke test (API offline): Trade sidebar renders the HUD and ticket; the PERP toggle, 10x chip/slider, and reduce-only validation work; no React/hydration errors; only leverage + margin mode persist
  - Backend at C12.5 (committed `paper.py`): pytest ✅ 314 passed / 0 failed, 12 deselected (312 at C12 + 2 environment-routing tests)
  - C3 live smoke test (2026-09-24): BTCUSDT/ETHUSDT mark ticks and open interest cached from the public Binance endpoints
  - Backend on **HEAD** (clean checkout): ☐ not re-run since `c63576e` committed the orchestrator `server.py` change the old hang depended on
  - Migrations: `alembic upgrade head` ✅ 001→020 on a fresh database; 020 downgrade/upgrade round trip ✅; 020 constraints exercised (spot SHORT, spot margin mode, spot leverage, CROSS with isolated margin, non-perp funding rows all rejected)
- Pytest runs via `docker compose -f infra/docker-compose.yml run --rm test uv run pytest tests -q`; the `test` service mounts the working tree, so it tests uncommitted code too
- Alembic config lives at `infra/migrations/alembic.ini` (not `infra/alembic.ini`)

## Known pre-existing issues
- Dev DB `cryptonite` is stamped at `013`, but its 014–016 tables were created by hand (`run_migration.sql`). Its `agent_safe_positions` view (`size`, `weight_pct`) differs from migration 014's, so `alembic upgrade head` against it will fail at 014 (`CREATE OR REPLACE VIEW` cannot drop columns). Recreate it from migrations or reconcile the view before upgrading. The test suite does not need 017–020 applied yet
- `agent_safe_positions` has three incompatible contracts: the dev DB view, migration 014, and the consumers `exit_monitor.py` (`unrealized_pnl_pct`) and `debate_worker` (`size`)
- `test_benchmarks.py::test_duckdb_query_p95_latency`: wall-clock threshold; flaky under Docker VM load
- `test_100k_bars_peak_rss_benchmark` headroom is thin in the full suite (573–648 MB vs 700 MB)

## Active Work / Blockers
- Resolved (C12.5): web manual orders are enqueued as `PAPER` / `PAPER-ACCOUNT-001`, so the paper orchestrator claims them (verified on a migrated DB with the orchestrator's own claim query).
- DECISION PENDING (other author's uncommitted `routes/paper.py`): it changes `_enqueue_command`'s **defaults** to `PAPER` / `PAPER-ACCOUNT-001`. Manual orders (C12.5) and the /paper kill switch (pre-flight fix) are now explicitly PAPER. The default change would additionally move `DEPLOY`/`PAUSE`/`RESUME`/`STOP`/`CLOSE_POSITION`/`CANCEL_ORDER` from the DEMO executor to the orchestrator (which claims all of them) — owner: unassigned
- The terminal's `KillSwitchPanel` (`/control/kill-switch/*`) still targets `DEMO` / `DEMO-ACCOUNT-001` (hard-coded in the component), so from the Trade sidebar it does **not** stop the paper engine. Use the Portfolio console's kill switch (`/paper/kill-switch`) for paper, or point the panel at PAPER — owner: unassigned
- Perpetuals C1: migration `020` (SHORT side for perp rows, leverage/margin columns, `funding_rates`) and `cryptonite_execution.derivatives` models. `chk_paper_position_margin_mode` still allows NULL on perp rows because `EventBridgeWorker` doesn't write `margin_mode`; tighten in C9
- Perpetuals C2: `cryptonite_execution.margin`, pure functions for IM, tiered MM, uPnL, exact liquidation price (isolated + cross, tier chosen self-consistently at the trigger notional), margin ratio, and liquidation distance (a fraction, 0.05 == 5%). BTCUSDT/ETHUSDT brackets are a **reference snapshot** of Binance's published tables, not live data. `cum` is derived from tier floors and MMRs. Replace with `GET /fapi/v1/leverageBracket` in C11 before any live use
- Perpetuals C3: `services/scanner/cryptonite_scanner_service/derivatives_feed.py` (`python -m cryptonite_scanner_service.derivatives_feed`; env `PERP_SYMBOLS`, default BTCUSDT,ETHUSDT).
  - Mark ticks go to `perp:mark:{symbol}` (15s TTL) and `channel:perp_mark_ticks`.
  - Open interest is polled every 30s into `perp:oi:{symbol}` (90s TTL).
  - Funding settlements are detected from `T` rollovers and written to `funding_rates` (`STREAM_SETTLEMENT`, deduplicated).
  - Not yet wired: no compose service, and `StreamHub` does not forward `channel:perp_mark_ticks` (C11). Settlements missed while disconnected need REST backfill (C4).
  - Binance USD-M market streams only deliver on `wss://fstream.binance.com/market/...`; the legacy `/ws` and `/stream` paths connect but stay silent. `services/ingest/cryptonite_ingest/stream.py` still uses the legacy futures URL for klines/bookTicker (flagged as a separate task).
  - The untracked `services/ingest/cryptonite_ingest/worker.py` it replaces was removed from the tree; a copy is kept outside the repo in the session scratchpad.
- Perpetuals C4: `GuardedOrderDispatcher` perp branch (`risk.py`).
  - Leverage 1–20 (default 1) and margin mode ISOLATED/CROSS (default CROSS). Spot rejects leverage > 1, `margin_mode`, and `reduce_only`.
  - Reduce-only: `REDUCE_ONLY_NO_POSITION`, `REDUCE_ONLY_INVALID_DIRECTION`, `REDUCE_ONLY_OVERSIZE`. The size cap also subtracts resting reduce-only orders. Reduce-only orders skip the conviction multiplier, the 1% floor, the concentration check, and margin reservation.
  - Entry orders: 1% of equity minimum notional (`ORDER_BELOW_MIN_NOTIONAL`), 25% cap on |position + order| × mark, and initial margin + taker fee reserved on the exposure the order opens.
  - Equity = cash + spot longs at bid + perp uPnL at mark.
  - C5 replaced the C4 interlock. `perp_trading_enabled` now defaults to True and acts as a kill flag (`PERP_TRADING_DISABLED` when False).
- Perpetuals C5: signed-quantity perp fill accounting in `GuardedOrderDispatcher._handle_perp_fill`.
  - Open/add uses a weighted average entry; reduce/close realizes PnL.
  - A flip is a close leg followed by an open leg at the fill price. A reduce-only overfill never opens the opposite side.
  - Cash moves only by realized PnL and fees. Initial margin moves from the order reservation into `locked_margin` and is released pro rata. `available_cash` = cash − reserved − locked margin.
  - Direction-aware controls:
    - `RESET_SWITCH` counts perp positions of either sign (`open_position_count`).
    - `CLOSE_POSITION` (engine + orchestrator) closes perps with an opposing reduce-only order (`flatten_perp_position`).
    - `PAUSE` cancels perp entries on both sides and keeps reduce-only exits (`is_entry_order`).
    - `dispatch()` now records `instance_id`, so targeted PAUSE/STOP actually match orders; before this they cancelled nothing.
- **Perp gaps before production use (in priority order):**
  1. ~~Bridge persistence spot-only~~ — resolved in C6 (below). Still open: tighten `chk_paper_position_margin_mode` to NOT NULL for perps (the bridge now writes it; left for a later migration because C6 had to stay within 020's constraints). `paper_trades` has no realized-PnL column; realized PnL is on the position row only.
  2. ~~No liquidation / funding~~ — liquidation since C7/C8, funding settlement since C10.
  3. ~~Orchestrator mark/param wiring~~ — resolved in C8; `derivatives_feed` runs as a compose service since C9.
  4. The dev DB is still at 013, so `funding_rates` (020) doesn't exist there. Each funding settlement the feed detects will fail its insert, drop that row, and make the mark stream reconnect once. Bring the dev DB to head (see Known pre-existing issues) before relying on funding history.
- Perpetuals C6: `EventBridgeWorker._persist_perp_fill` (`packages/execution/cryptonite_execution/bridge.py`; there is no orchestrator `bridge.py`).
  - Perp `ORDER_FILLED` events carry the dispatcher's post-fill snapshot (`dispatcher.fill_event_fields` → `realized_pnl`, `position`: signed qty, entry, leverage, margin mode, isolated margin, exact liquidation price, maintenance margin), and the bridge writes that snapshot rather than re-deriving positions in SQL.
  - Rows are LONG/SHORT with absolute quantity. A close or flip marks the open row FLAT/CLOSED with realized PnL (net of the fill's fee); a flip then opens a new row.
  - Perp cash moves only by realized PnL and fees. Account equity = cash + spot value + perp uPnL.
  - The mark-to-market projector and orchestrator position publisher now value shorts with the correct sign.
  - A perp fill without a snapshot logs an error and leaves `paper_positions` alone (fee still settles).
  - Spot path is unchanged.
  - `tests/migrated_db.py` is the shared throwaway-DB-at-head helper (used by the feed and bridge tests).
- Perpetuals C7: liquidation engine.
  - `GuardedOrderDispatcher.check_liquidations(mark_prices)` triggers on long mark ≤ P_liq and short mark ≥ P_liq, using the exact tiered liquidation price. Positions are checked largest MM first, re-evaluated after each close (cross shares the wallet).
  - A breach cancels resting orders on the symbol and closes the full size with an opposing reduce-only order at mark. ISOLATED fills at the bankruptcy price if mark gapped through it. Taker fee plus a 0.5% clearance fee (`liquidation_clearance_pct`) are charged.
  - Losses stop at the isolated margin (ISOLATED) or zero cash (CROSS). The shortfall is credited back (`shortfall_covered`), and shortfall + gap cost accrue to `insurance_fund_deficit`. It runs regardless of the kill switch, and symbols without a bracket table are skipped with a warning.
  - With BTC tier-1 MMR 0.4% < 0.5% clearance, an isolated liquidation at P_liq always loses exactly its margin.
  - `PaperTradingEngine.process_liquidations` enqueues an `ORDER_FILLED` event with `liquidation: True` and `shortfall_covered`; the bridge creates the missing `paper_orders` row and closes the position row. It also publishes `perp.liquidation` on `channel:perp_liquidations`, which `StreamHub` does not forward yet (C11).
  - Positions now remember their `instance_id` so liquidation rows land on the right `paper_positions` row.
- Perpetuals C8: orchestrator wiring (`services/orchestrator/cryptonite_orchestrator/server.py`).
  - `consume_perp_marks` seeds from `perp:mark:*`, follows `channel:perp_mark_ticks`, applies ticks with their exchange event time (`update_mark_price(inst, px, ts_event_ns)`), and every 1s calls `engine.process_liquidations` with **fresh marks only**, so a stalled feed never liquidates on stale prices. It resubscribes after errors.
  - `MANUAL_ORDER`/`SUBMIT_ORDER` forward `leverage`, `margin_mode`, and `reduce_only` into `OrderIntent`. Malformed values reject the command (`INVALID_ORDER_PARAMETERS`) instead of raising and leaving it RUNNING for lease retry.
  - Startup rehydration (`rehydrate_dispatcher_state`) no longer loads perp rows into the spot `positions` map (a short would have come back as a spot long). It reloads them with `set_perp_position` (signed qty, leverage, margin mode, instance, locked IM).
  - `risk.py`: marks carry timestamps, and perp entry orders are rejected with `STALE_MARK_PRICE` when the mark is older than 10s (`max_mark_age_s`). Reduce-only orders are exempt. A mark without a timestamp counts as stale.
- Perpetuals C9: `derivatives_feed` service in `infra/docker-compose.yml`.
  - Scanner image, `python -m cryptonite_scanner_service.derivatives_feed`, `PERP_SYMBOLS=BTCUSDT,ETHUSDT`, depends on healthy postgres/redis, `unless-stopped`, default network (needs outbound internet).
  - **No `env_file`**: public market data only, so the `.env` exchange/LLM/TOTP secrets are not injected.
  - `orchestrator` depends on it (`service_started`).
  - Verified 2026-09-24: image built, the container (uid 1000) cached live BTC/ETH `perp:mark:*` and `perp:oi:*` from Binance, then it was removed again. The stack was left as found (postgres + redis only).
- Perpetuals C10: funding settlement.
  - `margin.calculate_funding_payment(size, mark, rate) = -size * mark * rate`.
  - `GuardedOrderDispatcher.apply_funding_settlement(instrument, mark, rate, funding_time_ns)`:
    - Moves cash (and daily realized PnL) and tracks `cumulative_funding` per position.
    - For ISOLATED positions it also moves the isolated margin (Binance semantics), so a funding debit pulls the liquidation price closer.
    - Idempotent and monotonic per instrument (`last_funding_time_ns`). Timestamps are ns like every other funding/mark time.
  - `PaperTradingEngine.process_funding_settlement` enqueues `FUNDING_SETTLED`, publishes `perp.funding` on `channel:perp_funding_events` (not forwarded by `StreamHub` yet), then runs a liquidation sweep on fresh marks.
  - `EventBridgeWorker` books it into account cash/realized PnL and the open position's realized PnL, isolated margin, and liquidation levels. The journal row's run-independent idempotency key `FUNDING:{account}:{instrument}:{T}` makes this exactly-once across restarts without a new migration.
  - The orchestrator's `consume_perp_marks` settles at the previous `T` with the last pre-rollover rate and mark when a tick's `next_funding_time_ns` rolls forward. Settlements missed while disconnected are not reconstructed.
  - Cumulative funding is not a separate DB column (020 has none); it is folded into `paper_positions.realized_pnl`.
- Perpetuals C11: `StreamHub._perp_pubsub_loop` (`services/api/cryptonite_api/stream_gateway.py`).
  - Relays `channel:perp_mark_ticks` / `channel:perp_liquidations` / `channel:perp_funding_events` to `/ws/stream` clients as `{"type": "perp_mark_tick" | "perp_liquidation" | "perp_funding", "data": {...}}`.
  - Client protocol: `{"action": "subscribe", "channels": ["perpetuals"], "instruments": ["BTCUSDT-PERP.BINANCE"]}`. Instruments are optional (empty = all), perp instruments are allow-listed (`DEFAULT_PERP_INSTRUMENTS`), and unsubscribe with instruments narrows the filter while unsubscribe without them stops perps.
  - The instrument filter applies to mark ticks only; liquidation and funding notices reach every perpetuals subscriber.
  - On subscribe, cached `perp:mark:*` snapshots are sent (`"snapshot": true`).
  - Mark ticks are droppable under backpressure (`enqueue_droppable`, which never evicts queued reliable events); liquidation/funding use the reliable path.
  - It runs on its own pub/sub connection and resubscribes after errors.
  - The other author's uncommitted `_pubsub_loop` change in the same file (agent heartbeat + debates) was left unstaged. C11 was committed as HEAD + perp hunks only.
- Perpetuals C12: web.
  - `apps/web/src/lib/stores/usePerpetualStore.ts` (project convention, not `src/stores/`). It holds marks, positions, the liquidation alert, and funding history, and persists only `leverage` + `marginMode` (`reduceOnly` deliberately not persisted).
    - `usePerpetualStream(wsConnected)` is mounted in `app/page.tsx`, so liquidation toasts arrive with the sidebar closed.
    - `usePerpetualPositionsPoll` polls `/paper/positions` + `/paper/accounts` every 5s, because margin fields are not on the WS snapshot.
  - `apps/web/src/lib/perp/margin.ts`: a TS port of `margin.py` (same reference brackets). Parity was checked against the Python golden vectors to 1e-8 (ad hoc Node script); there is no web unit runner in the repo.
  - `components/terminal/MarginRiskHUD.tsx`:
    - Liquidation distance meter (>20% SAFE green, 5–20% CAUTION amber, <5% DANGER pulsing red, text labels + `role=meter`).
    - Margin ratio (MM / isolated balance or account equity).
    - Funding rate (who pays) with an HH:MM:SS countdown and the liquidation banner.
  - `components/terminal/OrderEntryPanel.tsx`: a SPOT/PERP ticket.
    - Perp: 1–20x slider + 1/5/10/20 chips, margin mode (locked while a position is open), reduce-only with the dispatcher's rules mirrored client-side.
    - Pre-flight notional / IM / projected liquidation price, computed for the post-fill position including adds and flips.
    - Spot payload is unchanged.
  - Both are mounted in the terminal's operator sidebar, which now has a **Trade** toggle in the Panels bar (previously no control opened it).
  - `lib/ws.ts`: `addSubscription`/`removeSubscription` keep the `perpetuals` subscription across `updateSubscriptions` and reconnects.
  - API: `ManualOrderRequest` accepts `leverage` (1–20), `margin_mode`, and `reduce_only` and forwards them into the MANUAL_ORDER payload. Only these hunks were committed; the other author's `paper.py` changes stay unstaged.
  - Pre-existing web issues noticed: `app/page.tsx` subscribes SOL/BNB, which the gateway allow-list rejects (the whole subscribe message errors). `operator/ManualTradeModal.tsx` is unused.
- Perpetuals C13: `apps/web/e2e/perpetuals-cockpit.spec.ts` (API mocked with `page.route`, gateway mocked with `page.routeWebSocket`, no backend needed).
  - Ticket test: Trade sidebar → PERP → 10x chip syncs the slider → ISOLATED → reduce-only with no position shows the error and disables submit → LIMIT 0.05 @ 50,000 shows notional 2,500 / IM 250 / projected liq 45,180.72. The intercepted POST body is exactly `{…, leverage: 10, margin_mode: "ISOLATED", reduce_only: false}`.
  - HUD test: mocked isolated long + mark ticks → `CAUTION · 9.64%` then `DANGER · 3.87%` (meter `aria-valuetext`), margin ratio 4.00%, funding countdown `HH:MM:SS`, and a `perp_liquidation` banner that can be dismissed.
  - Both fail on React hydration/render errors.
- Paper pre-flight fixes (2026-09-24):
  - /paper kill switch → paper engine (see the commit `fix(orchestrator): route /paper kill switch…`).
  - Perp quotes: ingest ran spot only and keyed every bookTicker `{SYM}.BINANCE`, so `BTCUSDT-PERP.BINANCE` never had a quote or a freshness heartbeat, and every perp order would have been rejected (`NO_LIVE_QUOTE_IN_CACHE` / `DATA_STALE`). Ingest now also runs a quotes-only futures client (`INGEST_PERP_SYMBOLS`, `/public` bookTicker) keyed `{SYM}-PERP.BINANCE`. The orchestrator's lookup needed no change. Deliberately **not** mapped to the spot quote (spot/perp basis would skew fills and collars). Live check: 5.5k perp quotes in 6s.
- Uncommitted backend work (packages/agents, `demo_pipeline.py`, scanner, storage, api auth/routes, debate_worker, 015 budget change) has no recorded owner
- `exit_monitor.py` stays uncommitted until it lands with a P&L column on `agent_safe_positions`, `debate_runs.type` (or a switch to `trigger_source`), and the `debate_worker` exit-queue consumer — owner: unassigned
- graphify housekeeping: 212 deleted old-version cache files, untracked new cache, dated backup folders, and an uncommitted `.gitignore` rule are left for a decision

## Next Steps
1. Perpetuals C4: funding-rate history backfill (`/fapi/v1/fundingRate`, source `BINANCE_REST`) and the mark-price Parquet series per blueprint §1.2. Acceptance: gaps from feed downtime are filled idempotently
2. Re-run pytest on a clean checkout of `main` — acceptance: no hang; then push
3. Triage the remaining uncommitted backend changes into owned commits
4. Run `npx playwright test` — acceptance: all specs pass
