# E1 Deployment Runbook: Pre-Scanner-Start Preset Upgrade Sequence

**Phase Gate:** E1 (Deploy the held services)
**Task Reference:** D2 / Finding F-H2 (`01a115b8-0555-7e40-b20f-987dd0f8d56a`)
**Target:** Production / Staging Deployment
**Audience:** Platform Engineer, Operator, Senior Project Manager

---

## 1. Overview & Operational Rationale

Phase D2 rewrote all built-in scanner presets (`macd-momentum-shift`, `bollinger-breakout`, `rsi-oversold-reversal`, `trend-pullback`) from continuous **state-based** rules (e.g., `close > bb_upper`) to discrete **event-based** crossover triggers (e.g., `close crosses_above bb_upper` with `lookback: 1`).

On the live host, preset rows already exist in the database with legacy state versions active, and the held legacy scanner container is **currently running**. To avoid processing incoming closed bars under legacy state rules upon deployment:
1. **Stop Running Scanner**: The running scanner service container must be explicitly stopped **before** migrations or preset upgrades.
2. **Pre-Scanner-Start Gate**: Preset specifications must be upgraded and verified **before** the new scanner container is started.
3. **Custom Spec Preservation**: Any user-customized scanner specifications are automatically detected and preserved without modification (`custom_preserved`).
4. **Active Cap Enforcement**: Upgrades strictly honor `MAX_ACTIVE_SPECS = 10` and serialize under `pg_advisory_xact_lock(hashtext('scan_specs:active_limit'))` and `FOR UPDATE` row locks.
5. **Stateful Container Safety**: Do **not** recreate or rebuild stateful `postgres` or `redis` containers (deferred per handover).

---

## 2. Service Deployment Sequencing

Follow this exact order during the E1 deployment:

1. **A3 Precondition**: Verify durable backup taken and restore proof verified (see `docs/runbooks/backup.md`).
2. **Stop Legacy Workers**: Explicitly stop the currently running legacy scanner, debate worker, and learner containers:
   ```bash
   docker compose -f infra/docker-compose.yml stop scanner debate_worker learner
   ```
3. **Database Migration**: Apply all Alembic migrations up to head using the canonical handover `test` service runner (the `api` runtime container does not package `infra/migrations`, while the `test` service mounts the repository workspace and includes the Alembic environment):
   ```bash
   docker compose -f infra/docker-compose.yml run --rm --no-deps test uv run python -m alembic -c infra/migrations/alembic.ini upgrade head
   ```
4. **Deploy Ingest & New API (B5 Sequencing Contract & Upgraded API Online)**:
   Rebuild and recreate the ingest producer container (ensuring `quote:tick:*` snapshot keys are populated in Redis for B5) and the new API container (serving the D2 `/presets/upgrade` route) using `--no-deps --force-recreate --build` while strictly preserving stateful containers (`postgres`, `redis`):
   ```bash
   docker compose -f infra/docker-compose.yml up -d --no-deps --force-recreate --build ingest api
   ```
5. **Verify API Health**: Ensure the newly deployed API service is healthy before executing operator upgrade commands:
   ```bash
   curl -sS --fail "http://localhost:8000/health"
   ```
6. **Capture Pre-Upgrade Snapshot**: Record the active version IDs of the built-in presets before applying upgrades:
   ```bash
   docker compose -f infra/docker-compose.yml exec postgres psql -U cryptonite -d cryptonite -c \
     "SELECT s.slug, s.active_version_id AS pre_upgrade_version_id, v.version_num, v.spec_hash \
      FROM scan_specs s \
      JOIN scan_spec_versions v ON s.active_version_id = v.id \
      WHERE s.slug IN ('macd-momentum-shift', 'bollinger-breakout', 'rsi-oversold-reversal', 'trend-pullback');"
   ```
7. **Preset Upgrade Sequence**: Execute Section 3 below (Dry-Run Verification -> Apply Upgrade -> Committed Re-Read Verification).
8. **Start New Scanner & Workers Online**: Once preset upgrade verification passes, rebuild and start the scanner, debate workers, and learner with `--no-deps` to preserve stateful infrastructure:
   ```bash
   docker compose -f infra/docker-compose.yml up -d --no-deps --build scanner debate_worker learner
   ```

---

## 3. Step-by-Step Preset Upgrade Execution

Upgrades can be performed either via the authenticated REST API or via the dedicated operator script.

### Method A: Via HTTP API (Recommended)

Requires an Operator authorization token (`OPERATOR_TOKEN`). Note: `( set -o pipefail; curl ... | jq . )` ensures that non-2xx failures (caught by `--fail-with-body`) fail the pipeline visibly with a non-zero exit code rather than succeeding silently due to `jq`.

