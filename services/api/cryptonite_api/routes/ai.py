"""Mandates, approvals and system status for Mission Control (AI-first blueprint §4.4–§4.6, §7.1; A5.1–A5.6).

- Mandates: create and loosen only with operator TOTP (they give the AI more room); tighten, pause and
  demote without it. Autonomy level and status never change through an edit, only through the
  autonomy endpoints (promotion needs a passing gate and TOTP) or the risk monitor (halts, demotions).
- Approvals: the operator approves or vetoes a PENDING approval from the web (session + CSRF) or from
  Telegram through the notifier (HMAC-signed, single-use nonce; approving new risk needs TOTP there).
  Decisions are server-side and idempotent (``cryptonite_execution.approvals``).
"""

import datetime
import json
import logging
import os
import re
import uuid
from decimal import Decimal
from typing import Any, Literal

from cryptonite_api.routes.auth import OPERATOR_WRITE, require_operator
from cryptonite_execution.approvals import (
    APPROVAL_COLUMNS,
    ApprovalConflict,
    as_dict,
    decide_approval,
)
from cryptonite_execution.autonomy import (
    AutonomyState,
    AutonomyTrigger,
    TransitionRefused,
    transition,
)
from cryptonite_execution.mandate_store import (
    MANDATE_COLUMNS,
    apply_transition,
    level_name,
    load_mandate,
    record_event,
)
from cryptonite_schemas import notifier as notifier_contract
from cryptonite_schemas.mandate import (
    MAX_DRAWDOWN_PCT_CAP,
    MAX_PERP_LEVERAGE_CAP,
    MAX_POSITION_PCT_CAP,
    ActionClass,
    Mandate,
    MandateStatus,
    risk_adding_change,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("cryptonite.api.ai")

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])

EXIT_MONITOR_HEARTBEAT = "exit_monitor:heartbeat"
NOTIFIER_HEARTBEAT = "notifier:heartbeat"
TOTP_RE = re.compile(r"^\d{6}$")


class MandateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    capital_budget_usd: Decimal = Field(gt=0)
    instruments: list[str] = Field(min_length=1, max_length=20)
    max_position_pct: Decimal = Field(ge=1, le=MAX_POSITION_PCT_CAP)
    max_drawdown_pct: Decimal = Field(default=MAX_DRAWDOWN_PCT_CAP, gt=0, le=MAX_DRAWDOWN_PCT_CAP)
    allow_perps: bool = False
    max_leverage: int = Field(default=1, ge=1, le=MAX_PERP_LEVERAGE_CAP)
    strategies: list[str] = Field(default_factory=list, max_length=20)
    active_hours_utc: str | None = None
    veto_window_s: int = Field(default=600, ge=60, le=3600)
    totp_code: str | None = None


class MandatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    capital_budget_usd: Decimal | None = Field(default=None, gt=0)
    instruments: list[str] | None = Field(default=None, min_length=1, max_length=20)
    max_position_pct: Decimal | None = Field(default=None, ge=1, le=MAX_POSITION_PCT_CAP)
    max_drawdown_pct: Decimal | None = Field(default=None, gt=0, le=MAX_DRAWDOWN_PCT_CAP)
    allow_perps: bool | None = None
    max_leverage: int | None = Field(default=None, ge=1, le=MAX_PERP_LEVERAGE_CAP)
    strategies: list[str] | None = Field(default=None, max_length=20)
    active_hours_utc: str | None = None
    clear_active_hours: bool = False
    veto_window_s: int | None = Field(default=None, ge=60, le=3600)
    totp_code: str | None = None


class TotpIn(BaseModel):
    totp_code: str | None = None
    reason: str | None = Field(default=None, max_length=300)


class DecisionIn(BaseModel):
    decision: Literal["APPROVE", "VETO"]


class NotifierDecisionIn(BaseModel):
    decision: Literal["APPROVE", "VETO"]
    chat_id: str = Field(min_length=1, max_length=32)
    totp_code: str | None = None


