"""Read views and the Stop all action for Auto-Trade profiles.

Profile creation, editing and ordinary lifecycle changes stay on the existing mandate routes. This
module adds only the P2 read models and the explicitly approved multi-profile stop operation.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import logging
import os
import re
import uuid
from decimal import Decimal
from importlib.resources import files
from typing import Any

from cryptonite_api.routes.auth import OPERATOR_WRITE, require_human_operator, require_operator
from cryptonite_execution import profile_lifecycle
from cryptonite_schemas.mandate import mandate_instance_id
from cryptonite_schemas.marks import read_live_quote, resolve_quote_price
from cryptonite_schemas.profile import (
    Direction,
    Market,
    Style,
    market_of,
    preset_ids_for,
    underlying_spot,
)
from cryptonite_schemas.universe import INGEST_UNIVERSE_KEY, IngestUniverse
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

logger = logging.getLogger("cryptonite.api.profiles")

router = APIRouter(prefix="/api/v1/ai/profiles", tags=["ai-profiles"])
UTC = dt.UTC
ZERO = Decimal("0")
_COIN_RE = re.compile(r"^[A-Z0-9]{2,24}$")
_CURSOR_SEP = "."
_CURSOR_MAX_LENGTH = 256
_MAX_DECISION_ID = 2**63 - 1
_DECISION_COLUMNS = "id, mandate_id, ts, instrument_id, stage, outcome, reason, detail, run_id, command_id"


def _decimal(value: Any) -> Decimal:
    """Convert database numerics without passing through binary floating point."""
    return Decimal(str(value if value is not None else 0))


def _decimal_text(value: Decimal | Any | None) -> str | None:
    """Serialize exact numeric values as plain decimal strings for JSON clients."""
    if value is None:
        return None
    number = _decimal(value).normalize()
    return format(number, "f")


def _json_object(value: Any) -> dict[str, Any]:
    """Decode asyncpg JSON/JSONB values while tolerating an empty detail payload."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _decision_view(row: Any) -> dict[str, Any]:
    """Return a JSON-safe decision row with the original structured detail."""
    item = dict(row)
    item["ts"] = _iso(item.get("ts"))
    for field in ("mandate_id", "run_id", "command_id"):
        if item.get(field) is not None:
            item[field] = str(item[field])
    item["detail"] = _json_object(item.get("detail"))
    return item