#### Step 3.1: Dry-Run Verification
Simulate the upgrade to inspect planned actions without mutating the database:

```bash
( set -o pipefail; curl -sS --fail-with-body -X POST "http://localhost:8000/api/v1/scanners/presets/upgrade?dry_run=true" \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}" | jq . )
```

**Expected JSON Response:**
```json
{
  "dry_run": true,
  "active_count": 4,
  "max_active_specs": 10,
  "items": [
    {
      "slug": "macd-momentum-shift",
      "name": "MACD Momentum Shift",
      "current_version_num": 1,
      "action": "would_upgrade",
      "detail": "Matched prior built-in hash ...; would upgrade to v2 with event crossover target hash ... (archived state preserved: false)."
    },
    {
      "slug": "bollinger-breakout",
      "name": "Bollinger Bands Breakout",
      "current_version_num": 1,
      "action": "would_upgrade",
      "detail": "Matched prior built-in hash ...; would upgrade to v2 with event crossover target hash ... (archived state preserved: false)."
    },
    {
      "slug": "rsi-oversold-reversal",
      "name": "RSI Oversold Reversal",
      "current_version_num": 1,
      "action": "would_upgrade",
      "detail": "Matched prior built-in hash ...; would upgrade to v2 with event crossover target hash ... (archived state preserved: false)."
    },
    {
      "slug": "trend-pullback",
      "name": "Trend Pullback EMA 20/50",
      "current_version_num": 1,
      "action": "would_upgrade",
      "detail": "Matched prior built-in hash ...; would upgrade to v2 with event crossover target hash ... (archived state preserved: false)."
    }
  ]
}
```

Verify that:
- Every unedited built-in preset reports `would_upgrade`.
- Any user-modified custom preset reports `custom_preserved`.
- `active_count` <= 10.

#### Step 3.2: Apply Upgrade
Execute the upgrade against the active database:

```bash
( set -o pipefail; curl -sS --fail-with-body -X POST "http://localhost:8000/api/v1/scanners/presets/upgrade?dry_run=false" \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}" | jq . )
```

**Expected JSON Response:**
- Items report `action: "upgraded"` (or `custom_preserved` for modified specs).
- `dry_run: false`.

#### Step 3.3: Re-read Verification
Verify that the committed active versions now reflect the new crossover rules (including archived presets):

```bash
( set -o pipefail; curl -sS --fail-with-body "http://localhost:8000/api/v1/scanners?include_archived=true" \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}" | jq '.[] | {slug: .slug, is_archived: .is_archived, version: .active_version.version_num, hash: .active_version.spec_hash, rules: .active_version.rules}' )
```

Verify that:
- `active_version.version_num` is incremented.
- Rule conditions show `crosses_above` or `crosses_below` with `lookback: 1`.

---

### Method B: Via Operator Script

Alternatively, execute the standalone Python script from the host or deploy container:

```bash
# 1. Dry run:
DATABASE_URL="${DATABASE_URL}" uv run python scripts/upgrade_scanner_presets.py

# 2. Apply (includes committed re-read verification):
DATABASE_URL="${DATABASE_URL}" uv run python scripts/upgrade_scanner_presets.py --apply
```

The script displays a formatted console table, verifies the committed database state, and prints conditional rollback SQL commands upon completion.

---

## 4. Rollback Criteria & Procedure

### Rollback Criteria
Initiate rollback if:
1. `GET /api/v1/scanners` fails validation or scanner logs syntax errors on startup.
2. An unexpected custom preset was inadvertently modified (prevented by hash checking).

### Rollback Procedure
Because versioning in `scan_spec_versions` is strictly append-only and immutable, rollback re-points `active_version_id` to the captured pre-upgrade version ID.

> **Precondition**: Confirm fresh backup (A3) exists and Reality Checker authorization is recorded.

Execute the rollback query with the exact pre-upgrade and applied IDs captured during Step 2.5:

```sql
-- Connect to database as operator:
-- docker compose -f infra/docker-compose.yml exec postgres psql -U cryptonite -d cryptonite

-- Safely revert a preset ONLY if current active version is the applied target version:
UPDATE scan_specs
SET active_version_id = '<pre_upgrade_version_id>', updated_at = now()
WHERE slug = '<preset-slug>' AND active_version_id = '<applied_version_id>';
```

After executing rollback SQL, verify active specs with:
```bash
( set -o pipefail; curl -sS --fail-with-body "http://localhost:8000/api/v1/scanners?include_archived=true" \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}" | jq . )
```

---

## 5. Security & Isolation Constraints

- Never hardcode database credentials, Redis passwords, or JWT secrets in scripts, documentation, or commit messages.
- Always use environment variables (`DATABASE_URL`, `OPERATOR_TOKEN`).
- Do not execute live writes or migrations without operator confirmation and verified backups.
