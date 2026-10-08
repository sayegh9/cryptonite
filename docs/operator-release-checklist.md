# Operator Release Checklist (A6)

Prepared by the A6 docs audit (2026-10-08). **Operator-only** target; agents prepare, the operator runs.
No live operations, migrations, merges or deploys happen from an agent worktree. Credentials are never
shown here or in any report — read them from `.env` / environment variables.

**Standing rules:** a fresh PostgreSQL **and** Redis backup plus an independent **Reality Checker PASS**
precede every live change; migrations run through the `test` service runner, never the runtime `api`
image; `postgres` and `redis` containers are never recreated.

---

## 0. Review and merge the open heads onto `main`

- [ ] For each open PR — **#39 A3** (`53fd89a`), **#41 D5** (`1ea8444`), **#43 D3** (`8be5efc`):
  confirm an exact-head focused lead review **PASS** and exact-head CI green (`gh pr checks <n>` shows
  `ruff`, `web`, `pytest`, `e2e-smoke`, `verify` pass). No CI on a conflicting PR (no branch protection
  on GitHub Free).
- [ ] Merge each to `main` (operator-only; the agent's permission classifier blocks merges).

## 1. A3: install the reviewed backup script and prove restore

- [ ] Reinstall `~/.local/bin/cryptonite-backup` **from the merged A3 head** — the installed copy
  (2026-10-04, sha `9295a5b3…`) predates A3 and lacks the fail-closed drill/scratch-drop behavior.
- [ ] Fresh `cryptonite-backup db` immediately before any E1 step (`scripts/backup.sh`,
  `docs/runbooks/backup.md`).
- [ ] Off-machine restore: copy `~/backups/cryptonite` off-machine, restore from that copy once, and
  verify row counts match (durable restore proof).
- [ ] **3 consecutive post-install nightly drill successes**: each `cryptonite-backup drill` writes
  `drill-<stamp>.log` (+ the `drill-last.log` symlink) with exit status and row-count comparison, and a
  failed final scratch-drop flips the verdict to FAILED. This is the release gate evidence
  (separately tracked operator evidence task).
- [ ] Fresh PostgreSQL **and** Redis backups before each live change — **a Postgres dump is not a Redis
  queue backup** (rollback `XADD` gets a new message ID, not identity restore).

## 2. Pre-deploy service drain

- [ ] Let in-flight debates finish (or fail closed), then stop the old workers:
  `docker compose -f infra/docker-compose.yml stop debate_worker learner`.
- [ ] Confirm no `RUNNING` debate runs older than 30 min before proceeding.

## 3. B5: deploy ingest before any B5 reader

- [ ] Rebuild and recreate the **ingest** producer with or before the debate worker so
  `quote:tick:<instrument>` keys exist before any B5 reader deploys — without the producer, every entry
  records fail-closed `NO_PRICE`.

## 4. D2: preset upgrade before the new scanner starts

- [ ] With the legacy scanner container stopped, run the guarded preset upgrade sequence
  **dry-run → apply → re-read verification** via `POST /api/v1/scanners/presets/upgrade` or
  `scripts/upgrade_scanner_presets.py` (`docs/runbooks/e1_preset_upgrade.md`).
- [ ] Verify active presets use event crossover rules (`crosses_above`/`crosses_below`, `lookback: 1`)
  and custom specs are preserved (`custom_preserved`).
- [ ] Only start the scanner service **after** verification passes.

## 5. Migrations and deploy (safe order)

- [ ] Back up (standing rule), then apply migrations to the merged release head:
  `docker compose -f infra/docker-compose.yml run --rm --no-deps test uv run python -m alembic -c infra/migrations/alembic.ini upgrade head`.
  Note: the dev DB is at `036`; **do not** cite dev-revision checks as production evidence — production
  reaches `037→040` only through this controlled step.
- [ ] Deploy with `--build`; D2 contract: `ingest`/`api` online before the preset upgrade, scanner last.
  `--force-recreate` only for released (previously held) services.
- [ ] **Stateful container preservation:** do not recreate/rebuild `postgres` and `redis` — use
  `--no-deps` and `--no-recreate` for everything else.
- [ ] Post-deploy checks: `curl 127.0.0.1:3000/` → 200; `curl 127.0.0.1:8000/health` → 200; one
  `/api/v1/…` call through the web → 200; `docker ps` healthy; a debate without the On-Chain Analyst;
  unrealized PnL moving (after B3 mark-to-market).

## 6. Review and rollback

- [ ] Record pre-change snapshots: scanner preset `active_version_id` per slug, container image
  digests/tags, `alembic current` on the live DB.
- [ ] Scanner preset rollback: re-point `active_version_id` to the captured pre-upgrade ID with the
  equality-guarded SQL in `docs/runbooks/e1_preset_upgrade.md` §4.
- [ ] Container rollback: redeploy the previous image tags/digests captured above.
- [ ] Reset the **BINANCE-SPOT-DEMO** kill-switch latch (set by a test on 2026-09-25) via the normal
  password + TOTP step-up.

## 7. Credential rotation reminder (no secrets here)

- [ ] Rotate the previously disclosed **Redis credential** via reviewed maintenance. Never copy, repeat
  or commit credential values in reports, docs or code — reference the URL only as
  `redis://127.0.0.1:6379/15` with the actual credentials supplied privately via `.env` environment
  variables.
- [ ] Per J4 hygiene: move `*_dev_pass` values to `.env` with generated values; secrets travel in `.env`,
  never in prose or commit messages (TruffleHog scans every commit in a PR range).

## 8. Independent Reality PASS

- [ ] Record the independent phase Reality Checker PASS before sign-off of each live change; no release
  gate is waived (Phase 0 run rules).

---

Reference: `docs/HANDOVER-2026-10-08.md` (current status), `docs/HANDOVER-2026-10-06.md` (historical run),
`docs/runbooks/backup.md`, `docs/runbooks/e1_preset_upgrade.md`, `docs/run-and-deployment-changes.md`.