async def _profile(conn: Any, profile_id: uuid.UUID) -> Any:
    """Fetch one profile or return the same not-found response from every profile read route."""
    row = await conn.fetchrow("SELECT * FROM ai_mandates WHERE id = $1", profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return row


def _encode_cursor(ts: dt.datetime, row_id: int) -> str:
    raw = f"{ts.astimezone(UTC).isoformat()}{_CURSOR_SEP}{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[dt.datetime, int]:
    """Decode an opaque stable decision cursor; malformed cursors become an ordinary 422."""
    if not cursor or len(cursor) > _CURSOR_MAX_LENGTH:
        raise HTTPException(status_code=422, detail="Invalid decision cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True).decode("utf-8")
        timestamp, row_id = decoded.rsplit(_CURSOR_SEP, 1)
        parsed = dt.datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed_id = int(row_id)
        if parsed_id <= 0 or parsed_id > _MAX_DECISION_ID:
            raise ValueError("decision id is outside the database range")
        return parsed.astimezone(UTC), parsed_id
    except (ValueError, UnicodeDecodeError, TypeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Invalid decision cursor") from exc


class StopAllIn(BaseModel):
    close: bool = False


@router.post("/stop-all", dependencies=OPERATOR_WRITE)
async def stop_all(request: Request, req: StopAllIn | None = None,
                   auth: dict[str, Any] = Depends(require_human_operator)) -> dict[str, Any]:
    """Stop each currently ACTIVE or PAUSED profile independently, preserving per-profile failures.

    By default (``close=False``) no new entries are accepted and any open position continues on its own
    exits; ``close=True`` also queues reduce-only closes, exactly as the single-profile stop. Each selected
    profile has its own transaction so one refusal cannot roll back a different profile's stop.
    """
    close = bool(req and req.close)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        candidates = await conn.fetch(
            "SELECT id FROM ai_mandates WHERE status IN ('ACTIVE', 'PAUSED') ORDER BY created_at, id"
        )

    results: list[dict[str, Any]] = []
    for candidate in candidates:
        profile_id = candidate["id"]
        result: dict[str, Any] = {"id": str(profile_id)}
        async with pool.acquire() as conn:
            try:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT status FROM ai_mandates WHERE id = $1 FOR UPDATE", profile_id
                    )
                    if row is None:
                        result.update(status="NOT_FOUND", error="PROFILE_NOT_FOUND")
                    elif row["status"] not in ("ACTIVE", "PAUSED"):
                        result.update(status=row["status"], outcome="SKIPPED_STATUS_CHANGED")
                    else:
                        stopped = await profile_lifecycle.stop(
                            conn, profile_id, actor=str(auth.get("username") or auth["user_id"]), close=close
                        )
                        result.update(status=stopped.status.value, outcome="STOP_REQUESTED")
            except profile_lifecycle.LifecycleError as exc:
                result.update(status="UNCHANGED", outcome="REFUSED", error=exc.code, detail=exc.detail)
            except Exception:
                logger.exception("Stop all failed for profile %s", profile_id)
                result.update(status="UNCHANGED", outcome="ERROR", error="PROFILE_STOP_FAILED")
        results.append(result)

    requested = sum(result.get("outcome") == "STOP_REQUESTED" for result in results)
    refused = sum(result.get("outcome") in {"REFUSED", "ERROR"} for result in results)
    return {
        "close": close,
        "selected_count": len(candidates),
        "requested_count": requested,
        "refused_count": refused,
        "profiles": results,
    }


@router.get("/{profile_id}/decisions")
async def profile_decisions(
    profile_id: uuid.UUID,
    request: Request,
    cursor: str | None = Query(default=None, max_length=_CURSOR_MAX_LENGTH),
    limit: int = Query(default=50, ge=1, le=100),
    auth: dict[str, Any] = Depends(require_operator),
) -> dict[str, Any]:
    """Return newest-first decision history with a stable (timestamp, id) cursor."""
    async with request.app.state.pool.acquire() as conn:
        await _profile(conn, profile_id)
        if cursor:
            cursor_ts, cursor_id = _decode_cursor(cursor)
            rows = await conn.fetch(
                f"""SELECT {_DECISION_COLUMNS} FROM ai_profile_decisions
                    WHERE mandate_id = $1 AND (ts, id) < ($2, $3)
                    ORDER BY ts DESC, id DESC LIMIT $4""",
                profile_id, cursor_ts, cursor_id, limit + 1,
            )
        else:
            rows = await conn.fetch(
                f"""SELECT {_DECISION_COLUMNS} FROM ai_profile_decisions
                    WHERE mandate_id = $1 ORDER BY ts DESC, id DESC LIMIT $2""",
                profile_id, limit + 1,
            )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1]["ts"], page[-1]["id"]) if has_more and page else None
    return {"profile_id": str(profile_id), "decisions": [_decision_view(row) for row in page],
            "next_cursor": next_cursor, "has_more": has_more}


