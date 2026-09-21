# Kill-Switch Runbook

## Description
The Kill-Switch is a durable mechanism to halt all automated trading and order admission across the fleet immediately. It uses a generation-based latch. When tripped, the current generation is latched, and no strategies can place new orders. The Executor is responsible for asynchronously shutting down active producers and cancelling outstanding venue orders.

## Triggering the Kill-Switch
**Emergency Trigger (API / UI)**
Operators can trigger the kill switch via the UI dashboard (red SHIELD icon) or via the API:
```bash
curl -X POST https://api.cryptonite.com/api/v1/paper/kill-switch \
     -H "Cookie: cryptonite_session=<session>" \
     -H "X-CSRF-Token: <csrf>" \
     -d '{"operator": "<your_id>", "reason": "Flash crash anomaly"}'
```

**Database Direct Trigger (Break Glass)**
If the API is unreachable, an administrator with `cryptonite_api` role access can invoke the `SECURITY DEFINER` function directly:
```sql
SELECT trip_kill_switch('<account_id>', 'production', 'Direct DB Break Glass', '127.0.0.1', '<admin_uuid>');
```

## Validating the Trip
To verify the kill-switch has fenced ingress:
1. **API**: `GET /api/v1/paper/kill-switch` should show `is_latched: true`.
2. **Logs**: Filter for `FENCING_TRIPPED` in the Executor logs.
3. **Redis**: `GET kill_switch_gen:<account>:<env>` should reflect the new generation.

## Resetting the Kill-Switch
Resetting requires a CAS (Compare-And-Swap) operation against the current generation. **Do NOT reset unless all open positions and orders have been manually reconciled and the anomaly is resolved.**

**API / UI:**
```bash
curl -X POST https://api.cryptonite.com/api/v1/paper/kill-switch/reset \
     -H "Cookie: cryptonite_session=<session>" \
     -H "X-CSRF-Token: <csrf>" \
     -d '{"operator": "<your_id>"}'
```
The API ensures the reset is safe by verifying zero open exposure before enqueuing the `RESET_SWITCH` command.

