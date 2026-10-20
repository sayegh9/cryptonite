"""Exit monitor (AI-first blueprint A2.6): deterministic exits by default, exit debates only on events.

Prices come from the same live quote stream the paper orchestrator fills against
(``cryptonite:stream:ui_quote_tick``). When that quote is older than ``MAX_QUOTE_AGE_S`` the stop is
checked against Binance's REST book ticker instead (the stream stalls on every reconnect of a flaky
network, and a stale quote used to skip the stop). Only a position with neither price is not evaluated:
it is "blind". Blind time is counted per instrument (``exit_monitor:blind_s:<hour>``) and the current
price source per position is published at ``exit_monitor:coverage`` for Mission Control; a CRITICAL
NO_FRESH_QUOTE alert goes out once a position has been blind for ``BLIND_ALERT_CYCLES`` checks in a row.

Every ``INTERVAL_S`` each open paper position is evaluated with pure rules (``evaluate_exit``):
- hard stop: loss >= ``STOP_LOSS_PCT`` at the live exit price (bid for longs, ask for shorts)
- trailing stop: once the gain reached ``TRAIL_ACTIVATE_PCT``, a give-back of ``TRAIL_PCT`` from the
  best price seen since the position opened

A triggered stop closes the position with a PAPER ``CLOSE_POSITION`` command (risk reducing, so it
runs immediately; blueprint D8) when the position belongs to an AI owner: ``AI_DEBATE_WORKER`` or a
mandate's own instance ``AI_MANDATE_<id>`` (the close targets that owner, so the bridge books it on the
owner's position row). Positions are evaluated per owner. On a spot instrument another owner also holds
(a manual trade, another mandate) the close carries the owner's own ``quantity``, so it sells only that
and leaves the other holding alone; the 2026-10-08 BTC hard stop only alerted for 99 minutes because a
manual position shared the instrument. A perp is one net position per instrument, so a shared perp
still only gets an alert, as does any position no AI owns. A rejected close is retried; alerts are debounced.

Perps (leverage plan 1.7): a perp position is priced at its mark (``perp:mark:<SYM>``, the price liquidation
uses) and carries its leverage and liquidation price. ``LIQUIDATION_PROXIMITY``: once the mark has used half of
the buffer between the stop (else the entry) and the liquidation price, a CRITICAL alert; at three quarters, a
reduce-only close of an AI-owned position, as the backup to the resting stop.

Model-proposed exit stops (``exit_decisions.target_stop_price``) are recommendations for the
operator and never trigger an automatic close.

Exit debates (paid) run only on events: the loss first crossing ``DEBATE_DRAWDOWN_PCT`` (once per
drawdown episode, re-armed after the loss recovers to half that), or a funding spike against the
position; debounced per instrument.

The five thresholds are read from ``system_settings`` at the start of every cycle (ADR 0007). The ``EXIT_*``
env vars set only the defaults, used for any key with no stored value. A stored set that does not parse or
breaks a rule is refused, and the last valid set stays in force.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, replace
from decimal import Decimal

import asyncpg
import httpx
import redis.asyncio as redis
from cryptonite_schemas.exit_thresholds import SETTING_KEYS, ExitThresholds, settled
from cryptonite_schemas.runtime_settings import RUNTIME_BOUNDS, read_number

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("exit_monitor")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://cryptonite_api:api_dev_pass@127.0.0.1:5432/cryptonite")
PAPER_ACCOUNT_ID = "PAPER-ACCOUNT-001"
AI_INSTANCE = "AI_DEBATE_WORKER"
HEARTBEAT_KEY = "exit_monitor:heartbeat"
QUOTE_STREAM = "cryptonite:stream:ui_quote_tick"
EXIT_DEBATE_QUEUE_MAXLEN = 10_000  # XADD trims the oldest entries (approximate); was unbounded (D1 / F-H2)

INTERVAL_S = float(os.getenv("EXIT_MONITOR_INTERVAL_S", "30"))
STOP_LOSS_PCT = float(os.getenv("EXIT_STOP_LOSS_PCT", "5"))
TRAIL_ACTIVATE_PCT = float(os.getenv("EXIT_TRAIL_ACTIVATE_PCT", "3"))
TRAIL_PCT = float(os.getenv("EXIT_TRAIL_PCT", "2"))
DEBATE_DRAWDOWN_PCT = float(os.getenv("EXIT_DEBATE_DRAWDOWN_PCT", "2"))
FUNDING_SPIKE = float(os.getenv("EXIT_FUNDING_SPIKE", "0.0005"))  # 0.05% per interval, against the position
MAX_QUOTE_AGE_S = 10.0
DEBATE_DEBOUNCE_S = 4 * 3600
REARM_FRACTION = 0.5  # a drawdown episode ends when the loss recovers to half the trigger
CLOSE_RETRY_AFTER_S = 60
ALERT_DEBOUNCE_S = 3600
BLIND_ALERT_CYCLES = 2  # one blind check is a blip; two in a row (about a minute) pages the operator
COVERAGE_KEY = "exit_monitor:coverage"
BLIND_S_KEY = "exit_monitor:blind_s:{hour}"  # hash: instrument -> blind seconds in that UTC hour
BLIND_S_TTL_S = 26 * 3600
REST_TIMEOUT_S = 3.0
LIQ_ALERT_BUFFER_USED = 0.5  # stop-to-liquidation buffer used: CRITICAL alert
LIQ_CLOSE_BUFFER_USED = 0.75  # ... and a reduce-only close of an AI-owned perp (backup to the resting stop)
SPOT_BOOK_TICKER_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
PERP_BOOK_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"


def default_thresholds() -> ExitThresholds:
    """The env-or-code defaults: what applies when no operator value is stored for a key."""
    return ExitThresholds(
        stop_loss_pct=STOP_LOSS_PCT, trail_activate_pct=TRAIL_ACTIVATE_PCT, trail_pct=TRAIL_PCT,
        debate_drawdown_pct=DEBATE_DRAWDOWN_PCT, funding_spike=FUNDING_SPIKE,
    )


async def load_interval(pool, last_good: float) -> float:
    """The seconds between exit checks, read every cycle (ADR 0007): the stored exit.monitor_interval_s, else the
    EXIT_MONITOR_INTERVAL_S default. A failed read or a refused value keeps the last valid one."""
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval("SELECT value FROM system_settings WHERE key = 'exit.monitor_interval_s'")
    except Exception as e:
        logger.warning("Exit monitor interval not re-read (%s); keeping %ss", e, last_good)
        return last_good
    bound = replace(RUNTIME_BOUNDS["exit.monitor_interval_s"], default=INTERVAL_S)
    value, problem = read_number(raw, bound, last_good)
    if problem:
        logger.error("Exit monitor interval refused, keeping the last valid value: %s", problem)
    return value


async def load_thresholds(pool, last_good: ExitThresholds) -> ExitThresholds:
    """The thresholds for this cycle: the stored set, read every cycle. If the read fails, or the stored set is
    refused, the last valid set stays in force (the same rule the budget guard follows)."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM system_settings WHERE key = ANY($1)", list(SETTING_KEYS))
    except Exception as e:
        logger.warning("Exit thresholds not re-read (%s); keeping the last valid set", e)
        return last_good
    applied, problems = settled({r["key"]: r["value"] for r in rows}, default_thresholds(), last_good)
    for problem in problems:
        logger.error("Exit thresholds refused, keeping the last valid set: %s", problem)
    return applied