@router.get("/{profile_id}/summary")
async def profile_summary(
    profile_id: uuid.UUID,
    request: Request,
    auth: dict[str, Any] = Depends(require_operator),
) -> dict[str, Any]:
    """Return the compact profile card, current daily P&L and explicit health-score components."""
    now = dt.datetime.now(UTC)
    day_start = dt.datetime.combine(now.date(), dt.time.min, UTC)
    async with request.app.state.pool.acquire() as conn:
        profile = await _profile(conn, profile_id)
        latest = await conn.fetchrow(
            f"""SELECT {_DECISION_COLUMNS} FROM ai_profile_decisions
                WHERE mandate_id = $1 ORDER BY ts DESC, id DESC LIMIT 1""",
            profile_id,
        )
        closed = await conn.fetchrow(
            """SELECT COALESCE(sum(COALESCE(NULLIF(detail->>'realized_pnl', '')::numeric, 0)), 0) AS realized_pnl
               FROM ai_profile_decisions WHERE mandate_id = $1 AND stage = 'EXIT' AND ts >= $2 AND ts < $3""",
            profile_id, day_start, now,
        )
        open_positions = await conn.fetchrow(
            """SELECT count(*) AS count, COALESCE(sum(COALESCE(unrealized_pnl, 0)), 0) AS unrealized
               FROM paper_positions WHERE instance_id = $1 AND status = 'OPEN' AND quantity > 0""",
            mandate_instance_id(profile_id),
        )
        session = await conn.fetchrow(
            "SELECT last_pnl FROM ai_mandate_daily_sessions WHERE mandate_id = $1 AND session_date = $2",
            profile_id, now.date(),
        )
        failed_runs = int(await conn.fetchval(
            """SELECT count(DISTINCT r.id) FROM debate_runs r
               JOIN ai_profile_decisions d ON d.run_id = r.id AND d.mandate_id = $1
                    AND d.stage = 'GATE' AND d.outcome = 'ACCEPTED'
               WHERE r.status = 'FAILED' AND COALESCE(r.completed_at, r.created_at) >= $2""",
            profile_id, now - dt.timedelta(hours=24),
        ) or 0)
        switch = (await profile_lifecycle.load_settings(conn)).enabled
    quotes = await _quote_freshness(_redis(request), list(profile["instruments"] or []))

    realized = _decimal(closed["realized_pnl"] if closed else 0)
    unrealized = _decimal(open_positions["unrealized"] if open_positions else 0)
    calculated_pnl = realized + unrealized
    budget = _decimal(profile["capital_budget_usd"])
    loss_limit_pct = _decimal(profile["daily_loss_limit_pct"]) if profile["daily_loss_limit_pct"] is not None else None
    target_pct = _decimal(profile["daily_profit_target_pct"]) if profile["daily_profit_target_pct"] is not None else None
    loss_limit = budget * loss_limit_pct / Decimal("100") if loss_limit_pct is not None else None
    target = budget * target_pct / Decimal("100") if target_pct is not None else None
    latest_at = latest["ts"] if latest else None
    age_s = max(0.0, (now - latest_at).total_seconds()) if latest_at else None
    fresh_count = sum(1 for q in quotes if q["fresh"])
    freshness_points = 40 if quotes and fresh_count == len(quotes) else 20 if fresh_count else 0
    recent_error_points = max(0, 30 - 10 * failed_runs)
    breaker_reason = profile["halted_reason"] or (
        profile["paused_reason"] if (profile["paused_reason"] or "").startswith("BREAKER_") else None
    )
    breaker_active = bool(breaker_reason)
    breaker_points = 0 if breaker_active else 30
    running = profile["status"] not in ("STOPPED", "ARCHIVED")
    health_score = max(0, min(100, freshness_points + recent_error_points + breaker_points)) if running else None
    # The monitor's daily-session value is the canonical equity-change P&L (including costs); the component
    # values remain available for transparency. Before the monitor's first sample, use the explicit components.
    pnl = _decimal(session["last_pnl"]) if session and session["last_pnl"] is not None else calculated_pnl
    autonomy_level = int(profile["autonomy_level"])
    mode_label = {1: "Ask me", 2: "Veto window", 3: "Fully automatic"}.get(autonomy_level, f"L{autonomy_level}")

    return {
        "profile_id": str(profile_id),
        "status": profile["status"],
        "today": {
            "date_utc": now.date().isoformat(),
            "realized_pnl": _decimal_text(realized),
            "unrealized_pnl": _decimal_text(unrealized),
            "pnl": _decimal_text(pnl),
            "session_pnl": _decimal_text(session["last_pnl"] if session else None) or "0",
            "daily_loss_limit_usd": _decimal_text(loss_limit),
            "daily_profit_target_usd": _decimal_text(target),
        },
        "open_positions_count": int(open_positions["count"] if open_positions else 0),
        "last_decision": _decision_view(latest) if latest else None,
        "breaker": {"active": breaker_active, "state": "ACTIVE" if breaker_active else "CLEAR",
                    "reason": breaker_reason},
        "dry_run": bool(profile["dry_run"]),
        "mode": {"level": autonomy_level, "label": mode_label},
        "master_switch": "ON" if switch else "OFF",
        "last_decision_at": _iso(latest_at),
        "last_decision_age_seconds": round(age_s, 1) if age_s is not None else None,
        "health_score": {
            "score": health_score,
            "running": running,
            "components": {
                "freshness": {"points": freshness_points, "fresh_instruments": fresh_count,
                              "instruments": quotes,
                              "rule": "40 when every instrument's live quote is fresh, 20 when some are, 0 when none"},
                "recent_failures": {"points": recent_error_points, "failed_debates_24h": failed_runs,
                                    "deduction_per_failed_debate": 10, "base_points": 30, "floor": 0,
                                    "source": "debate_runs.status=FAILED joined through the profile's GATE/ACCEPTED decision run_id"},
                "breaker": {"points": breaker_points, "active": breaker_active, "reason": breaker_reason,
                            "clear_points": 30, "active_points": 0},
            },
        },
    }


