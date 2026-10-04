"""Approvals with expiry and a timeout policy (AI-first blueprint D8, §4.5, A5.3).

An approval holds one AI action (a ``SUBMIT_ORDER`` that adds risk or a ``CLOSE_POSITION`` that
reduces it) until the operator decides or its deadline passes:

- approve -> the command is queued once (``idempotency_key``), bound to the approval by ``command_id``;
- veto -> nothing happens;
- timeout -> the policy decides: DENY (new risk at L1, or at L2 outside active hours) means no;
  PROCEED (risk reducing, or an L2 veto window inside active hours) runs the action.

Decisions are claimed with ``SELECT ... FOR UPDATE``, so a web approve, a Telegram veto and the
sweeper can race and exactly one wins. Everything here runs in the caller's connection.
"""

import datetime
import json
import uuid
from dataclasses import dataclass
from typing import Any

from cryptonite_schemas.mandate import (
    DEFAULT_APPROVAL_TTL_S,
    DEFAULT_REDUCE_VETO_S,
    ActionClass,
    ApprovalStatus,
    AutonomyLevel,
    Mandate,
    TimeoutPolicy,
    in_active_hours,
    mandate_instance_id,
    timeout_policy_for,
    timeout_status,
)

APPROVAL_COLUMNS = ("id, mandate_id, action_class, command_type, payload, summary, source, source_ref, level_at_creation, "
                    "timeout_policy, expires_at, status, decided_via, decided_by, decided_at, command_id, trace_id, created_at")


