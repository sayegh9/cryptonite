# Operator Release Checklist (A6)

Prepared by the A6 docs audit (2026-10-08); reordered into an executable chronology for the
lead-docs-review follow-up in PR #44 (`21d4b8e` → next head). **Operator-only** target: agents prepare,
the operator runs. No live operations, migrations, merges or deploys happen from an agent worktree.
Credentials are never shown here or in any report — read them from `.env` / environment variables.

**Standing rules:** a fresh PostgreSQL **and** Redis backup plus an independent **Reality Checker PASS**
precede every live change. Alembic migrations run through the `test` service runner, never the runtime
`api` image. **`postgres` and `redis` are never passed to `up -d` and are never recreated** — every
deploy command in §3 uses `--no-deps` and targets only stateless services.

---

## 1. Review and merge the open heads onto `main`

- [ ] For each open PR — **#39 A3** (`53fd89a`), **#41 D5** (`1ea8444`), **#43 D3** (`8be5efc`):
  confirm an exact-head focused lead review **PASS** and exact-head CI green (`gh pr checks <n>` shows
  `ruff`, `web`, `pytest`, `e2e-smoke`, `verify` pass). No CI on a conflicting PR (no branch protection
  on GitHub Free).
- [ ] Merge each to `main` (operator-only; the agent's permission classifier blocks merges), then
  re-verify the merged heads before the release window.

## 2. A3: install the reviewed backup script and prove restore

Do this **before** the release window opens (§3).

- [ ] Reinstall `~/.local/bin/cryptonite-backup` **from the merged A3 head** — the installed copy
  (2026-10-04, sha `9295a5b3…`) predates A3 and lacks the fail-closed drill/scratch-drop behavior.
- [ ] Fresh `cryptonite-backup db` immediately before any live step (`scripts/backup.sh`,
  `docs/runbooks/backup.md`).
- [ ] Off-machine restore: copy `~/backups/cryptonite` off-machine, restore from that copy once, and
  verify row counts match (durable restore proof).
- [ ] **3 consecutive post-install nightly drill successes**: each `cryptonite-backup drill` writes
  `drill-<stamp>.log` (+ the `drill-last.log` symlink) with exit status and row-count comparison, and a
  failed final scratch-drop flips the verdict to FAILED. This is the release-gate evidence
  (separately tracked operator evidence task).
- [ ] Fresh PostgreSQL **and** Redis backups before each live change — **a Postgres dump is not a Redis
  queue backup** (rollback `XADD` gets a new message ID, not identity restore).

## 3. Release window (E1) — execute strictly in this order

Chronology follows `docs/runbooks/e1_preset_upgrade.md` §2: stop legacy workers → migrate → deploy
ingest+API → API health → snapshot → preset upgrade (dry-run/apply/re-read) → start
scanner/workers. `postgres` and `redis` stay untouched throughout.

### 3.1 Drain and stop the legacy workers

- [ ] Let in-flight debates finish (or fail closed); confirm no `debate_runs` entry is `RUNNING` older
  than 30 min.
- [ ] Explicitly stop the legacy scanner, debate worker and learner — required before migrations and
  the preset upgrade so the worker never evaluates incoming bars under legacy rules:
  ```bash
  docker compose -f infra/docker-compose.yml stop scanner debate_worker learner
  ```
- [ ] Confirm `docker compose ps`: `scanner`, `debate_worker`, `learner` stopped; `postgres` and
  `redis` still running and untouched.

### 3.2 Migrations

- [ ] Back up (standing rule), then apply migrations to the **merged release head** through the `test`
  runner — `upgrade head`, then **verify the migrated head**:
  ```bash
  docker compose -f infra/docker-compose.yml run --rm --no-deps test uv run python -m alembic -c infra/migrations/alembic.ini upgrade head
  docker compose -f infra/docker-compose.yml run --rm --no-deps test uv run python -m alembic -c infra/migrations/alembic.ini current
  ```
- [ ] `current` must equal the **merged release head** — today `040`; once D5 #41 merges it is **`041`**
  (`infra/migrations/versions/041_risk_limit_overrides_version.py`, `down_revision = "040"`), subject to
  an exact merged-head check at deploy time (open heads #39/#43 may add revisions).
- [ ] The dev DB is at Alembic `036`; **do not** cite dev-revision checks as production evidence —
  production reaches the merged release head only through this controlled step.

### 3.3 Deploy the updated stateless services the downstream contracts need (B5 + D2)

- [ ] **B5 ingest-before-worker:** rebuild and recreate the **ingest** producer with or before any B5
  reader (the debate worker), so `quote:tick:<instrument>` snapshot keys exist before the worker
  deploys — without the producer, every entry records fail-closed `NO_PRICE`.
- [ ] Deploy the D2-capable **api** (serves `POST /api/v1/scanners/presets/upgrade`) together with
  ingest in one targeted step — `--no-deps` plus `--force-recreate --build` **for these updated
  stateless services only**; never `postgres`/`redis`:
  ```bash
  docker compose -f infra/docker-compose.yml up -d --no-deps --force-recreate --build ingest api
  ```

### 3.4 Verify API health

- [ ] The newly deployed API answers before any operator command:
  ```bash
  curl -sS --fail "http://localhost:8000/health"
  ```

### 3.5 Capture the pre-upgrade snapshot

- [ ] Record the active preset version IDs before applying upgrades (query in
  `docs/runbooks/e1_preset_upgrade.md` §2 step 6): slug → `active_version_id` → `version_num` →
  `spec_hash`, for all four built-in presets.

### 3.6 D2 preset upgrade — before the new scanner starts, gated on verification

- [ ] Run the guarded preset upgrade sequence **dry-run → apply → re-read verification** via
  `POST /api/v1/scanners/presets/upgrade` or `scripts/upgrade_scanner_presets.py`
  (`docs/runbooks/e1_preset_upgrade.md` §3, Method A or B).
- [ ] Verify: active presets use event crossover rules (`crosses_above`/`crosses_below`,
  `lookback: 1`), custom specs are preserved (`custom_preserved`), `active_count <= 10`.
- [ ] Do **not** start the scanner until verification passes.

### 3.7 Start the released scanner and workers (gated on 3.6 verification)

- [ ] Rebuild and start the previously held services — held containers sit on legacy images, so
  replacing them needs a targeted `--force-recreate --build` (same `--no-deps` pattern; `postgres` and
  `redis` never included):
  ```bash
  docker compose -f infra/docker-compose.yml up -d --no-deps --force-recreate --build scanner debate_worker learner
  ```
- [ ] Remaining E1 releases from the handover deploy note, same targeted pattern
  (`--no-deps` + `--force-recreate --build` for the listed stateless services only): `orchestrator`
  (B3 mark-to-market), and `strategy_sandbox` **must** be rebuilt (new `--heartbeat` flag).

### 3.8 Post-deploy checks

- [ ] `curl 127.0.0.1:3000/` → 200; `curl 127.0.0.1:8000/health` → 200; one `/api/v1/…` call through
  the web → 200; `docker ps` healthy.
- [ ] `quote:tick:<instrument>` keys present with fresh `arrival_ns` (B5), and the scanner is evaluating
  event-based crossover specs (D2).
- [ ] A debate without the On-Chain Analyst (R3.2); rule triage active (R3.1); unrealized PnL moving
  (after B3 mark-to-market).

## 4. Review and rollback

- [ ] Record pre-change snapshots: scanner preset `active_version_id` per slug (§3.5), container image
  digests/tags, `alembic current` on the live DB.
- [ ] Scanner preset rollback: re-point `active_version_id` to the captured pre-upgrade ID with the
  equality-guarded SQL in `docs/runbooks/e1_preset_upgrade.md` §4.
- [ ] Container rollback: redeploy the previous image tags/digests captured above.
- [ ] Reset the **BINANCE-SPOT-DEMO** kill-switch latch (set by a test on 2026-09-25) via the normal
  password + TOTP step-up.

## 5. Credential rotation reminder (no secrets here)

- [ ] Rotate the previously disclosed **Redis credential** via reviewed maintenance. Never copy, repeat
  or commit credential values in reports, docs or code — reference the URL only as
  `redis://127.0.0.1:6379/15` with the actual credentials supplied privately via `.env` environment
  variables.
- [ ] Per J4 hygiene: move `*_dev_pass` values to `.env` with generated values; secrets travel in `.env`,
  never in prose or commit messages (TruffleHog scans every commit in a PR range).

## 6. Independent Reality PASS

- [ ] Record the independent phase Reality Checker PASS before sign-off of each live change; no release
  gate is waived (Phase 0 run rules).

---

Reference: `docs/HANDOVER-2026-10-08.md` (current status), `docs/HANDOVER-2026-10-06.md` (historical run),
`docs/runbooks/backup.md`, `docs/runbooks/e1_preset_upgrade.md`, `docs/run-and-deployment-changes.md`.