def actor(auth: dict) -> str:
    return str(auth.get("username") or auth.get("user_id") or f"machine:{auth.get('role', 'operator')}")


async def verify_totp(request: Request, code: str | None, *, user_id: Any = None, username: str | None = None) -> bool:
    """The operator's current TOTP code (replay-protected; the dev-only 000000 follows the login policy)."""
    if not code or not TOTP_RE.match(code):
        return False
    auth_mgr = request.app.state.auth_manager
    async with request.app.state.pool.acquire() as conn:
        if user_id:
            row = await conn.fetchrow(
                "SELECT id, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag FROM v_operator_auth "
                "WHERE id = $1 AND is_active = true", uuid.UUID(str(user_id)))
        elif username:
            row = await conn.fetchrow(
                "SELECT id, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag FROM v_operator_auth "
                "WHERE username = $1 AND is_active = true", username)
        else:
            return False
    if row is None:
        return False
    if code == "000000":
        return await auth_mgr.verify_totp_code(row["id"], "", code)
    if not row["totp_secret_ciphertext"]:
        return False
    try:
        secret = auth_mgr.decrypt_totp_secret(row["totp_secret_ciphertext"], row["totp_secret_nonce"], row["totp_secret_auth_tag"])
    except Exception:
        logger.exception("Could not decrypt the TOTP secret")
        return False
    return await auth_mgr.verify_totp_code(row["id"], secret, code)


async def require_totp(request: Request, auth: dict, code: str | None) -> None:
    if not auth.get("user_id"):
        raise HTTPException(status_code=403, detail="TOTP_REQUIRED: a machine token cannot add risk")
    if not await verify_totp(request, code, user_id=auth["user_id"]):
        raise HTTPException(status_code=401, detail="TOTP_REQUIRED: this change gives the AI more room; enter your current TOTP code")


def _redis(request: Request):
    hub = getattr(request.app.state, "stream_hub", None)
    return getattr(hub, "redis", None)


async def _publish_updated(request: Request, mandate_id: uuid.UUID, change: str) -> None:
    r = _redis(request)
    if r is not None:
        try:
            await r.publish("channel:system_alerts", json.dumps({"type": "mandate_updated", "mandate_id": str(mandate_id), "change": change}))
        except Exception:
            pass


def _dec(v: Any) -> str | None:
    return None if v is None else str(v)


async def mandate_view(conn, row: Any) -> dict[str, Any]:
    m = Mandate.from_row(row)
    eq = await conn.fetchrow(
        "SELECT equity, hwm, drawdown_pct, ts FROM ai_mandate_equity WHERE mandate_id = $1 ORDER BY ts DESC, id DESC LIMIT 1", m.id)
    exposure = await conn.fetchval(
        "SELECT COALESCE(SUM(quantity * current_price), 0) FROM paper_positions WHERE instance_id = $1 AND status = 'OPEN'",
        m.instance_id)
    positions = await conn.fetch(
        """SELECT instrument_id, side, quantity, entry_price, current_price FROM paper_positions
           WHERE instance_id = $1 AND status = 'OPEN' AND quantity > 0 ORDER BY instrument_id""", m.instance_id)
    pending = await conn.fetchval("SELECT count(*) FROM ai_approvals WHERE mandate_id = $1 AND status = 'PENDING'", m.id)
    proposal = await conn.fetchrow(
        """SELECT id, to_level, detail, created_at FROM ai_autonomy_events
           WHERE mandate_id = $1 AND kind = 'PROPOSED' AND proposal_status = 'PENDING'""", m.id)
    extra = await conn.fetchrow(
        "SELECT halted_reason, halted_at, level_since, created_at, updated_at FROM ai_mandates WHERE id = $1", m.id)
    return {
        "id": str(m.id), "name": m.name, "capital_budget_usd": str(m.capital_budget_usd), "instruments": m.instruments,
        "max_position_pct": str(m.max_position_pct), "max_drawdown_pct": str(m.max_drawdown_pct),
        "allow_perps": m.allow_perps, "max_leverage": m.max_leverage, "strategies": m.strategies,
        "autonomy_level": int(m.autonomy_level), "level_name": level_name(int(m.autonomy_level)), "status": m.status.value,
        "active_hours_utc": m.active_hours_utc, "veto_window_s": m.veto_window_s, "instance_id": m.instance_id,
        "halted_reason": extra["halted_reason"], "halted_at": extra["halted_at"].isoformat() if extra["halted_at"] else None,
        "level_since": extra["level_since"].isoformat(),
        "equity": _dec(eq["equity"]) if eq else str(m.capital_budget_usd), "hwm": _dec(eq["hwm"]) if eq else str(m.capital_budget_usd),
        "drawdown_pct": _dec(eq["drawdown_pct"]) if eq else "0", "equity_ts": eq["ts"].isoformat() if eq else None,
        "exposure_usd": str(exposure), "budget_used_pct": str(round(Decimal(str(exposure)) / m.capital_budget_usd * 100, 2)),
        "positions": [{"instrument_id": p["instrument_id"], "side": p["side"], "quantity": str(p["quantity"]),
                       "entry_price": str(p["entry_price"]), "current_price": str(p["current_price"])} for p in positions],
        "pending_approvals": pending,
        "pending_proposal": {"id": proposal["id"], "to_level": proposal["to_level"],
                             "created_at": proposal["created_at"].isoformat()} if proposal else None,
    }


