"""H15 PR B: paper auto-coverage (ADR 0011, D12 amendment).

A worker that widens opted-in paper profiles so their scanners look at shorts and at fed coins nobody covers. The operator
enables a policy on the host (``python -m cryptonite_orchestrator.paper_coverage``); there is no API route. It changes
direction, strategies, timeframes, markets and instruments only: never a mode, switch, dry run, status, budget, risk,
drawdown, leverage, position or debate limit, breaker or exit. It starts nothing and sends no order.

A cycle plans with no locks held, then applies each change in one short transaction that aborts if the policy version
or the profile's ``updated_at`` moved, so a concurrent operator edit always wins. An item is applied once per policy
version: the ledger stops the worker adding back what the operator removed.
"""

import argparse
import asyncio
import datetime
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
from cryptonite_execution import profile_lifecycle
from cryptonite_execution.brackets import open_entry_market
from cryptonite_execution.leverage import (
    ACCOUNT_EXPOSURE_CAP,
    ACCOUNT_MAX_LEVERAGE,
    DEFAULT_FEE_ROUND_TRIP,
    DEFAULT_SLIPPAGE_ALLOWANCE,
    LeverageLimits,
    plan_spot_margin_order,
)
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_scanner.setup_grade import settlement_interval_hours
from cryptonite_schemas.mandate import Mandate
from cryptonite_schemas.profile import (
    PRESET_CATALOG,
    PROFILE_TIMEFRAMES,
    STYLE_TIMEFRAMES,
    Market,
    Style,
    TradeSide,
    allows_side,
    base_asset,
    instrument_for,
    mode_of_level,
    preset_ids_for,
    preset_timeframes,
    selection_errors,
    underlying_spot,
)
from cryptonite_schemas.runtime_settings import default_margin_settings, read_margin_settings
from cryptonite_schemas.universe import INGEST_UNIVERSE_KEY, IngestUniverse

logger = logging.getLogger("cryptonite.orchestrator.paper_coverage")

POLICY_KEY = "paper_auto_coverage"
CYCLE_S = 300
FEED_STALE_NS = 5 * 60 * 1_000_000_000
BINANCE_FUTURES_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
DEFAULT_PRESETS = (
    "trend-pullback-short", "bollinger-breakdown-short", "swing-trend-pullback-short-1h",
    "swing-sniper-pullback-short-15m", "swing-squeeze-breakdown-short-1h", "trend-breakdown-short-4h",
    "trend-breakout-4h",
)  # scalp-multi-tf-short is left out: the preset replay found a bug in it
DIRECTION, STRATEGY, TIMEFRAME, MARKET, COIN = "DIRECTION", "STRATEGY", "TIMEFRAME", "MARKET", "COIN"


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    version: int = 0
    actor: str | None = None
    updated_at: str | None = None
    profiles: tuple[str, ...] = ()  # priority order
    allowed_modes: tuple[str, ...] = ("ASK",)
    allowed_presets: tuple[str, ...] = DEFAULT_PRESETS
    allow_new_coins: bool = False
    max_changes_per_cycle: int = 3


def parse_policy(raw: Any) -> Policy:
    """Anything unreadable is a disabled policy: this worker must never run by accident."""
    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(d, dict):
            return Policy()
        return Policy(enabled=d.get("enabled") is True, version=int(d.get("version", 0)), actor=d.get("actor"),
                      updated_at=d.get("updated_at"), profiles=tuple(str(p) for p in d.get("profiles", ())),
                      allowed_modes=tuple(d.get("allowed_modes", ("ASK",))),
                      allowed_presets=tuple(p for p in d.get("allowed_presets", DEFAULT_PRESETS) if p in PRESET_CATALOG),
                      allow_new_coins=d.get("allow_new_coins") is True,
                      max_changes_per_cycle=max(0, int(d.get("max_changes_per_cycle", 3))))
    except (TypeError, ValueError):
        return Policy()


@dataclass(frozen=True)
class Change:
    kind: str
    value: str
    old_value: str | None = None


@dataclass
class Result:
    subject: str
    outcome: str  # APPLIED, PLANNED, NO_CHANGE or a named reason
    changes: list[Change] = field(default_factory=list)


Ledger = set[tuple[str, str]]