def _redis(request: Request) -> Any:
    return getattr(getattr(request.app.state, "stream_hub", None), "redis", None)


async def _quote_freshness(redis: Any, instruments: list[str]) -> list[dict[str, Any]]:
    """Live-quote freshness of each profile instrument; a spot-margin instrument is priced from its spot quote."""
    out = []
    for instrument in instruments:
        try:
            source = underlying_spot(instrument) if market_of(instrument) is Market.SPOT_MARGIN else instrument
        except ValueError:
            source = instrument
        price = resolve_quote_price(source, await read_live_quote(redis, source))
        out.append({"instrument_id": instrument, "fresh": price.price is not None,
                    "age_seconds": round(price.age_s, 1) if price.age_s is not None else None})
    return out


def _range_start(range_name: str, now: dt.datetime) -> dt.datetime | None:
    return {"7d": now - dt.timedelta(days=7), "30d": now - dt.timedelta(days=30), "all": None}[range_name]


@router.get("/{profile_id}/performance")
async def profile_performance(
    profile_id: uuid.UUID,
    request: Request,
    range: str = Query(default="30d", pattern="^(7d|30d|all)$"),
    auth: dict[str, Any] = Depends(require_operator),
) -> dict[str, Any]:
    """Return profile equity, closed-fill results, costs, funding, margin interest and drawdown."""
    now = dt.datetime.now(UTC)
    since = _range_start(range, now)
    instance_id = mandate_instance_id(profile_id)
    since_filter = since or dt.datetime(1970, 1, 1, tzinfo=UTC)
    async with request.app.state.pool.acquire() as conn:
        await _profile(conn, profile_id)
        equity_rows = await conn.fetch(
            """WITH filtered AS MATERIALIZED (
                   SELECT id, ts, equity, hwm, drawdown_pct,
                          count(*) OVER () AS total_rows,
                          ntile(4999) OVER (ORDER BY ts, id) AS bucket
                   FROM ai_mandate_equity
                   WHERE mandate_id = $1 AND ($2::timestamptz IS NULL OR ts >= $2)
               ), sampled AS (
                   SELECT DISTINCT ON (bucket) id, ts, equity, hwm, drawdown_pct, total_rows
                   FROM filtered ORDER BY bucket, ts DESC, id DESC
               ), first_point AS (
                   SELECT id, ts, equity, hwm, drawdown_pct, total_rows
                   FROM filtered ORDER BY ts, id LIMIT 1
               ), selected AS (
                   SELECT * FROM sampled
                   UNION
                   SELECT * FROM first_point
               )
               SELECT ts, equity, hwm, drawdown_pct, total_rows
               FROM selected ORDER BY ts, id""",
            profile_id, since,
        )
        max_drawdown = await conn.fetchval(
            """SELECT COALESCE(max(drawdown_pct), 0) FROM ai_mandate_equity
               WHERE mandate_id = $1 AND ($2::timestamptz IS NULL OR ts >= $2)""",
            profile_id, since,
        )
        fills = await conn.fetch(
            """SELECT trade_id, client_order_id, instrument_id, side, price, quantity,
                      commission, commission_currency, executed_at
               FROM paper_trades
               WHERE instance_id = $1 AND ($2::timestamptz IS NULL OR executed_at >= $2)
               ORDER BY executed_at, trade_id""",
            instance_id, since,
        )
        # Keep profile exit outcomes separate from fills. Some real closes use a generated child order ID that is
        # not persisted on the parent execution command, so joining by that ID would silently drop the fill or
        # attach an outcome to the wrong fill. The audit table is the authoritative source for R and realized P&L.
        exit_decisions = await conn.fetch(
            """SELECT id, ts, instrument_id, outcome, detail, command_id
               FROM ai_profile_decisions
               WHERE mandate_id = $1 AND stage = 'EXIT'
                 AND ($2::timestamptz IS NULL OR ts >= $2)
               ORDER BY ts, id""",
            profile_id, since,
        )
        fee_rows = await conn.fetch(
            """SELECT COALESCE(commission_currency, 'UNKNOWN') AS currency, COALESCE(sum(commission), 0) AS amount
               FROM paper_trades WHERE instance_id = $1 AND ($2::timestamptz IS NULL OR executed_at >= $2)
               GROUP BY COALESCE(commission_currency, 'UNKNOWN') ORDER BY currency""",
            instance_id, since,
        )
        funding = await conn.fetchval(
            """SELECT COALESCE(sum((payload->>'cash_flow')::numeric), 0)
               FROM paper_execution_events
               WHERE event_type = 'FUNDING_SETTLED' AND payload->>'instance_id' = $1
                 AND occurred_at_ns >= (extract(epoch FROM $2::timestamptz) * 1000000000)::bigint
                 AND occurred_at_ns < (extract(epoch FROM $3::timestamptz) * 1000000000)::bigint""",
            instance_id, since_filter, now,
        )
        margin_interest_rows = await conn.fetch(
            """SELECT i.asset, COALESCE(sum(i.amount), 0) AS amount
                   FROM paper_margin_interest i
                   JOIN paper_margin_accounts a ON a.id = i.margin_account_id
                   WHERE a.account_id = 'PAPER-ACCOUNT-001'
                     AND i.period_end > $2 AND i.period_end <= $3
                     AND EXISTS (
                         SELECT 1 FROM paper_positions p
                         WHERE p.instance_id = $1 AND p.account_id = a.account_id
                           AND split_part(p.instrument_id, '.', 1) = a.pair || '-MARGIN'
                           AND p.opened_at < i.period_end
                           AND (p.closed_at IS NULL OR p.closed_at > i.period_start)
                     )
                   GROUP BY i.asset ORDER BY i.asset""",
            instance_id, since_filter, now,
        )

    exit_rows: list[dict[str, Any]] = []
    realized_values: list[Decimal] = []
    r_values: list[Decimal] = []
    r_unknown = 0
    wins = 0
    for row in exit_decisions:
        detail = _json_object(row["detail"])
        realized_value = _decimal(detail["realized_pnl"]) if detail.get("realized_pnl") is not None else None
        r_value = _decimal(detail["r_multiple"]) if detail.get("r_multiple") is not None else None
        if realized_value is not None:
            realized_values.append(realized_value)
            wins += realized_value > 0
        if r_value is None:
            r_unknown += 1
        else:
            r_values.append(r_value)
        exit_rows.append({
            "decision_id": int(row["id"]), "instrument_id": row["instrument_id"],
            "price": _decimal_text(detail.get("price")), "quantity": _decimal_text(detail.get("qty")),
            "realized_pnl": _decimal_text(realized_value), "r_multiple": _decimal_text(r_value),
            "timestamp": _iso(row["ts"]), "command_id": str(row["command_id"]) if row["command_id"] else None,
            "outcome": row["outcome"],
        })

    curve = [{"ts": _iso(row["ts"]), "equity": _decimal_text(row["equity"]),
              "hwm": _decimal_text(row["hwm"]), "drawdown_pct": _decimal_text(row["drawdown_pct"])}
             for row in equity_rows]
    curve_sampled = bool(equity_rows and int(equity_rows[0]["total_rows"]) > len(equity_rows))
    fee_by_currency = {row["currency"]: _decimal_text(row["amount"]) for row in fee_rows}
    total_fee_amount = sum((_decimal(row["amount"]) for row in fee_rows), ZERO)
    fills_view = [{
        "trade_id": row["trade_id"], "client_order_id": row["client_order_id"],
        "instrument_id": row["instrument_id"], "side": row["side"],
        "price": _decimal_text(row["price"]), "quantity": _decimal_text(row["quantity"]),
        "fee": _decimal_text(row["commission"]), "fee_currency": row["commission_currency"] or "UNKNOWN",
        "executed_at": _iso(row["executed_at"]),
    } for row in fills]
    margin_interest_by_asset = {row["asset"]: _decimal_text(row["amount"]) for row in margin_interest_rows}
    valid_pnl_count = len(realized_values)
    return {
        "profile_id": str(profile_id), "range": range, "equity_curve": curve,
        "equity_curve_sampled": curve_sampled, "fills": fills_view, "exit_outcomes": exit_rows,
        "stats": {
            "audited_exit_count": len(exit_rows), "audited_wins": wins,
            "audited_win_rate": _decimal_text(Decimal(wins) / valid_pnl_count) if valid_pnl_count else None,
            "avg_r": _decimal_text(sum(r_values, ZERO) / len(r_values)) if r_values else None,
            "r_known": len(r_values), "r_unknown": r_unknown,
            "outcome_coverage": {
                "scope": "EXIT audit decisions only",
                "complete": False,
                "profile_fill_count": len(fills_view),
                "audited_exit_count": len(exit_rows),
                "note": (
                    "All profile-owned fills are listed separately. Some close paths do not persist a durable "
                    "one-to-one link between a fill and its EXIT audit row, so outcome metrics cover only audited exits."
                ),
            },
            "fees": {"total_amount": _decimal_text(total_fee_amount), "by_currency": fee_by_currency},
            "funding": _decimal_text(funding), "margin_interest_by_asset": margin_interest_by_asset,
            "max_drawdown_pct": _decimal_text(max_drawdown),
        },
    }


