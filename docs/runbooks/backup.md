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
stopped the night's run fails and is logged.

The `db` mode writes a row-count manifest beside each dump (`cryptonite-<stamp>.dump.counts`, same
14-day rotation): per-table counts taken right after `pg_dump`. The drill restores the newest dump
into the scratch database (created empty first, always dropped again — even when the restore or a
comparison fails, via a trap that can only drop the scratch name and refuses source/postgres names),
records the `pg_restore` exit code unmasked, then requires every manifest count to equal the restored
count exactly (a missing table, an unreadable `alembic_version`, or a missing manifest fails the
drill). Equality is manifest-vs-restored, never live-vs-restored: the manifest is seconds newer than
the dump, so concurrent writes between dump and drill cannot cause false failures. The drill exits
nonzero on any failure; the evidence file holds the restore log, every MATCH/MISMATCH line and the
final OK/FAILED verdict.

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
paper_accounts, portfolio_decisions 41, alembic 031).