@dataclass(frozen=True)
class Position:
    instrument_id: str
    side: str  # LONG | SHORT
    entry_price: float
    current_price: float  # live exit price: bid for longs, ask for shorts
    ai_owned: bool  # an AI owner's position the monitor may close (on a perp: the AI owns the whole instrument)
    opened_ts: int = 0  # identity of the position (earliest open row), for per-position state
    funding_rate: float | None = None
    owner_instance: str = AI_INSTANCE  # the AI instance (legacy or a mandate's) whose rows these are
    price_source: str = "STREAM"  # STREAM (live quote) | REST (backup: the stream quote was stale)
    stop_price: float | None = None
    take_profit_price: float | None = None
    quantity: float = 0.0
    shared: bool = False  # another owner also holds the instrument: a close sells only ``own_quantity``
    own_quantity: str | None = None  # the owner's exact open quantity (decimal string), for a shared close
    key_owner: bool = False  # the key names the owner: another owner's position opened in the same second
    leverage: int = 1  # perps
    liquidation_price: float | None = None  # perps: the snapshot written with the position (margin.py)

    @property
    def liquidation_buffer_used(self) -> float | None:
        """How much of the buffer between the stop (else the entry) and the liquidation price the price has used:
        0 at the stop, 1 at liquidation, negative while on the safe side of the stop. None without a liquidation
        price."""
        liq = self.liquidation_price
        if liq is None or liq <= 0 or not self.entry_price:
            return None
        long = self.side == "LONG"
        stop_ok = self.stop_price is not None and (liq < self.stop_price if long else self.stop_price < liq)
        ref = self.stop_price if stop_ok else self.entry_price
        span = (ref - liq) if long else (liq - ref)
        if span <= 0:
            return None
        return ((ref - self.current_price) if long else (self.current_price - ref)) / span

    @property
    def pnl_pct(self) -> float:
        if not self.entry_price:
            return 0.0
        move = (self.current_price - self.entry_price) / self.entry_price * 100
        return move if self.side == "LONG" else -move

    @property
    def key(self) -> str:
        key = f"{self.instrument_id}:{self.side}:{self.opened_ts}"
        return f"{key}:{self.owner_instance}" if self.key_owner else key