def _preset_document() -> dict[str, Any]:
    """Load the checked-in replay artifact as package data and compute its quality labels server-side."""
    path = files("cryptonite_scanner").joinpath("data/preset_replay.json")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.exception("Preset replay data could not be loaded")
        raise HTTPException(status_code=503, detail="Preset replay statistics are unavailable") from exc
    rows = []
    for source in document.get("results", []):
        row = dict(source)
        trades = int(row.get("trades") or 0)
        avg_r = _decimal(row.get("avg_r"))
        row["quality"] = "thin" if trades < 20 else "negative" if avg_r < 0 else "ok"
        rows.append(row)
    return {**document, "results": rows}


@router.get("/presets/stats")
async def preset_stats(auth: dict[str, Any] = Depends(require_operator)) -> dict[str, Any]:
    """Serve the immutable scanner replay and assign the three UI quality labels on the server.

    ``preset_ids`` is ``preset_ids_for(style, direction)`` for every pair, so the wizard never copies the mapping."""
    preset_ids = {f"{style.value}:{direction.value}": list(preset_ids_for(style, direction))
                  for style in Style for direction in Direction}
    return {**_preset_document(), "preset_ids": preset_ids}


def _requested_coins(coins: str) -> list[str]:
    """Parse a comma-separated list of normalized spot symbols without accepting path/query fragments."""
    parsed: list[str] = []
    for raw in coins.split(","):
        coin = raw.strip().upper()
        if not coin or not _COIN_RE.fullmatch(coin):
            raise HTTPException(status_code=422, detail=f"Invalid coin symbol: {raw!r}")
        if coin not in parsed:
            parsed.append(coin)
    if not parsed:
        raise HTTPException(status_code=422, detail="At least one coin is required")
    return parsed


