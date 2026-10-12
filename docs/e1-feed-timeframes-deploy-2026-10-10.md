# E1 Feed-Timeframe Deployment Evidence — 2026-10-10

## Scope

PR #45 (`741802213705b1484a1288b94a8e5b87cbe94be6`) was merged to `main` as
`893fb49ef31d3db794a4bb95f54553b162b02515`. The change is configuration-only:
ingest now subscribes to `1m,15m,1h,4h`. No database migration was run.

## Release-window safeguards

- PostgreSQL backup: `/home/samx/backups/cryptonite/db/cryptonite-2026-10-10T1544Z.dump`
- Redis backup: `/home/samx/backups/cryptonite/redis/redis-2026-10-10T1547Z.rdb` with adjacent SHA-256 manifest
- Local restore drill: `/home/samx/backups/cryptonite/drill-2026-10-10T1547Z.log`
- Drill result: `OK`; `pg_restore` exit 0; all six manifest counts and Alembic hash matched; scratch database cleanup passed.

## Deployment and verification

The first recreate used a stale local checkout and correctly exposed the old
`1m,1h` environment. No data services were touched. The checkout was then
fast-forwarded to merged `main` (`893fb49`), and ingest was recreated again:

```text
docker compose -f infra/docker-compose.yml up -d --no-deps --force-recreate ingest
```

Final state:

- Ingest container: `23b96abe86d1`, healthy
- `INGEST_TIMEFRAMES=1m,15m,1h,4h`
- Symbols and quote settings unchanged (`BTCUSDT,ETHUSDT`, spot/perp, 50 Hz)
- Redis container unchanged: `9901b56ebaee`
- PostgreSQL container unchanged: `016594c981d9`
- Ingest logs since restart: no error, exception, traceback, or fatal lines
- `cryptonite:stream:closed_bar_committed` contains fresh `15m` and `4h` events for BTCUSDT and ETHUSDT

No K1 migration, symbol onboarding, historical backfill, scanner restart, or
other service recreation was performed in this release window.
