# Verification corrections — 2026-10-10

Corrections to earlier status statements, checked against the live host, Git and GitHub on 2026-10-10.
Historical handovers are not rewritten. Times are UTC unless marked "local" (the host runs EEST, UTC+3).

Labels: **VERIFIED** = checked in this pass with the command evidence below. **CARRIED** = taken from
`finalize-instructions.md` §2 and not re-checked here (no live database read was made in this pass).

## 1. Host outage and missed scheduled backup and drill — VERIFIED

- `last -x` shows a system shutdown from 2026-10-10 01:53 to 11:53 local (10:00). `uptime -s` reports boot at 11:53:39 local.
- The cron entries are a DB backup at 02:15 local and a drill at 02:45 local, each daily.
- `backup.log` has the scheduled DB backup for the night of 2026-10-08 (`2026-10-08T23:16Z`, 02:16 local on 10-09) and no
  scheduled entry for the night of 10-09 (`2026-10-09T23:15Z`, 02:15 local on 10-10).
- The scheduled drill that passed is `drill-2026-10-08T2345Z` (`2026-10-08T23:46Z`, 02:46 local on 10-09). No scheduled drill ran at 02:45 local on 10-10.
- The 10-10 backup (`2026-10-10T10:17:56Z`) and drill (`drill-2026-10-10T1019Z`) were manual.
- The scheduled drill at 02:45 local on 2026-10-11 passed (`drill-2026-10-10T2345Z`, `2026-10-10T23:47Z`).

## 2. Postgres and Redis restarted at boot; not recreated — VERIFIED

`docker inspect` shows the containers were created earlier and started at boot:

| Container | ID | Created | Started |
|---|---|---|---|
| `infra-postgres-1` | `016594c981d9` | 2026-10-04T20:51:13Z | 2026-10-10T08:54:02Z |
| `cryptonite_redis` | `9901b56ebaee` | 2026-10-05T12:23:08Z | 2026-10-10T08:54:02Z |

Data was preserved. Neither container was recreated.

## 3. Local checkout branch state — VERIFIED (corrected)

- `finalize-instructions.md` §2 says the local checkout was on `docs/e1-deploy-evidence` at `c55b5b7`, not `main`. That was true at the time of that pass.
- At this check, the primary checkout `repo/` is on `main` at `99adceb`, in sync with `origin/main` (`## main...origin/main`).
- Action §3.1 of the finalize instructions is therefore already done.

## 4. PR #54 is merged into PR #52's branch — VERIFIED

- PR #54 (`review/k1-ingest-rate-limit-hardening`) is MERGED at `2026-10-09T19:15:58Z`, base `feat/k1-ingest-rate-limit-hardening`.
- PR #52 is still open against `main`. It is the only remaining merge in that stack.

## 5. Scanner preset gate — VERIFIED satisfied (2026-10-11; correction of earlier "not applied")

- The earlier claim that the presets were not upgraded was based on the runbook's operator names (`crosses_above` /
  `crosses_below`). The code emits `cross_above` with `lookback: 1`, which the engine evaluates as an event crossover
  (`packages/scanner/cryptonite_scanner/engine.py`, `is_cross_above`).
- The upgrade script's dry run (`scripts/upgrade_scanner_presets.py`, run inside the API container, no `--apply`)
  reports `CURRENT / NOOP` for all five active presets: `rsi-oversold-reversal`, `trend-pullback`,
  `bollinger-breakout`, `macd-momentum-shift`, `scalp-multi-tf`. Active count is 5 of 10. No database change was made.
- Runbook `docs/runbooks/e1_preset_upgrade.md` step 3.3 still expects `crosses_above` / `crosses_below` literally. That
  text is stale and should be corrected to `cross_above` with `lookback: 1`.

## 6. Scanner, workers and orchestrator were built before the 10-10 ingest deploy — VERIFIED

| Image | ID | Built (local) |
|---|---|---|
| `infra-ingest` | `bee2d23192d4` | 2026-10-09 02:01:14 |
| `infra-scanner` | `74e5d1376a9a` | 2026-10-09 02:03:54 |
| `infra-debate_worker` | `8be7d1968bb9` | 2026-10-09 02:03:54 |
| `infra-orchestrator` | `a1e6ce9e3915` | 2026-10-09 02:03:55 |

- The `cryptonite_scanner` container was created `2026-10-08T23:05:46Z` from image `74e5d1376a9a`.
- The ingest deploy on 10-10 recreated ingest from the same image `bee2d23192d4`. The change was config-only: `INGEST_TIMEFRAMES=1m,15m,1h,4h`.
- PR #45 merged as `893fb49` at `2026-10-10T15:43:59Z`. `git show --stat 893fb49` lists `docs/e1-feed-timeframes-status.md`, `infra/docker-compose.yml` (one line) and `tests/test_e1_feed_timeframes.py`.
- All of these images predate that merge. Rebuilding them is a separate step (`finalize-instructions.md` §5.6), after G3.

## Not verified in this pass

- Migration `042` not applied; live database at `041` — confirmed by `alembic_version` on 2026-10-11.
- No runtime, data, compose or `main` change was made by this document.

## 7. A3 backup and restore gate — CLOSED BY OPERATOR WAIVER (2026-10-11). Not a PASS.

The operator (the repository owner) closed A3 on 2026-10-11 and waived the remaining gate items below for release
purposes. This is recorded as a waiver. It is not evidence that the gate criteria were met.

Evidence that exists:

- Database backups and restore drills: `cryptonite-2026-10-11T0957Z`, `…1006Z` and `…1620Z`, each drill `OK` (`pg_restore` exit 0,
  row counts and Alembic hash matched, scratch database removed).
- Scheduled drills: `drill-2026-10-08T2345Z` passed (02:45 local on 2026-10-09). `drill-2026-10-10T2345Z` passed (02:45 local on
  2026-10-11). The 2026-10-10 02:45 local run was missed during the host outage.
- Redis snapshot `redis-2026-10-11T1624Z.rdb` with SHA-256 manifest (manual; no scheduled Redis backup exists).
- Data backup `data-2026-10-10T2330Z.tar.zst` and secrets archive `secrets-2026-10-10T2335Z.tar.gpg`.
- Recovery set copied to `/media/samx/Mac/cryptonite-offmachine-2026-10-11/` with `SHA256SUMS` verified and a restore
  from the copy verified (table counts match, Alembic `041`).

Waived items, with the reason as recorded by the operator:

- Three consecutive scheduled drills. Only one scheduled drill has passed since the outage. Waived.
- Off-machine copy. The `/media/samx/Mac` target is partition `nvme0n1p5` on the same physical SSD as the root filesystem,
  so it is **not** off-machine. The operator chose to defer the off-machine copy. Waived. It remains an open risk.
- Automated Redis backup. None is scheduled. Waived.

Operator action still open, not blocking A3 closure: move one copy of the recovery set to a separate physical device. The
passphrase must be stored apart from the encrypted archive.