class ApprovalConflict(Exception):
    """The approval is no longer PENDING (already decided, vetoed, expired or cancelled)."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


@dataclass(frozen=True)
class Decision:
    approval_id: uuid.UUID
    status: str
    command_id: uuid.UUID | None
    replayed: bool = False


def approval_ttl_s(action: ActionClass, mandate: Mandate) -> int:
    if action is ActionClass.REDUCE_RISK:
        return DEFAULT_REDUCE_VETO_S
    return mandate.veto_window_s if mandate.autonomy_level == AutonomyLevel.L2 else DEFAULT_APPROVAL_TTL_S


def _jsonb(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def as_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = _jsonb(d.get("payload")) or {}
    return d


async def create_approval(conn, mandate: Mandate, action: ActionClass, command_type: str, payload: dict[str, Any], *,
                          summary: str, source: str, source_ref: str, now: datetime.datetime | None = None,
                          trace_id: uuid.UUID | None = None, notify: bool = True) -> dict[str, Any] | None:
    """A PENDING approval (and its Action notification). None when the action needs no approval
    (ADD_RISK at L3). A producer retry returns the existing row."""
    now = now or datetime.datetime.now(datetime.UTC)
    policy = timeout_policy_for(action, mandate.autonomy_level, in_active_hours(mandate.active_hours_utc, now))
    if policy is None:
        return None
    expires = now + datetime.timedelta(seconds=approval_ttl_s(action, mandate))
    row = await conn.fetchrow(
        f"""INSERT INTO ai_approvals (mandate_id, action_class, command_type, payload, summary, source, source_ref,
                level_at_creation, timeout_policy, expires_at, trace_id)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (source, source_ref) DO NOTHING RETURNING {APPROVAL_COLUMNS}""",
        mandate.id, action.value, command_type, json.dumps({**payload, "mandate_id": str(mandate.id)}, default=str),
        summary, source, source_ref, int(mandate.autonomy_level), policy.value, expires, trace_id,
    )
    if row is None:
        row = await conn.fetchrow(f"SELECT {APPROVAL_COLUMNS} FROM ai_approvals WHERE source = $1 AND source_ref = $2",
                                  source, source_ref)
        return as_dict(row)
    approval = as_dict(row)
    if notify:
        await notify_approval(conn, approval, mandate)
    return approval


async def notify_approval(conn, approval: dict[str, Any], mandate: Mandate) -> None:
    await conn.execute(
        """INSERT INTO notifications_outbox (tier, kind, title, body, payload, approval_id, dedupe_key)
           VALUES ('ACTION', 'APPROVAL', $1, $2, $3::jsonb, $4, $5) ON CONFLICT (dedupe_key) DO NOTHING""",
        f"{mandate.name} (L{int(mandate.autonomy_level)})", approval["summary"],
        json.dumps({"mandate": mandate.name, "level": int(mandate.autonomy_level), "action_class": approval["action_class"],
                    "timeout_policy": approval["timeout_policy"], "expires_at": approval["expires_at"].isoformat(),
                    "trace_id": str(approval["trace_id"]) if approval.get("trace_id") else None}),
        approval["id"], f"approval:{approval['id']}",
    )


async def record_operator_approval(conn, mandate: Mandate, command_type: str, payload: dict[str, Any], *, summary: str,
                                   source: str, source_ref: str, decided_by: str, via: str = "web",
                                   idempotency_key: str | None = None, trace_id: uuid.UUID | None = None,
                                   action: ActionClass = ActionClass.ADD_RISK) -> Decision:
    """An action the operator approved on the spot (a Brain order card, a debate verdict): recorded
    APPROVED and its command queued, once. A retry returns the first decision."""
    now = datetime.datetime.now(datetime.UTC)
    policy = TimeoutPolicy.DENY if action is ActionClass.ADD_RISK else TimeoutPolicy.PROCEED
    row = await conn.fetchrow(
        f"""INSERT INTO ai_approvals (mandate_id, action_class, command_type, payload, summary, source, source_ref,
                level_at_creation, timeout_policy, expires_at, status, decided_via, decided_by, decided_at, trace_id)
            VALUES ($1, $12, $2, $3::jsonb, $4, $5, $6, $7, $13, $8, 'APPROVED', $9, $10, $8, $11)
            ON CONFLICT (source, source_ref) DO NOTHING RETURNING {APPROVAL_COLUMNS}""",
        mandate.id, command_type, json.dumps({**payload, "mandate_id": str(mandate.id)}, default=str), summary, source,
        source_ref, int(mandate.autonomy_level), now, via, decided_by, trace_id, action.value, policy.value,
    )
    if row is None:
        existing = as_dict(await conn.fetchrow(
            f"SELECT {APPROVAL_COLUMNS} FROM ai_approvals WHERE source = $1 AND source_ref = $2 FOR UPDATE", source, source_ref))
        if existing["status"] != ApprovalStatus.APPROVED:
            raise ApprovalConflict(existing["status"])
        if existing["command_id"] is None:
            cid = await enqueue_approval_command(conn, existing, idempotency_key=idempotency_key)
            return Decision(existing["id"], existing["status"], cid, replayed=True)
        return Decision(existing["id"], existing["status"], existing["command_id"], replayed=True)
    approval = as_dict(row)
    cid = await enqueue_approval_command(conn, approval, idempotency_key=idempotency_key)
    return Decision(approval["id"], ApprovalStatus.APPROVED, cid)


async def enqueue_approval_command(conn, approval: dict[str, Any], *, idempotency_key: str | None = None) -> uuid.UUID:
    """Queue the approval's command exactly once and bind it to the approval."""
    key = idempotency_key or f"approval:{approval['id']}"
    payload = {**approval["payload"], "approval_id": str(approval["id"]), "mandate_id": str(approval["mandate_id"])}
    payload.setdefault("account_id", "PAPER-ACCOUNT-001")
    requester = f"approval:{approval.get('decided_via') or 'timeout'}:{approval.get('decided_by') or 'system'}"
    cid = await conn.fetchval(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, requester_id,
               payload, status, environment, account_id, mandate_id)
           VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'PENDING', 'PAPER', $7, $8)
           ON CONFLICT (idempotency_key) DO NOTHING RETURNING command_id""",
        uuid.uuid4(), key, approval["command_type"], mandate_instance_id(approval["mandate_id"]), requester[:128],
        json.dumps(payload, default=str), payload["account_id"], approval["mandate_id"],
    )
    if cid is None:
        cid = await conn.fetchval("SELECT command_id FROM execution_commands WHERE idempotency_key = $1", key)
    await conn.execute("UPDATE ai_approvals SET command_id = $2 WHERE id = $1", approval["id"], cid)
    return cid


async def decide_approval(conn, approval_id: uuid.UUID, approve: bool, *, via: str, decided_by: str,
                          now: datetime.datetime | None = None) -> Decision:
    """Approve or veto a PENDING approval. Idempotent for the same decision. A late decision on an
    expired approval applies (and commits) the timeout policy instead, then raises ``ApprovalConflict``."""
    now = now or datetime.datetime.now(datetime.UTC)
    wanted = ApprovalStatus.APPROVED if approve else ApprovalStatus.VETOED
    conflict: str | None = None
    async with conn.transaction():
        row = await conn.fetchrow(f"SELECT {APPROVAL_COLUMNS} FROM ai_approvals WHERE id = $1 FOR UPDATE", approval_id)
        if row is None:
            raise LookupError(str(approval_id))
        a = as_dict(row)
        if a["status"] != ApprovalStatus.PENDING:
            if a["status"] != wanted:
                raise ApprovalConflict(a["status"])
            result = Decision(a["id"], a["status"], a["command_id"], replayed=True)
        elif a["expires_at"] <= now:
            conflict = await _apply_timeout(conn, a, now)
        else:
            await conn.execute(
                "UPDATE ai_approvals SET status = $2, decided_via = $3, decided_by = $4, decided_at = $5 WHERE id = $1",
                a["id"], wanted.value, via, decided_by[:128], now,
            )
            a.update(status=wanted.value, decided_via=via, decided_by=decided_by)
            cid = await enqueue_approval_command(conn, a) if approve else None
            await _notify_outcome(conn, a, wanted.value)
            result = Decision(a["id"], wanted.value, cid)
    if conflict is not None:
        raise ApprovalConflict(conflict)
    return result


async def _apply_timeout(conn, a: dict[str, Any], now: datetime.datetime | None = None) -> str:
    policy = TimeoutPolicy(a["timeout_policy"])
    if policy is TimeoutPolicy.PROCEED and a["action_class"] == ActionClass.ADD_RISK:
        # An L2 veto window proceeds only if the deadline itself falls inside the operator's active
        # hours (the policy was chosen at creation; a window can run past the end of the hours)
        hours = await conn.fetchval("SELECT active_hours_utc FROM ai_mandates WHERE id = $1", a["mandate_id"])
        if not in_active_hours(hours, now or datetime.datetime.now(datetime.UTC)):
            policy = TimeoutPolicy.DENY
    status = timeout_status(policy)
    await conn.execute(
        "UPDATE ai_approvals SET status = $2, decided_via = 'timeout', decided_by = 'system', decided_at = now() WHERE id = $1",
        a["id"], status.value,
    )
    a.update(status=status.value, decided_via="timeout", decided_by="system")
    if status is ApprovalStatus.EXPIRED_PROCEEDED:
        await enqueue_approval_command(conn, a)
    await _notify_outcome(conn, a, status.value)
    return status.value


async def _sync_source(conn, a: dict[str, Any], status: str) -> None:
    """A debate's run follows its approval: COMPLETED when the action ran, REJECTED when it did not."""
    if a.get("source") not in ("debate", "exit_debate"):
        return
    try:
        run_id = uuid.UUID(str(a["source_ref"]))
    except ValueError:
        return
    ran = status in (ApprovalStatus.APPROVED, ApprovalStatus.EXPIRED_PROCEEDED)
    await conn.execute(
        "UPDATE debate_runs SET status = $2, completed_at = now() WHERE id = $1 AND status = 'PENDING_APPROVAL'",
        run_id, "COMPLETED" if ran else "REJECTED",
    )