@router.get("/mandates")
async def list_mandates(request: Request, auth: dict = Depends(require_operator)):
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE status <> 'ARCHIVED' ORDER BY created_at")
        return {"mandates": [await mandate_view(conn, r) for r in rows]}


@router.get("/mandates/{mandate_id}")
async def get_mandate(mandate_id: uuid.UUID, request: Request, auth: dict = Depends(require_operator)):
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE id = $1", mandate_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Mandate not found")
        view = await mandate_view(conn, row)
        view["equity_curve"] = [
            {"ts": r["ts"].isoformat(), "equity": str(r["equity"]), "hwm": str(r["hwm"]),
             "benchmark_price": _dec(r["benchmark_price"])}
            for r in reversed(await conn.fetch(
                "SELECT ts, equity, hwm, benchmark_price FROM ai_mandate_equity WHERE mandate_id = $1 ORDER BY ts DESC LIMIT 500",
                mandate_id))
        ]
        view["events"] = [
            {"id": r["id"], "kind": r["kind"], "from_level": r["from_level"], "to_level": r["to_level"], "reason": r["reason"],
             "actor": r["actor"], "proposal_status": r["proposal_status"], "created_at": r["created_at"].isoformat()}
            for r in await conn.fetch(
                "SELECT * FROM ai_autonomy_events WHERE mandate_id = $1 ORDER BY id DESC LIMIT 50", mandate_id)
        ]
    return view