@dataclass(frozen=True)
class ExitAction:
    kind: str  # CLOSE | EXIT_DEBATE | ALERT | REDUCE_TP1
    reason: str


def evaluate_exit(pos: Position, best_price: float, debate_armed: bool, in_drawdown_episode: bool = False,
                  t: ExitThresholds | None = None, tp1_done: bool = False) -> list[ExitAction]:
    """Pure exit rules. ``best_price``: most favourable price since the position opened.
    ``debate_armed``: no exit debate within the debounce window. ``in_drawdown_episode``: a drawdown
    debate already ran and the loss has not recovered since. ``t``: the thresholds to apply (default: the
    env-or-code defaults)."""
    t = t or default_thresholds()
    long = pos.side == "LONG"
    used = pos.liquidation_buffer_used
    if used is not None and used >= LIQ_CLOSE_BUFFER_USED:
        return [ExitAction("CLOSE", "LIQUIDATION_PROXIMITY")] if pos.ai_owned else [ExitAction("ALERT", "LIQUIDATION_PROXIMITY")]
    proximity = [ExitAction("ALERT", "LIQUIDATION_PROXIMITY")] if used is not None and used >= LIQ_ALERT_BUFFER_USED else []
    stop_reason = None
    if pos.pnl_pct <= -t.stop_loss_pct:
        stop_reason = "HARD_STOP"
    elif pos.stop_price is not None and pos.stop_price > 0:
        if (pos.stop_price < pos.entry_price and pos.current_price <= pos.stop_price) if long else (pos.stop_price > pos.entry_price and pos.current_price >= pos.stop_price):
            stop_reason = "STRUCTURAL_STOP"
    if stop_reason is None and pos.entry_price and best_price:
        best_gain = (best_price - pos.entry_price) / pos.entry_price * 100 * (1 if long else -1)
        give_back = (best_price - pos.current_price) / best_price * 100 * (1 if long else -1)
        if best_gain >= t.trail_activate_pct and give_back >= t.trail_pct:
            stop_reason = "TRAILING_STOP"
    if stop_reason:
        return proximity + ([ExitAction("CLOSE", stop_reason)] if pos.ai_owned else [ExitAction("ALERT", stop_reason)])
    if proximity:
        return proximity

    if not tp1_done and pos.take_profit_price is not None and pos.take_profit_price > 0:
        tp1_hit = (pos.take_profit_price > pos.entry_price and pos.current_price >= pos.take_profit_price) if long else (pos.take_profit_price < pos.entry_price and pos.current_price <= pos.take_profit_price)
        if tp1_hit:
            return [ExitAction("REDUCE_TP1", "TAKE_PROFIT_1")] if pos.ai_owned else [ExitAction("ALERT", "TAKE_PROFIT_1")]

    funding_against = pos.funding_rate is not None and (pos.funding_rate if long else -pos.funding_rate) >= t.funding_spike
    drawdown = pos.pnl_pct <= -t.debate_drawdown_pct and not in_drawdown_episode
    if debate_armed and (drawdown or funding_against):
        return [ExitAction("EXIT_DEBATE", "DRAWDOWN" if drawdown else "FUNDING_SPIKE")]
    return []