def eligibility_reason(m: Mandate | None, policy: Policy) -> str | None:
    if m is None:
        return "PROFILE_NOT_FOUND"
    if m.status.value != "ACTIVE":
        return "PROFILE_NOT_ACTIVE"
    if str(getattr(m.environment, "value", m.environment)) != "PAPER":
        return "PROFILE_NOT_PAPER"
    if mode_of_level(int(m.autonomy_level)).value not in policy.allowed_modes:
        return "MODE_NOT_ALLOWED"
    return None


def plan_scope(m: Mandate, policy: Policy, ledger: Ledger) -> list[Change]:
    """What the policy would add to this profile. Pure. An item in the ledger is never planned again."""
    def fresh(kind: str, value: str) -> bool:
        return (kind, value) not in ledger

    style, direction = str(m.style), str(m.direction)
    markets = {str(x) for x in m.markets}
    changes: list[Change] = []
    wants_short = any(PRESET_CATALOG[p].side is TradeSide.SHORT for p in policy.allowed_presets)
    if Market.PERP.value not in markets and fresh(MARKET, Market.PERP.value):
        changes.append(Change(MARKET, Market.PERP.value))
        markets.add(Market.PERP.value)
    if direction == "LONG" and wants_short and fresh(DIRECTION, "BOTH") and markets & {"PERP", "SPOT_MARGIN"}:
        changes.append(Change(DIRECTION, "BOTH", "LONG"))
        new_direction = "BOTH"
    else:
        new_direction = direction

    picks = list(m.strategies)
    if not picks:  # an empty list means the style defaults: make them explicit before adding to them
        for slug in preset_ids_for(style, direction):
            if fresh(STRATEGY, slug):
                picks.append(slug)
                changes.append(Change(STRATEGY, slug, "style default"))
    added = [p for p in policy.allowed_presets
             if p not in picks and allows_side(new_direction, PRESET_CATALOG[p].side) and fresh(STRATEGY, p)]
    picks += added
    changes += [Change(STRATEGY, p) for p in added]

    timeframes = list(m.timeframes)
    for slug in added:
        if preset_timeframes(style, new_direction, picks, timeframes).get(slug):
            continue
        need = next(tf for tf in PROFILE_TIMEFRAMES if tf in PRESET_CATALOG[slug].timeframes)
        if not timeframes:  # keep what the empty list meant
            for tf in STYLE_TIMEFRAMES[Style(style)]:
                timeframes.append(tf)
                changes.append(Change(TIMEFRAME, tf, "style default"))
        if need not in timeframes and fresh(TIMEFRAME, need):
            timeframes.append(need)
            changes.append(Change(TIMEFRAME, need))
    # A plan that only sets a market or only makes defaults explicit, with nothing to add, changes nothing useful.
    # A short-capable profile always has PERP or SPOT_MARGIN already (chk_ai_mandates_short_market).
    return changes if any(c.kind in (DIRECTION, COIN) or (c.kind != MARKET and c.old_value is None) for c in changes) else []


def _with_item(values: Iterable[Any], item: str) -> list[str]:
    out = [str(v) for v in values]
    return out if item in out else [*out, item]


def fields_after(m: Mandate, changes: list[Change]) -> dict[str, Any]:
    """The profile's scope columns after ``changes``. ``allow_perps`` moves with ``markets`` (a table constraint)."""
    direction, strategies = str(m.direction), list(m.strategies)
    timeframes, markets = list(m.timeframes), sorted(str(x) for x in m.markets)
    instruments = list(m.instruments)
    for c in changes:
        if c.kind == MARKET:
            markets = _with_item(markets, c.value)
        elif c.kind == DIRECTION:
            direction = c.value
        elif c.kind == STRATEGY:
            strategies = _with_item(strategies, c.value)
        elif c.kind == TIMEFRAME:
            timeframes = _with_item(timeframes, c.value)
    new_coins = {c.value for c in changes if c.kind == COIN}
    widened = {underlying_spot(i) for i in instruments} if any(c.kind == MARKET for c in changes) else set()
    for spot in sorted(new_coins | widened):
        for market in markets:
            instruments = _with_item(instruments, instrument_for(spot, Market(market)))
    return {"direction": direction, "strategies": strategies, "timeframes": timeframes, "markets": markets,
            "instruments": instruments, "allow_perps": Market.PERP.value in markets}


