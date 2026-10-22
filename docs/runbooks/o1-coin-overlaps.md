# O1: coin overlaps before migration 050

Migration 050 (auto-trade profiles, ADR 0011 D6) gives each base asset (BTC, ETH, WLD ...) to one mandate whose status
is ACTIVE, PAUSED or HALTED. BTCUSDT and BTCUSDT-PERP are both BTC, so a spot mandate and a perp mandate on BTC
overlap. The migration refuses to run while any coin has two such owners. The live DB is at 049.

Before 050 there is no STOPPED status and the API cannot archive, so `scripts/profile_overlap_resolve.py` makes the
change by hand. Each change is guarded by the position checks and writes an `audit_events` row.

## 1. Backup

```bash
cd /home/samx/Documents/projects/cryptonite/deploy-main
scripts/backup.sh db
```

## 2. Report

The default run changes nothing. It exits 1 when a coin has two owners, 0 when none.

```bash
docker compose -f infra/docker-compose.yml run --rm --no-deps test \
  uv run python scripts/profile_overlap_resolve.py
```

Check which database the `test` service's `DATABASE_URL` points to before running it against live data. Each owner
line shows whether its instance holds an open position or a working order on that coin.

## 3. Recommended resolution for the live data

Each step is subject to its position check. The script refuses a step when the mandate holds the coin, and the
refusal names the instrument. Run the steps in this order. Archiving "3 assets" first is required, because it
still owns ETH and the spot L1 change would otherwise add a second ETH owner, which is refused.

1. Keep **Brain BTCUSDT-PERP.BINANCE** for BTC. No change.
2. Archive **3 assets**:
   ```bash
   ... scripts/profile_overlap_resolve.py --archive "3 assets"                     # dry run
   ... scripts/profile_overlap_resolve.py --archive "3 assets" --confirm "3 assets" --reason "O1 overlap"
   ```
3. Archive **WLDUSDT** (same form, name `WLDUSDT`).
4. Set **BTC-ETH spot L1** to ETHUSDT only (drops BTCUSDT; refused while spot BTC is held):
   ```bash
   ... scripts/profile_overlap_resolve.py --set-instruments "BTC-ETH spot L1" ETHUSDT --confirm "BTC-ETH spot L1" --reason "O1 overlap"
   ```
5. Keep **WLDUSDT.BINANCE** for WLD. No change.
6. Run the report again. It must print the passing line and exit 0.

If a step is refused because a coin is held, the position must be closed through the normal lifecycle first. The
script does not close positions and does not cancel orders.

## 4. Exit codes and refusals

| Exit | Meaning |
|------|---------|
| 0 | Report with no overlaps, a dry run that would succeed, or an applied change |
| 1 | Report found overlaps |
| 2 | Refused (position, working order, queued command, empty list, new owner, confirm mismatch) or invalid input |

- A confirm must equal the mandate name exactly. Any other value changes nothing.
- Refusals list every blocking instrument, so the next step is clear.
- The script runs one transaction. The mandate row is locked (`FOR UPDATE`), and a refusal rolls back.
- From migration 050 on (`ai_mandates.markets` exists) the script only reports. Use the profile API after that.

## 5. After the overlaps are gone

Run the migration with the normal deploy step, then check `alembic current` shows `050`. The audit rows written by
this script are kept (event types `PROFILE_OVERLAP_ARCHIVED` and `PROFILE_OVERLAP_INSTRUMENTS_SET`).