class QuoteBook:
    """Latest bid/ask per instrument from the live quote stream."""

    def __init__(self) -> None:
        self.quotes: dict[str, tuple[float, float, float]] = {}  # instrument -> (bid, ask, arrival_s)

    def apply(self, data: dict) -> None:
        arrival_s = int(data.get("arrival_ns", time.time_ns())) / 1e9
        self.quotes[data["instrument_id"]] = (float(data["bid_price"]), float(data["ask_price"]), arrival_s)

    def exit_price(self, instrument_id: str, side: str, now_s: float | None = None) -> float | None:
        quote = self.quotes.get(instrument_id)
        if not quote or (now_s if now_s is not None else time.time()) - quote[2] > MAX_QUOTE_AGE_S:
            return None
        return quote[0] if side == "LONG" else quote[1]

    async def follow(self, redis_client) -> None:
        last_id = "$"
        try:
            for _, fields in reversed(await redis_client.xrevrange(QUOTE_STREAM, "+", "-", count=500) or []):
                self._apply_fields(fields)
        except Exception as e:
            logger.error(f"Quote backfill failed: {e}")
        while True:
            try:
                for _, msgs in await redis_client.xread({QUOTE_STREAM: last_id}, count=500, block=1000) or []:
                    for msg_id, fields in msgs:
                        last_id = msg_id
                        self._apply_fields(fields)
            except Exception as e:
                logger.warning(f"Quote stream error: {e}")
                await asyncio.sleep(1)

    def _apply_fields(self, fields) -> None:
        try:
            raw = fields.get("data") or fields.get(b"data")
            self.apply(json.loads(raw))
        except Exception:
            pass


_SOURCE_RANK = {"STREAM": 0, "MARK": 0, "REST": 1, None: 2}  # MARK: a perp at its live mark


async def rest_exit_price(http: httpx.AsyncClient | None, instrument_id: str, side: str) -> float | None:
    """The backup exit price from Binance's REST book ticker (bid for longs, ask for shorts); None on any failure."""
    if http is None:
        return None
    symbol = instrument_id.split(".")[0]
    perp = symbol.endswith("-PERP")
    try:
        resp = await http.get(PERP_BOOK_TICKER_URL if perp else SPOT_BOOK_TICKER_URL,
                              params={"symbol": symbol.removesuffix("-PERP").upper()}, timeout=REST_TIMEOUT_S)
        resp.raise_for_status()
        book = resp.json()
        price = float(book["bidPrice"] if side == "LONG" else book["askPrice"])
        return price if price > 0 else None
    except Exception as e:
        logger.warning("Backup REST price for %s failed: %s", instrument_id, e)
        return None


class Coverage:
    """Which price each position's stops ran on, and how long each has been blind (no price at all)."""

    def __init__(self) -> None:
        self.blind_cycles: dict[str, int] = {}  # instrument -> consecutive blind checks
        self.blind_since: dict[str, float] = {}  # instrument -> first blind check of the current run
        self.last_check: float | None = None

    async def record(self, redis_client, sources: dict[str, str | None], now_s: float | None = None) -> None:
        """``sources``: instrument -> STREAM | REST, or None when blind. Counts blind seconds, alerts on
        sustained blindness, and publishes the snapshot Mission Control reads."""
        now_s = time.time() if now_s is None else now_s
        elapsed = min(now_s - self.last_check, 3 * INTERVAL_S) if self.last_check else 0.0
        self.last_check = now_s
        hour_key = BLIND_S_KEY.format(hour=time.strftime("%Y%m%d%H", time.gmtime(now_s)))
        for inst in list(self.blind_cycles):
            if sources.get(inst, "gone") is not None:  # priced again, or the position closed
                self.blind_cycles.pop(inst, None)
                self.blind_since.pop(inst, None)
        for inst, source in sources.items():
            if source is not None:
                continue
            cycles = self.blind_cycles[inst] = self.blind_cycles.get(inst, 0) + 1
            self.blind_since.setdefault(inst, now_s)
            if elapsed:
                await redis_client.hincrbyfloat(hour_key, inst, round(elapsed, 1))
                await redis_client.expire(hour_key, BLIND_S_TTL_S)
            if cycles >= BLIND_ALERT_CYCLES and await redis_client.set(
                    f"exit:alert:{inst}:NO_QUOTE", "1", nx=True, ex=ALERT_DEBOUNCE_S):
                await redis_client.publish("channel:system_alerts", json.dumps({
                    "type": "EXIT_MONITOR", "severity": "CRITICAL", "instrument_id": inst,
                    "action": "ALERT", "reason": "NO_FRESH_QUOTE",
                    "blind_s": round(now_s - self.blind_since[inst]),
                }))
        await redis_client.set(COVERAGE_KEY, json.dumps({
            "checked_at": now_s,
            "positions": [{"instrument_id": inst, "source": source,
                           "blind_since": self.blind_since.get(inst)} for inst, source in sorted(sources.items())],
        }), ex=int(max(90, 3 * INTERVAL_S)))


