# Backups

`scripts/backup.sh` (installed as `~/.local/bin/cryptonite-backup`, run by the user crontab):

| When (local time) | Command | Kept |
|---|---|---|
| daily 02:15 | `cryptonite-backup db`: `pg_dump -Fc` of `cryptonite` | 14 days |
| Sunday 02:30 | `cryptonite-backup data`: `data/` (catalog, lake, artifacts) as `.tar.zst` | 4 |
| Sunday 02:35 | `cryptonite-backup secrets`: `.env`, `.env.notifier`, `infra/security/*.pem`, gpg AES-256 | 4 |
| Sunday 02:45 | `cryptonite-backup drill`: restore the newest dump into `cryptonite_restore_drill`, prove row-count equality against the backup-time manifest, drop it | — |

Output goes to `~/backups/cryptonite/{db,data,secrets}` (mode 700/600); every mode appends to
`~/backups/cryptonite/backup.log`, and each drill additionally writes a per-run evidence file
`~/backups/cryptonite/drill-<stamp>.log`. The DB backup needs `infra-postgres-1` running; when the stack is
stopped the night's run fails and is logged. PG access is portable: with `PGHOST`/`PGPORT`/`PGDATABASE`
set or `CRYPTONITE_PG_DIRECT=1` the client binaries run directly against that cluster (this is how CI
exercises the drill against its throwaway postgres); otherwise they run through the `infra-postgres-1`
container.

The `db` mode writes a row-count manifest beside each dump (`cryptonite-<stamp>.dump.counts`, same
14-day rotation). The dump and every count come from one exported PostgreSQL snapshot (a holder
session exports it, `pg_dump --snapshot` uses it, and each count runs `SET TRANSACTION SNAPSHOT` on
it), so the manifest always describes exactly the rows inside the dump even while the database is
being written to. Every count query is fail-checked and must return a number before anything is
published: a failed query aborts the backup instead of logging success with a hollow value. The
manifest also carries `alembic_version_hash` (sha256 of the ordered `alembic_version` rows). The
drill restores the newest dump into the scratch database (created empty first, always dropped again —
even when the restore or a comparison fails, via a trap that can only drop the scratch name and
refuses source/postgres names), records the `pg_restore` exit code unmasked, then strictly validates
the manifest — exactly the six count tables, one numeric count each, no extras, duplicates or blank
lines, identifiers allowlisted, and the alembic hash present — and requires every restored count and
the restored version hash to match the manifest exactly. A missing, partial, empty or duplicate
manifest, a non-numeric value, a missing or unreadable table, or a hash mismatch fails the drill.
Equality is manifest-vs-restored, never live-vs-restored. The drill exits nonzero on any failure;
the evidence file holds the restore log, every MATCH/MISMATCH line and the final OK/FAILED verdict.
Dumps written before this manifest format existed (`alembic_version_hash` absent) are flagged
invalid by the drill; take a fresh `db` backup first.

**The DB dump alone cannot restore TOTP**: operator TOTP secrets are encrypted with the AES key in
`.env`. Restore the secrets archive with the dump.

**Passphrase:** `~/.config/cryptonite-backup/passphrase` (mode 600). Keep a copy off this machine (a
password manager). Without it the secrets archive cannot be opened. Backups on the same disk do not
survive a disk failure; copy `~/backups/cryptonite` elsewhere if that matters.

## Restore

```bash
# secrets
gpg --batch --pinentry-mode loopback --passphrase-file ~/.config/cryptonite-backup/passphrase \
    -d ~/backups/cryptonite/secrets/secrets-<stamp>.tar.gpg | tar -C <repo> -xf -
# database (stack stopped except postgres)
docker exec -i infra-postgres-1 pg_restore -U cryptonite -d cryptonite --clean --if-exists < ~/backups/cryptonite/db/cryptonite-<stamp>.dump
# data
zstd -dc ~/backups/cryptonite/data/data-<stamp>.tar.zst | tar -C <repo> -xf -
```

First drill: 2026-10-04 20:53 UTC, OK (operator_users, ai_mandates, execution_commands 241,
paper_accounts, portfolio_decisions 41). The alembic head was operator-confirmed as 031 at the time;
from the snapshot-consistency rework onward the drill itself verifies the alembic revision via
`alembic_version_hash` (older dumps lack it and are flagged invalid by the newer drill).