async def _notify_outcome(conn, a: dict[str, Any], status: str) -> None:
    await _sync_source(conn, a, status)
    text = {"APPROVED": "approved", "VETOED": "vetoed", "EXPIRED": "expired: timeout means no",
            "EXPIRED_PROCEEDED": "timed out: proceeding"}.get(status, status.lower())
    await conn.execute(
        """INSERT INTO notifications_outbox (tier, kind, title, body, payload, approval_id, dedupe_key)
           VALUES ('INFO', 'APPROVAL_OUTCOME', $1, $2, $3::jsonb, $4, $5) ON CONFLICT (dedupe_key) DO NOTHING""",
        f"{a['action_class']} {text}", a["summary"], json.dumps({"status": status, "via": a.get("decided_via")}),
        a["id"], f"approval-outcome:{a['id']}",
    )


async def sweep_expired(conn, now: datetime.datetime | None = None, limit: int = 50) -> list[tuple[uuid.UUID, str]]:
    """Apply the timeout policy to every PENDING approval past its deadline (the orchestrator runs this)."""
    now = now or datetime.datetime.now(datetime.UTC)
    done: list[tuple[uuid.UUID, str]] = []
    async with conn.transaction():
        rows = await conn.fetch(
            f"""SELECT {APPROVAL_COLUMNS} FROM ai_approvals WHERE status = 'PENDING' AND expires_at <= $1
                ORDER BY expires_at LIMIT $2 FOR UPDATE SKIP LOCKED""", now, limit)
        for row in rows:
            a = as_dict(row)
            done.append((a["id"], await _apply_timeout(conn, a, now)))
    return done
