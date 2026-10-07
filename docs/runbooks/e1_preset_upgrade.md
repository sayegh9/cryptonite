# E1 Deployment Runbook: Pre-Scanner-Start Preset Upgrade Sequence

**Phase Gate:** E1 (Deploy the held services)
**Task Reference:** D2 / Finding F-H2 (`01a115b8-0555-7e40-b20f-987dd0f8d56a`)
**Target:** Production / Staging Deployment
**Audience:** Platform Engineer, Operator, Senior Project Manager

---

## 1. Overview & Operational Rationale

Phase D2 rewrote all built-in scanner presets (`macd-momentum-shift`, `bollinger-breakout`, `rsi-oversold-reversal`, `trend-pullback`) from continuous **state-based** rules (e.g., `close > bb_upper`) to discrete **event-based** crossover triggers (e.g., `close crosses_above bb_upper` with `lookback: 1`).

In the deployed database, preset rows already exist with pre-D2 versions (v1) active. To prevent the scanner service from processing incoming closed bars under legacy state rules upon deployment:
1. **Pre-Scanner-Start Gate**: Preset specifications must be upgraded **before** the `cryptonite_scanner` service is started.
2. **Custom Spec Preservation**: Any user-customized scanner specifications are automatically detected and preserved without modification (`custom_preserved`).
3. **Active Cap Enforcement**: Upgrades strictly honor `MAX_ACTIVE_SPECS = 10` and serialize under `pg_advisory_xact_lock(hashtext('scan_specs:active_limit'))` and `FOR UPDATE` row locks.

---

## 2. Service Deployment Sequencing

Follow this exact order during the E1 deployment:

1. **A3 Precondition**: Verify durable backup taken and restore proof verified (see `docs/runbooks/backup.md`).
2. **Database Migration**: Apply all Alembic migrations up to head:
   ```bash
   docker compose -f infra/docker-compose.yml exec api alembic -c infra/alembic.ini upgrade head
   ```
3. **Core Services Online**: Rebuild and start core infrastructure services:
   - `postgres`
   - `redis`
   - `ingest` (produces `quote:tick:*` snapshot keys required by B5)
   - `api` (serves operator routes)
   > **STOP / GATE**: Do **NOT** start `scanner`, `debate_worker`, or `learner` yet.
4. **Preset Upgrade Sequence**: Execute Step 3 below (Dry-Run -> Apply -> Re-read Verification).
5. **Worker Services Online**: Once preset upgrade verification passes, start the remaining services:
   - `scanner`
   - `debate_worker` (replicas ×3)
   - `learner`

---

## 3. Step-by-Step Preset Upgrade Execution

Upgrades can be performed either via the authenticated REST API or via the dedicated operator script.

### Method A: Via HTTP API (Recommended)

Requires an Operator authorization token (`OPERATOR_TOKEN`).

#### Step 3.1: Dry-Run Verification
Simulate the upgrade to inspect planned actions without mutating the database:

```bash
curl -s -X POST "http://localhost:8000/api/v1/scanners/presets/upgrade?dry_run=true" \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}" | jq .
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
curl -s -X POST "http://localhost:8000/api/v1/scanners/presets/upgrade?dry_run=false" \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}" | jq .
```

**Expected JSON Response:**
- Items report `action: "upgraded"` (or `custom_preserved` for modified specs).
- `dry_run: false`.

#### Step 3.3: Re-read Verification
Verify that the active versions now reflect the new crossover rules:

```bash
curl -s "http://localhost:8000/api/v1/scanners" \
  -H "Authorization: Bearer ${OPERATOR_TOKEN}" | jq '.[] | {slug: .slug, version: .active_version.version_num, rules: .active_version.rules}'
```

Verify that:
- `active_version.version_num` is incremented (e.g. `2`).
- Rule conditions show `crosses_above` or `crosses_below` with `lookback: 1`.

---

### Method B: Via Operator Script

Alternatively, execute the standalone Python script from the host or deploy container:

```bash
# 1. Dry run:
DATABASE_URL="${DATABASE_URL}" uv run python scripts/upgrade_scanner_presets.py

# 2. Apply:
DATABASE_URL="${DATABASE_URL}" uv run python scripts/upgrade_scanner_presets.py --apply
```

The script displays a formatted console table and prints rollback SQL commands upon completion.

---

## 4. Rollback Criteria & Procedure

### Rollback Criteria
Initiate rollback if:
1. `GET /api/v1/scanners` fails validation or scanner logs syntax errors on startup.
2. An unexpected custom preset was inadvertently modified (prevented by hash checking).

### Rollback Procedure
Because versioning in `scan_spec_versions` is strictly append-only and immutable, rollback simply re-points `active_version_id` to the previous version:

```sql
-- Connect to database as operator:
-- docker compose -f infra/docker-compose.yml exec postgres psql -U cryptonite -d cryptonite

-- Revert a preset back to version 1:
UPDATE scan_specs s
SET active_version_id = v.id, updated_at = now()
FROM scan_spec_versions v
WHERE v.spec_id = s.id
  AND v.version_num = 1
  AND s.slug = '<preset-slug>';
```

After executing rollback SQL, verify active specs with `GET /api/v1/scanners`.

---

## 5. Security & Isolation Constraints

- Never hardcode database credentials, Redis passwords, or JWT secrets in scripts, documentation, or commit messages.
- Always use environment variables (`DATABASE_URL`, `OPERATOR_TOKEN`).
- Do not execute live writes or migrations without operator confirmation and verified backups.