async def load_positions(conn, redis_client, quotes: QuoteBook, http: httpx.AsyncClient | None = None,
                         sources: dict[str, str | None] | None = None) -> list[Position]:
    """Open positions priced for their stops. ``sources`` (filled in when given): instrument -> the price
    source used, None for a blind position."""
    rows = await conn.fetch(
        """
        WITH open_rows AS (
            SELECT * FROM paper_positions WHERE account_id = $1 AND status = 'OPEN' AND quantity > 0
        ), owners AS (  -- how many instances hold the instrument (either side)
            SELECT instrument_id, COUNT(DISTINCT instance_id) AS n_owners FROM open_rows GROUP BY instrument_id
        )
        -- one position per owner: an AI owner (the legacy AI instance or a mandate's own instance, A5.2)
        -- keeps its stops on a spot instrument another owner shares
        SELECT r.instrument_id, r.side, r.instance_id AS owner,
               SUM(r.quantity) AS quantity,
               SUM(r.quantity * r.entry_price) / NULLIF(SUM(r.quantity), 0) AS entry_price,
               MIN(r.opened_at) AS opened_at,
               (r.instance_id = $2 OR r.instance_id LIKE 'AI!_MANDATE!_%' ESCAPE '!')
                   AND (o.n_owners = 1 OR r.instrument_id NOT LIKE '%-PERP.%') AS ai_owned,
               o.n_owners > 1 AS shared,
               MAX(r.leverage) AS leverage,
               -- the liquidation nearest the price: highest for a long, lowest for a short
               CASE WHEN r.side = 'SHORT' THEN MIN(r.liquidation_price) ELSE MAX(r.liquidation_price) END AS liquidation_price
        FROM open_rows r JOIN owners o USING (instrument_id)
        GROUP BY r.instrument_id, r.side, r.instance_id, o.n_owners
        ORDER BY r.instrument_id, r.side, MIN(r.opened_at), r.instance_id
        """,
        PAPER_ACCOUNT_ID, AI_INSTANCE,
    )
    positions = []
    sources = {} if sources is None else sources
    keys: set[str] = set()
    for r in rows:
        inst = r["instrument_id"]
        mark = await perp_mark(redis_client, inst) if is_perp(inst) else None
        if mark is not None:  # a perp is valued at its mark, the price liquidation uses
            price, source = mark, "MARK"
        else:
            price, source = quotes.exit_price(inst, r["side"]), "STREAM"
        if price is None:
            price, source = await rest_exit_price(http, inst, r["side"]), "REST"
        if price is None:
            source = None
            # A stop monitor without prices must fail loudly, not silently skip (Coverage alerts)
            logger.warning("No fresh or backup price for %s: stops are not being evaluated", inst)
        # both sides of an instrument: it is as covered as its worst-covered side
        sources[inst] = max(sources[inst], source, key=_SOURCE_RANK.__getitem__) if inst in sources else source
        if price is None:
            continue
        funding = None
        sym = r["instrument_id"].split(".")[0].replace("-PERP", "").upper()
        try:
            if raw := await redis_client.get(f"perp:mark:{sym}"):
                funding = float(json.loads(raw).get("funding_rate"))
        except Exception:
            pass
        stop_price: float | None = None
        take_profit_price: float | None = None
        if redis_client is not None:
            try:
                if raw_plan := await redis_client.get(f"exit:plan:{inst}:{r['owner']}"):
                    plan_data = json.loads(raw_plan)
                    sl = plan_data.get("stop_price") or plan_data.get("proposed_stop_loss")
                    if sl is not None and float(sl) > 0:
                        stop_price = float(sl)
                    tp = plan_data.get("take_profit_price") or plan_data.get("proposed_take_profit")
                    if tp is not None and float(tp) > 0:
                        take_profit_price = float(tp)
            except Exception:
                pass
        if stop_price is None or take_profit_price is None:
            try:
                cmd_row = await conn.fetchrow(
                    """
                    SELECT payload FROM execution_commands
                    WHERE target_instance_id = $1
                      AND command_type = 'SUBMIT_ORDER'
                      AND status = 'COMPLETED'
                      AND (payload->>'instrument_id' = $2 OR upper(split_part(payload->>'instrument_id', '.', 1)) = upper(split_part($2, '.', 1)))
                    ORDER BY created_at DESC, command_id DESC
                    LIMIT 1
                    """,
                    r["owner"], inst,
                )
                if cmd_row and cmd_row["payload"]:
                    payload = json.loads(cmd_row["payload"]) if isinstance(cmd_row["payload"], str) else cmd_row["payload"]
                    if stop_price is None:
                        sl = payload.get("proposed_stop_loss") or payload.get("stop_price")
                        if sl is not None and float(sl) > 0:
                            stop_price = float(sl)
                    if take_profit_price is None:
                        tp = payload.get("proposed_take_profit") or payload.get("take_profit_price") or payload.get("take_profit")
                        if tp is not None and float(tp) > 0:
                            take_profit_price = float(tp)
            except Exception as e:
                logger.debug("Failed to lookup entry command stop/target for %s: %s", inst, e)
        opened_ts = int(r["opened_at"].timestamp()) if r["opened_at"] else 0
        position = Position(
            r["instrument_id"], r["side"], float(r["entry_price"] or 0), price, bool(r["ai_owned"]),
            opened_ts, funding,
            r["owner"] if r["ai_owned"] else AI_INSTANCE, source,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            quantity=float(r["quantity"] or 0),
            leverage=int(_row(r, "leverage") or 1),
            liquidation_price=float(_row(r, "liquidation_price")) if _row(r, "liquidation_price") else None,
            shared=bool(r["shared"]),
            own_quantity=format(Decimal(str(r["quantity"] or 0)).normalize(), "f"),
        )
        if position.key in keys:  # two owners opened in the same second: keep their state and closes apart
            position = replace(position, key_owner=True)
        keys.add(position.key)
        positions.append(position)
    return positions