@dataclass
class Probes:
    """What the checks read from outside the database, so a test can stand in for it."""
    perp_symbols: Callable[[], Awaitable[set[str] | None]]
    route_refusal: Callable[[Any, Any, Mandate, str], Awaitable[str | None]]


async def fetch_perp_symbols() -> set[str] | None:
    """USD-M perpetuals Binance lists as TRADING. ``instruments`` has no perp rows; None when it cannot be read."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(BINANCE_FUTURES_INFO)
            resp.raise_for_status()
        return {s["symbol"] for s in resp.json().get("symbols", [])
                if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"}
    except Exception as e:
        logger.warning("Could not read the perpetuals' status: %s", e)
        return None


async def _margin_route_refusal(pool, m: Mandate, spot: str, mark: Decimal) -> str | None:
    """The margin dry run a real short would pass, sized as the debate's margin route sizes it."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM system_settings WHERE key LIKE 'margin.%'")
        equity = await conn.fetchval("SELECT total_equity FROM paper_accounts WHERE account_id = $1",
                                     profile_lifecycle.PAPER_ACCOUNT_ID)
    if equity is None or Decimal(str(equity)) <= 0:
        return "ORDER_ROUTE_REFUSED: NON_POSITIVE_EQUITY"
    settings, _ = read_margin_settings({r["key"]: r["value"] for r in rows}, default_margin_settings())
    budget = m.capital_budget_usd
    margin_usd = budget * Decimal(str(m.max_position_pct)) / Decimal("100")
    limits = LeverageLimits(
        risk_budget_usd=budget * m.max_risk_per_trade_pct / Decimal("100"),
        max_notional_usd=min(margin_usd, Decimal(str(equity)) * ACCOUNT_EXPOSURE_CAP),
        max_leverage=min(m.max_leverage, ACCOUNT_MAX_LEVERAGE), slippage_pct=DEFAULT_SLIPPAGE_ALLOWANCE,
        fee_pct=DEFAULT_FEE_ROUND_TRIP, min_notional_usd=Decimal(str(equity)) * Decimal("0.01"))
    plan = plan_spot_margin_order(
        instrument_for(spot, Market.SPOT_MARGIN), "SELL", mark, mark * Decimal("1.02"), margin_usd, limits,
        requested_leverage=m.max_leverage, pair_settings=settings.pair_settings.get(spot.split(".")[0]),
        base_asset=base_asset(spot), budget_usd=budget, shrink_margin_to_fit=True)
    return None if plan.ok else f"ORDER_ROUTE_REFUSED: {plan.reject}"


async def route_refusal(pool, redis, m: Mandate, spot: str) -> str | None:
    """The dry run a real order would pass: a hypothetical short at the profile's real size and leverage ceiling, on the
    market it would really route to."""
    from cryptonite_api.leverage_card import card_leverage_plan, perp_mark

    perp = instrument_for(spot, Market.PERP)
    mark, _ = await perp_mark(redis, perp)
    if mark is None:
        return "NO_PERP_MARK"
    if await open_entry_market(redis, Market.PERP, spot, m.markets) is Market.SPOT_MARGIN:
        return await _margin_route_refusal(pool, m, spot, mark)
    probe = m.model_copy(update={"allow_perps": True, "markets": frozenset(_with_item(m.markets, Market.PERP.value)),
                                 "instruments": _with_item(_with_item(m.instruments, spot), perp)})
    card = {"instrument_id": perp, "side": "SELL", "order_type": "MARKET", "allocation_pct": float(m.max_position_pct),
            "stop_loss": float(mark * Decimal("1.02")), "setup_grade": "B"}
    plan = await card_leverage_plan(pool, redis, probe, card, shrink_margin_to_fit=True)
    return None if plan.ok else f"ORDER_ROUTE_REFUSED: {plan.reject}"


DEFAULT_PROBES = Probes(fetch_perp_symbols, route_refusal)


