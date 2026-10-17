# Paper command queue: failed, unknown and stuck commands

Scope: the PAPER rows of `execution_commands`, processed one at a time by the orchestrator's `command_poller`
(`services/orchestrator/cryptonite_orchestrator/server.py`). The alerts below arrive as CRITICAL (or INFO) rows in
`notifications_outbox`, and on Telegram and ntfy when the notifier runs.

## What each alert means

| Alert (title)                                  | Kind             | Dedupe key                | Meaning and action                                  |
|------------------------------------------------|------------------|---------------------------|-----------------------------------------------------|
| Paper command failed                           | COMMAND_FAILED   | `command-failed:<id>`     | Raised a non-transient error, nothing was applied. Read `error_message`. Redo it from the UI if still wanted. |
| Paper command outcome unknown                  | COMMAND_FAILED   | `command-failed:<id>`     | Effects may exist. Held as UNKNOWN_PENDING_RECONCILIATION, never retried. **Section 3.** |
| A stop is waiting: CLOSE_POSITION ...          | STUCK_COMMAND    | `stuck-command:<id>`      | A close has waited over 30 s. The queue may be blocked. Section 2. |
| <TYPE> for <target> is PENDING/RUNNING ...     | STUCK_COMMAND    | `stuck-command:<id>`      | Any other command has waited over 120 s. Section 2. |
| <TYPE> ... is UNKNOWN_PENDING_RECONCILIATION   | STUCK_COMMAND    | `stuck-command:<id>`      | The held outcome. **Section 3.** |
| Paper command refused                          | COMMAND_REFUSED (INFO) | `command-refused:<id>` | A strategy command (pause, resume, stop, deploy) was sent to an AI mandate. Expected: mandates are managed in Mission Control. No action. |

Stuck-command alerts only consider PAPER commands queued in the last 24 hours, except an UNKNOWN CLOSE_POSITION, which
is alerted at any age until it is resolved (it still blocks stops). Any other older row is not alerted again: find it
with the query in section 1.

## 1. Find the commands

```sql
SELECT command_id, command_type, target_instance_id, status, command_outcome, attempt_count,
       socket_write_started, error_message, requester_id, payload, created_at, processed_at
FROM execution_commands
WHERE environment = 'PAPER'
  AND (command_outcome = 'UNKNOWN_PENDING_RECONCILIATION' OR status IN ('PENDING', 'CLAIMED', 'RUNNING', 'SENDING'))
ORDER BY created_at;
```

Each alert's `command_id` is in its `payload`. The orchestrator log also has one line per settled command:
`Command <command_id> settled as UNKNOWN: ...`.

## 2. A command is PENDING or RUNNING for a long time

- PENDING: the poller has not claimed it. Usually another command is ahead of it, or a row is locked. Check whether
  the head of the queue is an UNKNOWN row (section 3), and the orchestrator log for `Error in command poller`.
- RUNNING without the UNKNOWN outcome: a claim that never finished. Do not force it; escalate to the operator.

## 3. An UNKNOWN_PENDING_RECONCILIATION command

**Consequence.** For a CLOSE_POSITION, the exit monitor and the mandate monitor treat the latest close for the
instrument as in flight until it is REJECTED, FAILED or COMPLETED. While it is UNKNOWN, **all stops and flattens for
that instrument are blocked**. For other command types the row is held and never retried, and nothing else reads it.

**Inspect.** Find what the command touched. The poller does not store its generated order id, so match by instrument
and time. Use the instrument id as `paper_positions` stores it, and the account of the command (`PAPER-ACCOUNT-001`
at the time of writing).

```sql
-- the closing order written by a CLOSE_POSITION (client_order_id CLOSE-<8 hex>)
SELECT client_order_id, instance_id, instrument_id, side, quantity, avg_fill_price, status, created_at
FROM paper_orders
WHERE instrument_id = '<instrument_id>' AND client_order_id LIKE 'CLOSE-%' AND created_at >= '<command created_at>'
ORDER BY created_at;

-- the position it was meant to close
SELECT position_id, instance_id, instrument_id, side, quantity, status, opened_at, closed_at
FROM paper_positions
WHERE instrument_id = '<instrument_id>' AND account_id = 'PAPER-ACCOUNT-001'
ORDER BY opened_at;
```

The orchestrator keeps working orders and reservations in memory, and those are not visible in SQL. If the log shows
`Orchestrator processing CLOSE_POSITION ...` followed by an error for this command, the in-memory state may still hold
a reservation. Ask the operator before you resolve it.

**Decide.**

- The position is closed, and the close filled or was never needed: resolve as COMPLETED.
- The position is still OPEN and no close filled: resolve as FAILED. The monitors then issue a new close after their
  retry pause, which is what a stop should do.
- Anything else, such as a partial fill or a mismatch: do not resolve. Escalate.

**Resolve.** Run one statement at a time in a transaction, check that exactly one row changed, then commit. The
`error_message` is kept and the reason is appended, for the audit trail.

```sql
BEGIN;
UPDATE execution_commands
SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now(),
    error_message = coalesce(error_message, '') || ' | RESOLVED: <reason>'
WHERE command_id = '<command_id>' AND command_outcome = 'UNKNOWN_PENDING_RECONCILIATION';
-- expect UPDATE 1, then:
COMMIT;
```

For FAILED, use `status = 'FAILED', command_outcome = 'REJECTED'` in the same statement.

After the commit, the exit and mandate monitors read the new state on their next pass, so no restart is needed. The
stuck-command alert for the row stops being raised once the row leaves the queue states.

## 4. Why the queue cannot stay blocked

- A transient error (lost connection, deadlock, serialization failure) is retried up to 5 attempts, with a pause that
  grows with each attempt, and then settled FAILED. It is retried only while no effect may exist. After an effect
  (an order booked, a close applied) it is held as UNKNOWN, like any other error after an effect, unless the effect
  was undone again.
- Any other error settles the command at once: FAILED, or UNKNOWN when an effect may already exist. The poller then
  moves on to the next command.
- If the settlement itself fails, the poller waits (0.5 s, doubling, capped at 5 s) and tries again. The command is not
  run again meanwhile: its stored error is settled. After 3 failed settlements the command is closed by a bare status
  change: FAILED, or UNKNOWN when an effect may exist. The settlement runs with a 2 s lock timeout, so a row held by a
  half-open connection cannot stall the poller.