def _row(r, key):
    try:
        return r[key]
    except (KeyError, IndexError):
        return None


async def perp_mark(redis_client, instrument_id: str) -> float | None:
    """The cached mark (15 s TTL, so a present key is fresh), or None."""
    if redis_client is None:
        return None
    sym = instrument_id.split(".")[0].replace("-PERP", "").upper()
    try:
        raw = await redis_client.get(f"perp:mark:{sym}")
        return float(json.loads(raw)["mark_price"]) if raw else None
    except Exception:
        return None


async def best_price_since_open(redis_client, pos: Position) -> float:
    key = f"exit:best:{pos.key}"
    stored = await redis_client.get(key)
    best = float(stored) if stored else pos.entry_price
    best = max(best, pos.current_price) if pos.side == "LONG" else min(best, pos.current_price)
    await redis_client.set(key, str(best), ex=30 * 86400)
    return best


async def enqueue_close(conn, pos: Position, reason: str) -> bool:
    """One close per position and reason; a REJECTED/FAILED close is retried after a pause."""
    prefix = f"exit_monitor:{pos.key}:{reason}"
    last = await conn.fetchrow(
        "SELECT status, created_at FROM execution_commands WHERE idempotency_key LIKE $1 || '%' ORDER BY created_at DESC LIMIT 1",
        prefix,
    )
    if last is not None:
        if last["status"] not in ("REJECTED", "FAILED"):
            return False  # pending, running or done
        if time.time() - last["created_at"].timestamp() < CLOSE_RETRY_AFTER_S:
            return False
    attempts = await conn.fetchval("SELECT count(*) FROM execution_commands WHERE idempotency_key LIKE $1 || '%'", prefix)
    await conn.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id,
               requester_id, payload, status, environment, account_id)
           VALUES ($1, $2, 'CLOSE_POSITION', $3, 'risk:exit_monitor', $4::jsonb, 'PENDING', 'PAPER', $5)
           ON CONFLICT (idempotency_key) DO NOTHING""",
        uuid.uuid4(), f"{prefix}:{attempts}", pos.owner_instance, json.dumps(close_payload(pos, reason)), PAPER_ACCOUNT_ID,
    )
    return True


def close_payload(pos: Position, reason: str) -> dict:
    """The CLOSE_POSITION payload. Alone on its instrument the owner's close flattens the instrument; on a shared
    spot instrument it carries the owner's own quantity, so the orchestrator sells only that."""
    payload = {"instrument_id": pos.instrument_id, "reason": reason}
    if pos.shared and pos.own_quantity:
        payload["quantity"] = pos.own_quantity
    return payload


