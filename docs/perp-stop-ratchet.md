# Perp stop ratchet (auto-trade plan A4)

## What it does

Trails the resting STOP_MARKET of perp positions owned by an AI instance (`is_ai_instance`: `AI_MANDATE_<hex>` or the
legacy AI instance) behind the best mark since entry. The best mark is the peak for a long and the trough for a short.

It runs on every mark pass in `consume_perp_marks`, after `fire_resting_stops` and after the liquidation check
(`process_liquidations`). The pass has a budget of `STOP_RATCHET_TIMEOUT_S` (1 s, `asyncio.wait_for`). It reads Postgres
and Redis, which have no timeouts of their own, so a stall or a failure is logged and skipped, and never delays the
liquidations. The row write of a stop that already moved is shielded from that timeout.

Spot positions (no resting stops) and manual or strategy perp positions are not touched.
The exit monitor's percentage trailing stop is unchanged and still runs. It is a backup for AI perps, and the only trail for spot.

## Rule

- **Activation.** The best mark's favourable move from entry must reach `exit.trail_activate_pct`. The trail also arms
  at once when the owner is in the target-met set.
- **New stop.**
  - Long: `max(current, best x (1 - trail_pct/100))`, rounded down to the tick.
  - Short: `min(current, best x (1 + trail_pct/100))`, rounded up to the tick.
- **Never loosens.** A move is at least one tick, and only tighter.
- **Rate limit.** At most once per `STOP_RATCHET_MIN_INTERVAL_S` (10 s) per position. A refused move counts as a move
  for this: it is retried after the interval, not on every pass.
- **Best mark sampling.** The best mark is sampled once per liquidation interval (about 1 s), from the marks that are
  fresh at that pass. A peak between two samples is missed, so the trail can sit lower than a tick-by-tick trail would.
  It is never looser than the current stop.
- **Never at the mark.** A stop that would be at or through the mark is not placed. The fire path and the exit monitor
  handle that position.

## Add to a position

An entry that carries a `stop_loss` re-attaches the position's stop (`attach_resting_stop`), which replaces the owner's
stop for the whole, larger position. It never loosens an existing stop: if the owner already has a stop on that
instrument, the stop placed is `max(existing, new)` for a long and `min(existing, new)` for a short. A tighter new stop
wins. If the existing stop is tighter, it stays as it is when it already covers the whole position (no new row), else
it is placed again at its own price for the full size. A first entry has no stop yet and places the entry's
`stop_loss` as given. Example: a long ratcheted to 50,960 that adds with `stop_loss` 48,000 keeps 50,960.

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
- `STOP_RATCHET_TIMEOUT_S` = 1

**Tick (fallback caveat).** The tick is `instruments.price_increment` of the perp id, or else of its spot sibling. The
ingest records spot instruments only, so in practice the spot tick is used. The spot tick can be finer than the USD-M
tick. This is harmless on paper, but a live venue would refuse or round a stop price off its own grid. A perp without a
spot sibling has no known tick and is not trailed (logged). Both must be revisited before Stage 6 or any live venue.
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
`restore_pending_stops` places it only while the ingress is fresh (a stale restart pass keeps it pending, CRITICAL
and an alert after `STOP_RESTORE_STALE_ALERT_S`); it closes the position only when the refusal is `STOP_WOULD_TRIGGER_IMMEDIATELY`.

If the row write fails, it is logged at CRITICAL (the ratchet progress would be lost on a restart). The stop still rests
in memory and the old row stays ACCEPTED. A restart in that window
would restore the older, looser stop; once any later reconcile has cancelled that row, the restore falls back
to the entry order's `stop_loss`, as before the ratchet.

## State

- Redis hash `perp:stop_ratchet:{account}:{instrument}` with `instance_id`, `side`, `entry` and `best`.
- TTL is 14 days.
- A record whose owner, side or entry differs from the open position belongs to a different position. It is ignored,
  and the trail restarts from the current mark.
- Rate-limit timestamps are kept in memory only.
- At the start of each ratchet pass the trail (memory and Redis hash) of every position that is closed, or whose owner
  has no resting stop on it, is dropped. A position reopened at the same entry starts fresh. A hash left by a position
  that closed while the orchestrator was down is only replaced by a position with another owner, side or entry.
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