async def coin_refusal(pool, redis, m: Mandate, spot: str, perps: set[str] | None, probes: Probes,
                       now_ns: int) -> str | None:
    """The first of the seven checks a coin fails, by name, else None."""
    symbol = spot.split(".")[0]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_active, status, price_increment, size_increment, min_notional FROM instruments WHERE instrument_id = $1",
            spot)
        if row is None or not row["price_increment"] or not row["size_increment"] or not row["min_notional"]:
            return "INSTRUMENT_METADATA_MISSING"
        if not row["is_active"] or row["status"] != "TRADING":
            return "INSTRUMENT_NOT_TRADING"
        newest = await conn.fetchval(
            "SELECT max(last_timestamp_ns) FROM ingest_checkpoints WHERE symbol = $1 AND timeframe = '1m'", symbol)
        if newest is None or now_ns - int(newest) > FEED_STALE_NS:
            return "STALE_FEED"
        if perps is None:
            return "PERP_STATUS_UNKNOWN"
        if symbol not in perps:
            return "PERP_NOT_TRADING"
        rows = await conn.fetch(
            """SELECT funding_interval_hours, funding_time_ns FROM funding_rates WHERE instrument_id = $1
               ORDER BY funding_time_ns DESC LIMIT 2""", instrument_for(spot, Market.PERP))
        if settlement_interval_hours(dict(r) for r in rows) is None:
            return "FUNDING_INTERVAL_UNKNOWN"
        holder = await profile_lifecycle.coin_holder(conn, spot)
        if holder is not None and holder[0] != m.id:
            return "COIN_LOCKED"
    return await probes.route_refusal(pool, redis, m, spot)


async def read_policy(conn) -> Policy:
    return parse_policy(await conn.fetchval("SELECT value FROM system_settings WHERE key = $1", POLICY_KEY))


async def read_ledger(conn, version: int, mandate_id: uuid.UUID | None = None) -> Ledger:
    rows = await conn.fetch(
        """SELECT item_kind, item_value FROM paper_coverage_ledger WHERE policy_version = $1
           AND ($2::uuid IS NULL OR mandate_id = $2)""", version, mandate_id)
    return {(r["item_kind"], r["item_value"]) for r in rows}


