# Backups

`scripts/backup.sh` (installed as `~/.local/bin/cryptonite-backup`, run by the user crontab):

| When (local time) | Command | Kept |
|---|---|---|
| daily 02:15 | `cryptonite-backup db`: `pg_dump -Fc` of `cryptonite` | 14 days |
| Sunday 02:30 | `cryptonite-backup data`: `data/` (catalog, lake, artifacts) as `.tar.zst` | 4 |
| Sunday 02:35 | `cryptonite-backup secrets`: `.env`, `.env.notifier`, `infra/security/*.pem`, gpg AES-256 | 4 |
| Sunday 02:45 | `cryptonite-backup drill`: restore the newest dump into `cryptonite_restore_drill`, compare row counts, drop it | — |

Output goes to `~/backups/cryptonite/{db,data,secrets}` (mode 700/600); the log is
`~/backups/cryptonite/backup.log`. The DB backup needs `infra-postgres-1` running; when the stack is
stopped the night's run fails and is logged.

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