async def enqueue_tp1_reduce(conn, pos: Position, reason: str = "TAKE_PROFIT_1") -> bool:
    """One TP1 50% scale-out per position; enqueues a SUBMIT_ORDER to reduce holding."""
    if pos.quantity <= 0:
        return False
    prefix = f"exit_monitor:{pos.key}:{reason}"
    last = await conn.fetchrow(
        "SELECT status, created_at FROM execution_commands WHERE idempotency_key LIKE $1 || '%' ORDER BY created_at DESC LIMIT 1",
        prefix,
    )
    if last is not None:
        if last["status"] not in ("REJECTED", "FAILED"):
            return False  # pending, running or done
        if time.time() - last["created_at"].timestamp() < CLOSE_RETRY_AFTER_S:
            return False
    attempts = await conn.fetchval("SELECT count(*) FROM execution_commands WHERE idempotency_key LIKE $1 || '%'", prefix)
    half_qty = (Decimal(str(pos.quantity)) / Decimal(2)).normalize()
    half_qty_str = format(half_qty, "f")
    if is_perp(pos.instrument_id):
        # A perp scales out through the close path with a quantity (a reduce-only fill of half, the resting stop
        # stays for the rest): a SUBMIT_ORDER on the other side would be a new entry, not a reduction
        command_type = "CLOSE_POSITION"
        payload = {"instrument_id": pos.instrument_id, "quantity": half_qty_str, "reason": reason,
                   "source": "exit_monitor_tp1"}
    else:
        command_type = "SUBMIT_ORDER"
        payload = {
            "instrument_id": pos.instrument_id,
            "side": "SELL" if pos.side == "LONG" else "BUY",
            "order_type": "MARKET",
            "quantity": half_qty_str,
            "reason": reason,
            "source": "exit_monitor_tp1",
        }
    await conn.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id,
               requester_id, payload, status, environment, account_id)
           VALUES ($1, $2, $6, $3, 'risk:exit_monitor', $4::jsonb, 'PENDING', 'PAPER', $5)
           ON CONFLICT (idempotency_key) DO NOTHING""",
        uuid.uuid4(), f"{prefix}:{attempts}", pos.owner_instance,
        json.dumps(payload), PAPER_ACCOUNT_ID, command_type,
    )
    return True


def is_perp(instrument_id: str) -> bool:
    return instrument_id.split(".", 1)[0].upper().endswith("-PERP")


async def act(conn, redis_client, pos: Position, action: ExitAction) -> None:
    if action.kind == "CLOSE":
        if await enqueue_close(conn, pos, action.reason):
            logger.warning("%s: closing %s %s (pnl %.2f%%)", action.reason, pos.side, pos.instrument_id, pos.pnl_pct)
    elif action.kind == "REDUCE_TP1":
        if await enqueue_tp1_reduce(conn, pos, action.reason):
            await redis_client.set(f"exit:tp1:{pos.key}", "1", ex=30 * 86400)
            logger.info("%s: reducing 50%% %s %s (pnl %.2f%%)", action.reason, pos.side, pos.instrument_id, pos.pnl_pct)
    elif action.kind == "EXIT_DEBATE":
        run_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at) VALUES ($1, $2, 'RUNNING', $3, now())",
            run_id, pos.instrument_id, f"exit_monitor:{action.reason}",
        )
        await redis_client.xadd("stream:exit_debate_queue", {"instrument_id": pos.instrument_id, "run_id": str(run_id)},
                                maxlen=EXIT_DEBATE_QUEUE_MAXLEN, approximate=True)
        await redis_client.set(f"debounce:exit:{pos.instrument_id}", "1", ex=DEBATE_DEBOUNCE_S)
        logger.info("Exit debate for %s (%s, pnl %.2f%%)", pos.instrument_id, action.reason, pos.pnl_pct)
    if await redis_client.set(f"exit:alert:{pos.key}:{action.kind}:{action.reason}", "1", nx=True, ex=ALERT_DEBOUNCE_S):
        alert = {
            "type": "EXIT_MONITOR", "severity": "INFO" if action.kind in ("EXIT_DEBATE", "REDUCE_TP1") else "CRITICAL",
            "instrument_id": pos.instrument_id, "action": action.kind, "reason": action.reason,
            "pnl_pct": round(pos.pnl_pct, 2), "ai_owned": pos.ai_owned,
        }
        if action.reason == "LIQUIDATION_PROXIMITY":
            # Leverage plan 4.6: the notifier sends CRITICAL system alerts to Telegram and ntfy; this one says how
            # close the position is and what the monitor does about it
            used = pos.liquidation_buffer_used
            alert.update({
                "leverage": pos.leverage, "mark_price": pos.current_price, "liquidation_price": pos.liquidation_price,
                "buffer_used_pct": round(used * 100, 1) if used is not None else None,
                "message": (f"{pos.instrument_id} {pos.side} {pos.leverage}x: the mark {pos.current_price:g} has used "
                            f"{(used or 0) * 100:.0f}% of the buffer from the stop (else the entry) to liquidation "
                            f"({pos.liquidation_price:g}). "
                            + ("Closing it (reduce-only market)." if action.kind == "CLOSE" else
                               "Alert only: a manual position is not closed by the monitor.")),
            })
        await redis_client.publish("channel:system_alerts", json.dumps(alert))


async def monitor_once(pool, redis_client, quotes: QuoteBook, t: ExitThresholds | None = None,
                       http: httpx.AsyncClient | None = None, coverage: Coverage | None = None) -> None:
    t = t or default_thresholds()
    sources: dict[str, str | None] = {}
    async with pool.acquire() as conn:
        positions = await load_positions(conn, redis_client, quotes, http, sources)
        # the loop passes one Coverage for its lifetime: blind checks in a row are counted across cycles
        await (coverage or Coverage()).record(redis_client, sources)
        for pos in positions:
            best = await best_price_since_open(redis_client, pos)
            episode_key = f"exit:dd_episode:{pos.key}"
            if pos.pnl_pct > -t.debate_drawdown_pct * REARM_FRACTION:
                await redis_client.delete(episode_key)
            armed = not await redis_client.exists(f"debounce:exit:{pos.instrument_id}")
            in_episode = bool(await redis_client.exists(episode_key))
            tp1_done = bool(await redis_client.exists(f"exit:tp1:{pos.key}"))
            for action in evaluate_exit(pos, best, armed, in_episode, t, tp1_done=tp1_done):
                if action.reason == "DRAWDOWN":
                    await redis_client.set(episode_key, "1", ex=30 * 86400)
                await act(conn, redis_client, pos, action)


async def monitor_loop():
    state = default_thresholds()
    interval = INTERVAL_S
    logger.info("Starting exit monitor with %s", state)
    redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=30)  # redis-py 8 times a blocking read out at exactly its block time (LE-3)
    pool = await asyncpg.create_pool(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"), min_size=1, max_size=2)
    quotes = QuoteBook()
    http = httpx.AsyncClient()
    coverage = Coverage()
    follower = asyncio.create_task(quotes.follow(redis_client))
    while True:
        if follower.done():  # supervise the quote feed: never run stops on a dead feed
            logger.error("Quote follower stopped (%s); restarting", follower.exception() if not follower.cancelled() else "cancelled")
            follower = asyncio.create_task(quotes.follow(redis_client))
        try:
            interval = await load_interval(pool, last_good=interval)
            applied = await load_thresholds(pool, last_good=state)
            if applied != state:
                logger.info("Exit thresholds now %s", applied)
            state = applied
            await monitor_once(pool, redis_client, quotes, state, http, coverage)
            # Brain order approvals are refused unless this is fresh: AI-owned positions rely on it for stops.
            # The payload carries the set in force, so the registry can show what the monitor actually applies.
            await redis_client.set(HEARTBEAT_KEY, json.dumps(asdict(state)), ex=int(max(90, 3 * interval)))
        except Exception as e:
            logger.error(f"Error in exit monitor loop: {e}")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(monitor_loop())