@router.get("/estimate")
async def estimate(
    request: Request,
    coins: str = Query(..., min_length=2, max_length=500),
    style: str = Query(...),
    direction: str = Query(...),
    auth: dict[str, Any] = Depends(require_operator),
) -> dict[str, Any]:
    """Estimate feed availability and daily debate/cost from the prior seven days of candidate runs."""
    requested = _requested_coins(coins)
    style = style.strip().upper()
    direction = direction.strip().upper()
    if style not in {"SWING", "INTRADAY"}:
        raise HTTPException(status_code=422, detail="style must be SWING or INTRADAY")
    if direction not in {"LONG", "SHORT", "BOTH"}:
        raise HTTPException(status_code=422, detail="direction must be LONG, SHORT or BOTH")

    redis = getattr(getattr(request.app.state, "stream_hub", None), "redis", None)
    raw_universe = await redis.get(INGEST_UNIVERSE_KEY) if redis is not None else None
    universe = IngestUniverse.parse(raw_universe)
    if universe is None:
        raise HTTPException(status_code=503, detail="INGEST_UNIVERSE_UNKNOWN: feed status is unavailable")
    fed = sorted({*universe.symbols, *universe.perp_symbols})
    fed_set = set(fed)
    not_in_feed = [coin for coin in requested if coin not in fed_set]
    try:
        max_symbols = int(os.getenv("INGEST_MAX_SYMBOLS", "12"))
        if max_symbols < 1:
            raise ValueError
    except ValueError:
        max_symbols = 12

    now = dt.datetime.now(UTC)
    since = now - dt.timedelta(days=7)
    async with request.app.state.pool.acquire() as conn:
        metrics = await conn.fetchrow(
            """WITH RECURSIVE runs AS (
                   SELECT id, candidate_id, decision_trace_id,
                          regexp_replace(split_part(instrument_id, '.', 1), '-(PERP|MARGIN)$', '') AS coin
                   FROM debate_runs WHERE created_at >= $1 AND created_at < $2
               ), trace_tree(run_id, trace_id) AS (
                   SELECT id, decision_trace_id FROM runs WHERE decision_trace_id IS NOT NULL
                   UNION
                   SELECT tree.run_id, child.id FROM trace_tree tree
                   JOIN ai_decision_traces child ON child.parent_id = tree.trace_id
               ), run_cost AS (
                   SELECT runs.id, runs.candidate_id, runs.coin,
                          COALESCE(sum(ledger.total_cost_usd), 0) AS cost
                   FROM runs
                   LEFT JOIN trace_tree tree ON tree.run_id = runs.id
                   LEFT JOIN llm_cost_ledger ledger ON ledger.trace_id = tree.trace_id
                       AND ledger.created_at >= $1 AND ledger.created_at < $2
                   GROUP BY runs.id, runs.candidate_id, runs.coin
               )
               SELECT count(*) FILTER (WHERE candidate_id IS NOT NULL) AS candidate_runs,
          count(DISTINCT candidate_id) FILTER (WHERE candidate_id IS NOT NULL) AS candidates,
          count(DISTINCT coin) FILTER (WHERE candidate_id IS NOT NULL) AS candidate_coins,
                      COALESCE(sum(cost) FILTER (WHERE candidate_id IS NOT NULL), 0) AS candidate_cost,
          count(*) AS all_runs, count(DISTINCT coin) AS all_coins,
          COALESCE(sum(cost), 0) AS all_cost
               FROM run_cost""",
            since, now,
        )
        switch = (await profile_lifecycle.load_settings(conn)).enabled

    candidate_runs = int(metrics["candidate_runs"] or 0)
    candidates = int(metrics["candidates"] or 0)
    candidate_coins = int(metrics["candidate_coins"] or 0)
    all_runs = int(metrics["all_runs"] or 0)
    all_coins = int(metrics["all_coins"] or 0)
    use_candidates = candidate_coins > 0 and candidate_runs > 0
    base_runs = candidate_runs if use_candidates else all_runs
    base_coin_count = candidate_coins if use_candidates else all_coins
    base_cost = _decimal(metrics["candidate_cost"] if use_candidates else metrics["all_cost"])
    debates_per_coin_day = (Decimal(base_runs) / Decimal(base_coin_count) / Decimal("7")) if base_coin_count else ZERO
    mean_cost_per_debate = base_cost / Decimal(base_runs) if base_runs else ZERO
    estimated_debates = debates_per_coin_day * len(requested)
    estimated_cost = estimated_debates * mean_cost_per_debate
    method = (
        "Uses the last 7 UTC days. Mean candidate-linked debate_runs per observed coin per day is candidate runs "
        "divided by distinct normalized instrument symbols with candidate runs and 7; estimated debates/day multiplies "
        "that rate by requested coins. "
        "Mean LLM cost per debate sums llm_cost_ledger rows under the recursive ai_decision_traces parent tree rooted "
        "at each debate_runs.decision_trace_id, divided by candidate-linked runs. LLM $/day is estimated debates/day "
        "times that mean. If candidate activity is unavailable, all runs and their trace costs are divided by distinct "
        "observed symbols. Historical data is global "
        "and is not filtered by requested style/direction."
    )
    return {
        "coins": requested,
        "master_switch": "ON" if switch else "OFF",
        "style": style,
        "direction": direction,
        "feed": {"ingest_max_symbols": max_symbols, "coins_in_feed": fed,
                 "not_in_feed": [{"coin": coin, "code": "NOT_IN_FEED",
                                  "onboarding_action": {"method": "POST", "path": "/api/v1/instruments/onboard",
                                                        "body": {"symbol": coin}}}
                                 for coin in not_in_feed]},
        "estimate": {"debates_per_day": _decimal_text(estimated_debates),
                     "llm_usd_per_day": _decimal_text(estimated_cost),
                     "historical_candidate_count": candidates,
                     "historical_candidate_runs_7d": candidate_runs,
                     "mean_llm_usd_per_candidate_run": _decimal_text(mean_cost_per_debate),
                     "method": method},
    }
