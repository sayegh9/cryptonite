# E1: Ingest Feed Timeframes — 1m / 15m / 1h / 4h

**Status:** PR OPEN (branch `fix/d3-feed-timeframes`, base `origin/main` `fbc80f5`). **No live deploy, no main merge** — rebuild of `ingest` is the operator's next step after merge.

**Task:** `01a1206e-f482-7b73-9ab3-68f51c06792a` (E1 blocker: provide live 15m/4h committed-bar triggers).

---

## 1. The gap

Before this change the ingest service subscribed klines for `INGEST_TIMEFRAMES: 1m,1h` only.
D3 restricts automatic triggers to `ALLOWED_AUTOMATIC_TIMEFRAMES = ("15m", "1h", "4h")`
(`docs/d3-structure-stop-target-status.md`; presets `bollinger-breakout` → `["15m","1h"]`,
`scalp-multi-tf` → `["15m","1h","4h"]`). With no live 15m or 4h committed bars, D3 automatic
triggers could never fire (observed: 15m/4h count 0 in the intake; scanner emits no such events).

## 2. The change — one line, config only

`infra/docker-compose.yml`, ingest service:

```diff
-      INGEST_TIMEFRAMES: 1m,1h
+      INGEST_TIMEFRAMES: 1m,15m,1h,4h
```

Nothing else in the compose file changed. The ingest code already subscribes
`kline_<tf>` per configured timeframe (`stream.py`) and routes closed klines through
`_on_closed_bar_event` per timeframe (`recorder.py`); Binance emits `kline_15m` / `kline_4h`
natively, and `parse_timeframe_to_nautilus_spec` (`cryptonite_schemas/identifiers.py`)
resolves 15m → MINUTE(15) and 4h → HOUR(4). No code change was required.

Perp and spot setup preserved exactly: `INGEST_PERP_SYMBOLS: BTCUSDT,ETHUSDT`,
`INGEST_SYMBOLS: BTCUSDT,ETHUSDT`, `INGEST_SPOT_QUOTE_HZ: "50"`, REDIS `.../0`, port 8080,
healthcheck, all other services untouched.

## 3. Verification (focused tests/config validation)

New `tests/test_e1_feed_timeframes.py` (static, no network/DB, mirrors
`tests/test_compose_supervision.py`):

- compose `INGEST_TIMEFRAMES` parses to exactly `{1m, 15m, 1h, 4h}` and contains D3's `{15m, 4h}`;
- every configured timeframe is a Binance kline interval and maps to the expected Nautilus
  `BarSpecification`/`BarType` (15m → MINUTE(15), 4h → HOUR(4));
- WS subscription stream names equal `{kline_1m, kline_15m, kline_1h, kline_4h}`;
- perp-quote and spot config preserved (regression guard).

Run result: `uv run pytest tests/test_e1_feed_timeframes.py tests/test_compose_supervision.py -q` → passed.
(Full suite run on `main` CI independently; no repo test hardcodes the production timeframe list.)

## 4. K1 plan conflict (tracked forward)

`docs/symbol-onboarding-plan-v2.md` (K1, task `01a1206e-f436-7dd1-9604-ef91a0a76481`) assumes a
global feed of `1m,1h` bars for every onboarded symbol. With four feed timeframes
(`1m, 15m, 1h, 4h`) the plan must be revised: per-symbol bar volumes rise (~2× more kline
subscriptions and analytics-lake rows per symbol), reconciliation/backfill covers the extra
intervals, and the "top N symbols" capacity estimate must be recomputed against the
four-timeframe feed before any quarantine/re-onboard batch.

## 5. Operator next step (after merge; not performed here)

```bash
docker compose -f infra/docker-compose.yml up -d --no-deps --force-recreate --build ingest
```

Standing rules first: fresh PostgreSQL **and** Redis backup + independent Reality PASS, per
`docs/operator-release-checklist.md`. Post-deploy verify `cryptonite:stream:closed_bar_committed`
starts carrying 15m and 4h entries for BTCUSDT/ETHUSDT and that `ingest:universe` advertises the
four timeframes.