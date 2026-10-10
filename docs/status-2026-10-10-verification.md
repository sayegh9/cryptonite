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

## 5. Scanner preset gate is not applied — CARRIED (UNVERIFIED in this pass)

- `finalize-instructions.md` §2 says the 5 active scan spec versions contain no `crosses_above` / `crosses_below` operators.
- Not re-checked here. The gate remains open (G3, owner: operator).

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

- Migration `042` not applied; live database at `041` — CARRIED from `finalize-instructions.md` §2.
- A3 remains open, including the scheduled-drill count and the off-machine copy — see `finalize-instructions.md` §4 G1.
- No runtime, data, compose or `main` change was made in this pass.
