# Perp stop ratchet (auto-trade plan A4)

## What it does

Trails the resting STOP_MARKET of perp positions owned by an AI instance (`is_ai_instance`: `AI_MANDATE_<hex>` or the
legacy AI instance) behind the best mark since entry. The best mark is the peak for a long and the trough for a short.

It runs on every mark pass in `consume_perp_marks`, right after `fire_resting_stops` and before the liquidation check.
A failure is logged and never skips the liquidation check.

Spot positions (no resting stops) and manual or strategy perp positions are not touched.
The exit monitor's percentage trailing stop is unchanged and still runs. It is a backup for AI perps, and the only trail for spot.

## Rule

- **Activation.** The best mark's favourable move from entry must reach `exit.trail_activate_pct`. The trail also arms
  at once when the owner is in the target-met set.
- **New stop.**
  - Long: `max(current, best x (1 - trail_pct/100))`, rounded down to the tick.
  - Short: `min(current, best x (1 + trail_pct/100))`, rounded up to the tick.
- **Never loosens.** A move is at least one tick, and only tighter.
- **Rate limit.** At most once per `STOP_RATCHET_MIN_INTERVAL_S` (10 s) per position.
- **Never at the mark.** A stop that would be at or through the mark is not placed. The fire path and the exit monitor
  handle that position.

## Settings

| Key | Env default | Use |
| --- | --- | --- |
| `exit.trail_activate_pct` | `EXIT_TRAIL_ACTIVATE_PCT` (3) | Favourable move that arms the trail |
| `exit.trail_pct` | `EXIT_TRAIL_PCT` (2) | Distance of the stop behind the best mark |

These are the exit monitor's keys, env defaults and validation (`settled`). The ratchet re-reads them every
`STOP_RATCHET_SETTINGS_TTL_S` (30 s). A refused or unreadable set keeps the last valid one.

Other constants in `server.py`:

- `STOP_RATCHET_MIN_INTERVAL_S` = 10
- `STOP_RATCHET_SETTINGS_TTL_S` = 30
- `STOP_RATCHET_TICK_RETRY_S` = 300

**Tick.** The tick is `instruments.price_increment` of the perp id, or else of its spot sibling. The ingest records spot
instruments only, so in practice the spot tick is used. It can be finer than the USD-M tick. This is harmless on paper,
but must be revisited before any live venue. If no tick is known, the instrument is not trailed and this is logged.
The lookup is retried after `STOP_RATCHET_TICK_RETRY_S`.

## Moving a stop

1. `place_resting_stop` is called with a new client id. In one synchronous dispatcher call, it places the new stop and
   releases the owner's old stop on that instrument. There is no await between the two, so there is no window without a stop.
   If the new stop is refused, the old one is put back and kept.
2. `record_resting_stop` writes the new `paper_orders` row as ACCEPTED.
3. `reconcile_working_orders` cancels the old row, which has no working order in memory any more.

The `paper_orders` history is the audit trail. Each move is logged at INFO as old -> new.

The restart restore (#110, `load_stop_restores`) takes the newest ACCEPTED STOP_MARKET row. That row carries the
ratcheted price.

If the row write fails, the stop still rests in memory and the old row stays ACCEPTED. A restart in that window
would restore the older, looser stop; once any later reconcile has cancelled that row, the restore falls back
to the entry order's `stop_loss`, as before the ratchet.

## State

- Redis hash `perp:stop_ratchet:{account}:{instrument}` with `instance_id`, `side`, `entry` and `best`.
- TTL is 14 days.
- A record whose owner, side or entry differs from the open position belongs to a different position. It is ignored,
  and the trail restarts from the current mark.
- Rate-limit timestamps are kept in memory only.
- No migration. The code adds no table or column.

## Contract with the mandate session code

The Redis set `mandate:session:target_met` is written by the mandate session work on branch `feat/auto-trade-phase-a`.
Its members are `AI_MANDATE_<hex>`, and it expires at 00:00 UTC.

This code only reads it, with `SISMEMBER`. A missing set or a failed read is treated as not met.

## Not verified in the cloud

- The live Binance mark feed.
- A real orchestrator restart on the deployed Docker stack.
- Binance USD-M tick sizes. There is no exchangeInfo access here.
- The interaction with the real mandate session code that writes the target-met set. It is only simulated in tests.

Tests: tests/test_perp_stop_ratchet.py