@router.post("/mandates", dependencies=OPERATOR_WRITE, status_code=201)
async def create_mandate(req: MandateIn, request: Request, auth: dict = Depends(require_operator)):
    """A new mandate always starts at L1 Copilot; creating one gives the AI room, so it needs TOTP."""
    if req.allow_perps:
        raise HTTPException(status_code=422, detail="MANDATE_PERPS_NOT_ALLOWED: perps can be enabled only after L3 (O1)")
    await require_totp(request, auth, req.totp_code)
    try:
        m = Mandate(id=uuid.uuid4(), **req.model_dump(exclude={"totp_code"}))
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e.errors()[0]["msg"]))
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            try:
                await conn.execute(
                    """INSERT INTO ai_mandates (id, name, capital_budget_usd, instruments, max_position_pct, max_drawdown_pct,
                           allow_perps, max_leverage, strategies, active_hours_utc, veto_window_s, created_by)
                       VALUES ($1, $2, $3, $4, $5, $6, false, $7, $8, $9, $10, $11)""",
                    m.id, m.name, m.capital_budget_usd, m.instruments, m.max_position_pct, m.max_drawdown_pct,
                    m.max_leverage, m.strategies, m.active_hours_utc, m.veto_window_s, actor(auth))
            except Exception as e:
                if "uq" in str(e).lower() or "unique" in str(e).lower():
                    raise HTTPException(status_code=409, detail="A mandate with this name exists")
                raise
            await record_event(conn, m.id, "CREATED", to_level=1, reason="created", actor=actor(auth),
                               detail=json.loads(m.model_dump_json()))
            row = await conn.fetchrow(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE id = $1", m.id)
            view = await mandate_view(conn, row)
    await _publish_updated(request, m.id, "created")
    return view


@router.patch("/mandates/{mandate_id}", dependencies=OPERATOR_WRITE)
async def edit_mandate(mandate_id: uuid.UUID, req: MandatePatch, request: Request, auth: dict = Depends(require_operator)):
    """Tightening is free; anything that gives the AI more room needs TOTP."""
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            old = await load_mandate(conn, mandate_id, for_update=True)
            if old is None:
                raise HTTPException(status_code=404, detail="Mandate not found")
            if old.status is MandateStatus.ARCHIVED:
                raise HTTPException(status_code=409, detail="The mandate is archived")
            patch = req.model_dump(exclude_none=True, exclude={"totp_code", "clear_active_hours"})
            if req.clear_active_hours:
                patch["active_hours_utc"] = None
            try:
                new = Mandate(**{**old.model_dump(), **patch})
            except ValidationError as e:
                raise HTTPException(status_code=422, detail=str(e.errors()[0]["msg"]))
            if risk_adding_change(old, new):
                await require_totp(request, auth, req.totp_code)
            await conn.execute(
                """UPDATE ai_mandates SET name = $2, capital_budget_usd = $3, instruments = $4, max_position_pct = $5,
                       max_drawdown_pct = $6, allow_perps = $7, max_leverage = $8, strategies = $9, active_hours_utc = $10,
                       veto_window_s = $11, updated_at = now() WHERE id = $1""",
                mandate_id, new.name, new.capital_budget_usd, new.instruments, new.max_position_pct, new.max_drawdown_pct,
                new.allow_perps, new.max_leverage, new.strategies, new.active_hours_utc, new.veto_window_s)
            changed = {k: [str(getattr(old, k)), str(getattr(new, k))] for k in patch if getattr(old, k) != getattr(new, k)}
            await record_event(conn, mandate_id, "EDITED", reason=", ".join(changed) or "no change", actor=actor(auth),
                               detail={"changes": changed, "risk_adding": risk_adding_change(old, new)})
            view = await mandate_view(conn, await conn.fetchrow(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE id = $1", mandate_id))
    await _publish_updated(request, mandate_id, "edited")
    return view


async def _transition(request: Request, auth: dict, mandate_id: uuid.UUID, trigger: AutonomyTrigger, *,
                      totp_ok: bool = False, reason: str | None = None) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            m = await load_mandate(conn, mandate_id, for_update=True)
            if m is None:
                raise HTTPException(status_code=404, detail="Mandate not found")
            try:
                t = transition(AutonomyState(m.autonomy_level, m.status, m.allow_perps), trigger, totp_ok=totp_ok,
                               reason=reason or "")
            except TransitionRefused as e:
                raise HTTPException(status_code=409, detail=e.code)
            await apply_transition(conn, m.id, t, actor=actor(auth))
            if trigger is AutonomyTrigger.PAUSE:
                await conn.execute(
                    """UPDATE ai_approvals SET status = 'CANCELLED', decided_via = 'system', decided_at = now()
                       WHERE mandate_id = $1 AND status = 'PENDING' AND action_class = 'ADD_RISK'""", m.id)
            view = await mandate_view(conn, await conn.fetchrow(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE id = $1", m.id))
    await _publish_updated(request, mandate_id, t.kind.lower())
    return view


@router.post("/mandates/{mandate_id}/pause", dependencies=OPERATOR_WRITE)
async def pause_mandate(mandate_id: uuid.UUID, req: TotpIn, request: Request, auth: dict = Depends(require_operator)):
    """Risk reducing: no TOTP. The mandate takes no new risk; exits and stops keep running."""
    return await _transition(request, auth, mandate_id, AutonomyTrigger.PAUSE, reason=req.reason)


@router.post("/mandates/{mandate_id}/resume", dependencies=OPERATOR_WRITE)
async def resume_mandate(mandate_id: uuid.UUID, req: TotpIn, request: Request, auth: dict = Depends(require_operator)):
    """Resuming a halted or paused mandate needs TOTP; its high-water mark restarts from here."""
    await require_totp(request, auth, req.totp_code)
    return await _transition(request, auth, mandate_id, AutonomyTrigger.RESUME, totp_ok=True, reason=req.reason)


@router.post("/mandates/{mandate_id}/demote", dependencies=OPERATOR_WRITE)
async def demote_mandate(mandate_id: uuid.UUID, req: TotpIn, request: Request, auth: dict = Depends(require_operator)):
    return await _transition(request, auth, mandate_id, AutonomyTrigger.OPERATOR_DEMOTE, reason=req.reason)


def approval_view(a: dict[str, Any], now: datetime.datetime, mandate_name: str | None = None) -> dict[str, Any]:
    return {
        "id": str(a["id"]), "mandate_id": str(a["mandate_id"]), "mandate": mandate_name, "action_class": a["action_class"],
        "command_type": a["command_type"], "summary": a["summary"], "source": a["source"],
        "level": a["level_at_creation"], "timeout_policy": a["timeout_policy"], "status": a["status"],
        "expires_at": a["expires_at"].isoformat(), "seconds_left": max(0, int((a["expires_at"] - now).total_seconds())),
        "decided_via": a["decided_via"], "decided_by": a["decided_by"],
        "decided_at": a["decided_at"].isoformat() if a["decided_at"] else None,
        "command_id": str(a["command_id"]) if a["command_id"] else None,
        "trace_id": str(a["trace_id"]) if a.get("trace_id") else None, "payload": a["payload"],
        "created_at": a["created_at"].isoformat(),
    }


@router.get("/approvals")
async def list_approvals(request: Request, status: Literal["pending", "recent"] = "pending",
                         auth: dict = Depends(require_operator)):
    now = datetime.datetime.now(datetime.UTC)
    where = "a.status = 'PENDING'" if status == "pending" else "a.created_at > now() - interval '7 days'"
    cols = ", ".join(f"a.{c.strip()}" for c in APPROVAL_COLUMNS.split(","))
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {cols}, m.name AS mandate_name FROM ai_approvals a JOIN ai_mandates m ON m.id = a.mandate_id "
            f"WHERE {where} ORDER BY a.expires_at ASC LIMIT 200")
    return {"now": now.isoformat(), "approvals": [approval_view(as_dict(r), now, r["mandate_name"]) for r in rows]}


async def _decide(request: Request, approval_id: uuid.UUID, approve: bool, via: str, by: str) -> dict[str, Any]:
    async with request.app.state.pool.acquire() as conn:
        try:
            d = await decide_approval(conn, approval_id, approve, via=via, decided_by=by)
        except LookupError:
            raise HTTPException(status_code=404, detail="Approval not found")
        except ApprovalConflict as e:
            raise HTTPException(status_code=409, detail=f"The approval is {e.status}")
    return {"approval_id": str(d.approval_id), "status": d.status, "command_id": str(d.command_id) if d.command_id else None,
            "replayed": d.replayed}


@router.post("/approvals/{approval_id}/decision", dependencies=OPERATOR_WRITE)
async def decide_from_web(approval_id: uuid.UUID, req: DecisionIn, request: Request, auth: dict = Depends(require_operator)):
    """The logged-in operator (password + TOTP at login, CSRF here) approves or vetoes."""
    return await _decide(request, approval_id, req.decision == "APPROVE", "web", actor(auth))


@router.post("/notifier/approvals/{approval_id}")
async def decide_from_telegram(approval_id: uuid.UUID, request: Request):
    """Telegram decisions relayed by the notifier: HMAC-signed with a single-use nonce, from an allowed
    chat; approving ADD_RISK needs the operator's TOTP (D8). Veto and REDUCE_RISK approval are one tap."""
    secret = os.getenv("NOTIFIER_HMAC_SECRET", "")
    body = await request.body()
    nonce = notifier_contract.verify_request(secret, "POST", request.url.path, body, request.headers)
    if nonce is None:
        raise HTTPException(status_code=401, detail="Invalid notifier signature")
    r = _redis(request)
    if r is None or not await r.set(f"notifier:nonce:{nonce}", "1", nx=True, ex=2 * notifier_contract.MAX_SKEW_S):
        raise HTTPException(status_code=401, detail="Replayed notifier request")
    try:
        req = NotifierDecisionIn.model_validate_json(body)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid decision")
    allowed = {c.strip() for c in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
    if req.chat_id not in allowed:
        raise HTTPException(status_code=403, detail="Chat not allowed")
    approve = req.decision == "APPROVE"
    if approve:
        async with request.app.state.pool.acquire() as conn:
            action = await conn.fetchval("SELECT action_class FROM ai_approvals WHERE id = $1", approval_id)
        if action is None:
            raise HTTPException(status_code=404, detail="Approval not found")
        if action == ActionClass.ADD_RISK:
            operator = os.getenv("NOTIFIER_OPERATOR_USERNAME", "")
            if not operator or not await verify_totp(request, req.totp_code, username=operator):
                raise HTTPException(status_code=401, detail="TOTP_REQUIRED")
    return await _decide(request, approval_id, approve, "telegram", f"telegram:{req.chat_id}")


@router.get("/status")
async def ai_status(request: Request, auth: dict = Depends(require_operator)):
    """What Mission Control's banners show: kill switch, halted mandates, stop protection, notifier, spend."""
    r = _redis(request)
    exit_up = notifier_up = False
    budget_alert = False
    if r is not None:
        try:
            exit_up = bool(await r.get(EXIT_MONITOR_HEARTBEAT))
            notifier_up = bool(await r.get(NOTIFIER_HEARTBEAT))
            budget_alert = bool(await r.get(f"llm_budget_alert:{datetime.datetime.now(datetime.UTC).date().isoformat()}"))
        except Exception:
            pass
    async with request.app.state.pool.acquire() as conn:
        ks = await conn.fetchrow(
            "SELECT is_latched, reason, latched_by, latched_at FROM execution_kill_switch WHERE environment = 'PAPER' AND account_id = 'PAPER-ACCOUNT-001'")
        halted = await conn.fetch("SELECT id, name, halted_reason, halted_at FROM ai_mandates WHERE status = 'HALTED' ORDER BY halted_at DESC")
        backstop = await conn.fetchrow(
            "SELECT equity, hwm, drawdown_pct, ts FROM ai_mandate_equity WHERE mandate_id IS NULL ORDER BY ts DESC, id DESC LIMIT 1")
    degraded: list[str] = []
    if ks and ks["is_latched"]:
        degraded.append(f"PAPER kill switch latched: {ks['reason']}")
    if not exit_up:
        degraded.append("Exit monitor down: AI positions have no deterministic stops and new AI orders are refused")
    if not notifier_up:
        degraded.append("Notifier down: no Telegram approvals or Critical alerts")
    if budget_alert:
        degraded.append("LLM spend passed 70% of the daily cap: optional AI work degrades first")
    return {
        "kill_switch": {"latched": bool(ks and ks["is_latched"]), "reason": ks["reason"] if ks else None,
                        "latched_by": ks["latched_by"] if ks else None},
        "halted_mandates": [{"id": str(h["id"]), "name": h["name"], "reason": h["halted_reason"],
                             "halted_at": h["halted_at"].isoformat() if h["halted_at"] else None} for h in halted],
        "account": {"equity": _dec(backstop["equity"]), "hwm": _dec(backstop["hwm"]), "drawdown_pct": _dec(backstop["drawdown_pct"]),
                    "backstop_pct": "20"} if backstop else None,
        "exit_monitor_up": exit_up, "notifier_up": notifier_up, "budget_alert": budget_alert, "degraded": degraded,
    }