async def apply_changes(pool, mandate_id: uuid.UUID, policy: Policy, seen_updated_at: Any, changes: list[Change]) -> str:
    """One short transaction: everything lands or nothing does. Returns APPLIED or why not."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", POLICY_KEY)
            m = await load_mandate(conn, mandate_id, for_update=True)
            current = await read_policy(conn)
            updated_at = await conn.fetchval("SELECT updated_at FROM ai_mandates WHERE id = $1", mandate_id)
            if not current.enabled or current.version != policy.version or updated_at != seen_updated_at:
                return "ABORTED_CONCURRENT_EDIT"
            if reason := eligibility_reason(m, current):
                return reason
            new = fields_after(m, changes)
            if errors := selection_errors(m.style, new["direction"], new["strategies"], new["timeframes"]):
                return f"INVALID_SELECTION: {', '.join(errors)}"
            await conn.execute(
                """UPDATE ai_mandates SET direction = $2, strategies = $3, timeframes = $4, markets = $5, instruments = $6,
                       allow_perps = $7, updated_at = now() WHERE id = $1""",
                mandate_id, new["direction"], new["strategies"], new["timeframes"], new["markets"], new["instruments"],
                new["allow_perps"])
            await profile_lifecycle.sync_locks(conn, await load_mandate(conn, mandate_id))
            for c in changes:
                await conn.execute(
                    """INSERT INTO paper_coverage_ledger (policy_version, mandate_id, item_kind, item_value, old_value)
                       VALUES ($1, $2, $3, $4, $5)""", policy.version, mandate_id, c.kind, c.value, c.old_value)
            await conn.execute(
                "INSERT INTO audit_events (event_type, source, details) VALUES ('PAPER_COVERAGE_APPLIED', 'orchestrator', $1::jsonb)",
                json.dumps({"mandate_id": str(mandate_id), "policy_version": policy.version, "actor": policy.actor,
                            "changes": [asdict(c) for c in changes]}))
    return "APPLIED"


async def _fed_coins(redis) -> list[str] | None:
    try:
        raw = await redis.get(INGEST_UNIVERSE_KEY)
        universe = IngestUniverse.parse(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception as e:
        logger.warning("Could not read %s: %s", INGEST_UNIVERSE_KEY, e)
        return None
    return None if universe is None else [f"{s}.BINANCE" for s in sorted(universe.symbols)]


async def run_cycle(pool, redis, *, probes: Probes = DEFAULT_PROBES, apply: bool = True, now_ns: int | None = None,
                    policy: Policy | None = None) -> list[Result]:
    """One pass. ``apply=False`` is the preview: it plans, checks and reports, and writes nothing."""
    now_ns = now_ns or time.time_ns()
    async with pool.acquire() as conn:
        policy = policy or await read_policy(conn)
    if apply and not policy.enabled:
        return []
    perps = await probes.perp_symbols()
    results: list[Result] = []
    applied = 0
    eligible: list[Mandate] = []

    async def attempt(m: Mandate, seen: Any, subject: str, changes: list[Change], coins: Iterable[str]) -> None:
        nonlocal applied
        for spot in coins:
            if refused := await coin_refusal(pool, redis, m, spot, perps, probes, now_ns):
                results.append(Result(subject, f"{spot.split('.')[0]}: {refused}", changes))
                return
        if not apply:
            results.append(Result(subject, "PLANNED", changes))
            return
        if applied >= policy.max_changes_per_cycle:
            results.append(Result(subject, "CYCLE_LIMIT", changes))
            return
        try:
            outcome = await apply_changes(pool, m.id, policy, seen, changes)
        except profile_lifecycle.LifecycleError as e:
            outcome = str(e.code if hasattr(e, "code") else e)
        except asyncpg.PostgresError as e:
            outcome = f"APPLY_FAILED: {type(e).__name__}"
        applied += outcome == "APPLIED"
        results.append(Result(subject, outcome, changes))

    for pid in policy.profiles:
        async with pool.acquire() as conn:
            m = await load_mandate(conn, pid)
            seen = await conn.fetchval("SELECT updated_at FROM ai_mandates WHERE id::text = $1", pid)  # taken with the plan
            reason = eligibility_reason(m, policy)
            ledger = await read_ledger(conn, policy.version, m.id) if m is not None else set()
        if reason:
            results.append(Result(pid if m is None else m.name, reason))
            continue
        assert m is not None
        if errors := selection_errors(m.style, m.direction, m.strategies, m.timeframes):
            results.append(Result(m.name, f"INVALID_SELECTION: {', '.join(errors)}"))
            continue
        eligible.append(m)
        changes = plan_scope(m, policy, ledger)
        if not changes:
            results.append(Result(m.name, "NO_CHANGE"))
            continue
        await attempt(m, seen, m.name, changes, sorted({underlying_spot(i) for i in m.instruments}))

    if policy.allow_new_coins:
        fed = await _fed_coins(redis)
        if fed is None:
            results.append(Result("new coins", "FEED_UNIVERSE_UNKNOWN"))
        else:
            async with pool.acquire() as conn:
                held = {r["base_asset"] for r in await conn.fetch("SELECT base_asset FROM ai_profile_coin_locks")}
                done = {r["item_value"] for r in await conn.fetch(
                    "SELECT item_value FROM paper_coverage_ledger WHERE policy_version = $1 AND item_kind = $2",
                    policy.version, COIN)}
            for spot in fed:
                if base_asset(spot) in held or spot in done:
                    continue
                if not eligible:
                    results.append(Result(spot.split(".")[0], "NO_ELIGIBLE_PAPER_PROFILE"))
                    continue
                owner = eligible[0]
                async with pool.acquire() as conn:
                    seen = await conn.fetchval("SELECT updated_at FROM ai_mandates WHERE id = $1", owner.id)
                await attempt(owner, seen, f"{spot.split('.')[0]} to {owner.name}", [Change(COIN, spot)], [spot])
    return results


async def coverage_loop(pool, redis) -> None:
    """The orchestrator's worker. Disabled policy: a settings read every cycle and nothing else."""
    while True:
        try:
            for r in await run_cycle(pool, redis):
                if r.outcome != "NO_CHANGE":
                    logger.info("Paper coverage %s: %s %s", r.subject, r.outcome, [asdict(c) for c in r.changes])
        except Exception as e:
            logger.warning("Paper coverage cycle failed: %s", e)
        await asyncio.sleep(CYCLE_S)


async def items_in_place(conn, version: int | None = None) -> list[dict[str, Any]]:
    """Ledger items the profile still has. A later manual edit is left alone: an item it changed is not listed."""
    rows = await conn.fetch(
        """SELECT l.*, m.name, m.direction, m.strategies, m.timeframes, m.markets, m.instruments
           FROM paper_coverage_ledger l JOIN ai_mandates m ON m.id = l.mandate_id
           WHERE ($1::int IS NULL OR l.policy_version = $1) ORDER BY l.id""", version)
    out = []
    for r in rows:
        kind, value = r["item_kind"], r["item_value"]
        present = {DIRECTION: r["direction"] == value, STRATEGY: value in r["strategies"], TIMEFRAME: value in r["timeframes"],
                   MARKET: value in r["markets"], COIN: value in {underlying_spot(i) for i in r["instruments"]}}[kind]
        if present:
            out.append(dict(r))
    return out


async def undo(pool, ids: set[int]) -> list[str]:
    """Remove confirmed items, one transaction per profile. A coin or market with an open position stays."""
    report: list[str] = []
    async with pool.acquire() as conn:
        items = [i for i in await items_in_place(conn) if i["id"] in ids]
    for mid in {i["mandate_id"] for i in items}:
        mine = [i for i in items if i["mandate_id"] == mid]
        name = mine[0]["name"]
        kept: list[str] = []
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    m = await load_mandate(conn, mid, for_update=True)
                    in_use = await profile_lifecycle._coins_in_use(conn, m.instance_id)
                    direction, strategies = str(m.direction), list(m.strategies)
                    timeframes, markets = list(m.timeframes), sorted(str(x) for x in m.markets)
                    instruments = list(m.instruments)
                    for i in mine:
                        kind, value = i["item_kind"], i["item_value"]
                        if kind == DIRECTION:
                            direction = i["old_value"] or direction
                        elif kind == STRATEGY:
                            strategies.remove(value)
                        elif kind == TIMEFRAME:
                            timeframes.remove(value)
                        elif kind == MARKET:
                            if any(base_asset(x) in in_use for x in instruments):
                                kept.append(f"{i['id']} kept: open position on the {value} market")
                                continue
                            markets.remove(value)
                            instruments = [x for x in instruments
                                           if x != instrument_for(x, Market(value))]
                        elif kind == COIN:
                            if base_asset(value) in in_use:
                                kept.append(f"{i['id']} kept: {value} has an open position or working order")
                                continue
                            instruments = [x for x in instruments if underlying_spot(x) != value]
                        kept.append(f"{i['id']} removed: {kind} {value}")
                    await conn.execute(
                        """UPDATE ai_mandates SET direction = $2, strategies = $3, timeframes = $4, markets = $5,
                               instruments = $6, allow_perps = $7, updated_at = now() WHERE id = $1""",
                        mid, direction, strategies, timeframes, markets, instruments, Market.PERP.value in markets)
                    await profile_lifecycle.sync_locks(conn, await load_mandate(conn, mid))
                    await conn.execute(
                        "INSERT INTO audit_events (event_type, source, details) VALUES ('PAPER_COVERAGE_UNDONE', 'host', $1::jsonb)",
                        json.dumps({"mandate_id": str(mid), "report": kept}))
            report += kept
        except asyncpg.PostgresError as e:  # e.g. a short profile cannot lose its only short market
            report.append(f"{name}: nothing removed ({type(e).__name__}: {e})")
    return report


async def write_policy(conn, policy: Policy, actor: str) -> Policy:
    """Same content keeps its version, so disable then enable never reapplies what the operator removed."""
    def content(p: Policy) -> tuple[Any, ...]:
        return p.profiles, p.allowed_modes, p.allowed_presets, p.allow_new_coins, p.max_changes_per_cycle

    async with conn.transaction():
        # The lock apply_changes holds: a write waits for an apply in progress, and later applies see the new policy
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", POLICY_KEY)
        before = await read_policy(conn)
        version = before.version if before.version and content(before) == content(policy) else before.version + 1
        out = Policy(**{**asdict(policy), "version": version, "actor": actor,
                        "updated_at": datetime.datetime.now(datetime.UTC).isoformat()})
        await conn.execute(
            """INSERT INTO system_settings (key, value, updated_at) VALUES ($1, $2::jsonb, now())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            POLICY_KEY, json.dumps(asdict(out)))
    return out


async def _resolve_profiles(conn, names: str) -> tuple[str, ...]:
    out = []
    for name in (n.strip() for n in names.split(",") if n.strip()):
        row = await conn.fetchrow("SELECT id FROM ai_mandates WHERE (name = $1 OR id::text = $1) AND status <> 'ARCHIVED'", name)
        if row is None:
            raise SystemExit(f"No profile named {name}")
        out.append(str(row["id"]))
    return tuple(out)


def wanted_policy(current: Policy, args: argparse.Namespace, profiles: tuple[str, ...]) -> Policy:
    """The policy a preview or enable asks for. An option left out keeps its current value."""
    return Policy(**{**asdict(current), "profiles": profiles,
                     "allow_new_coins": current.allow_new_coins if args.new_coins is None else args.new_coins,
                     "allowed_modes": tuple(m.strip().upper() for m in args.modes.split(",")) if args.modes else current.allowed_modes,
                     "enabled": args.command == "enable"})


def _print(results: list[Result]) -> None:
    if not results:
        print("Nothing to do.")
    for r in results:
        print(f"{r.subject}: {r.outcome}")
        for c in r.changes:
            print(f"    {c.kind} {c.value}" + (f" (was {c.old_value})" if c.old_value else ""))


async def _cli(args: argparse.Namespace) -> int:
    import redis.asyncio as aioredis

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=4)
    redis = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        async with pool.acquire() as conn:
            current = await read_policy(conn)
        if args.command == "disable":
            async with pool.acquire() as conn:
                out = await write_policy(conn, Policy(**{**asdict(current), "enabled": False}), args.actor)
            print(f"Disabled, version {out.version}. What was applied stays as explicit settings.")
        elif args.command in ("preview", "enable"):
            async with pool.acquire() as conn:
                profiles = await _resolve_profiles(conn, args.profiles) if args.profiles else current.profiles
            wanted = wanted_policy(current, args, profiles)
            if not wanted.profiles:
                raise SystemExit("Name the profiles: --profiles NAME[,NAME] in priority order.")
            if args.command == "enable":
                async with pool.acquire() as conn:
                    wanted = await write_policy(conn, wanted, args.actor)
                print(f"Enabled, version {wanted.version}. The worker runs within {CYCLE_S}s. Disable or undo with this tool.")
            print(f"Modes {','.join(wanted.allowed_modes)}, new coins {'on' if wanted.allow_new_coins else 'off'}. What the policy would change:")
            _print(await run_cycle(pool, redis, apply=False, policy=wanted))
        elif args.command == "undo":
            async with pool.acquire() as conn:
                items = await items_in_place(conn)
            if not args.confirm:
                for i in items:
                    print(f"{i['id']}  {i['name']}  {i['item_kind']} {i['item_value']}" + (f" (was {i['old_value']})" if i["old_value"] else ""))
                print("Nothing removed. Rerun with --confirm ID[,ID] or --confirm all." if items else "No ledger items in place.")
            else:
                ids = {i["id"] for i in items} if args.confirm == "all" else {int(x) for x in args.confirm.split(",")}
                print("\n".join(await undo(pool, ids)) or "Nothing removed.")
    finally:
        await redis.aclose()
        await pool.close()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paper auto-coverage: paper profiles only, and it sends no order.")
    parser.add_argument("command", choices=("preview", "enable", "disable", "undo"))
    parser.add_argument("--profiles", help="profile names or ids, in priority order")
    parser.add_argument("--modes", help="allowed modes, default ASK. Rule-based entries need ASK,AUTO")
    parser.add_argument("--new-coins", action=argparse.BooleanOptionalAction, default=None,
                        help="assign fed coins nobody covers, or stop assigning them. Left out, the current value stays")
    parser.add_argument("--confirm", help="undo: ledger ids to remove, or all")
    parser.add_argument("--actor", default=os.environ.get("USER", "operator"))
    return parser


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_cli(_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
