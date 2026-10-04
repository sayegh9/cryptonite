"""Exit monitor (AI-first blueprint A2.6): deterministic exits by default, exit debates only on events.

Prices come from the same live quote stream the paper orchestrator fills against
(``cryptonite:stream:ui_quote_tick``); a position without a fresh quote is not evaluated at all.

Every ``INTERVAL_S`` each open paper position is evaluated with pure rules (``evaluate_exit``):
- hard stop: loss >= ``STOP_LOSS_PCT`` at the live exit price (bid for longs, ask for shorts)
- trailing stop: once the gain reached ``TRAIL_ACTIVATE_PCT``, a give-back of ``TRAIL_PCT`` from the
  best price seen since the position opened

A triggered stop closes the position with a PAPER ``CLOSE_POSITION`` command (risk reducing, so it
runs immediately; blueprint D8) — only when every open row on the instrument, on either side, was
opened by one AI owner: ``AI_DEBATE_WORKER`` or a mandate's own instance ``AI_MANDATE_<id>`` (the close
targets that owner, so the bridge books it on the owner's position row). CLOSE_POSITION flattens the account's whole instrument, so a
shared instrument only gets an alert. A rejected close is retried; alerts are debounced.

Model-proposed exit stops (``exit_decisions.target_stop_price``) are recommendations for the
operator and never trigger an automatic close.

Exit debates (paid) run only on events: the loss first crossing ``DEBATE_DRAWDOWN_PCT`` (once per
drawdown episode, re-armed after the loss recovers to half that), or a funding spike against the
position; debounced per instrument.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass

import asyncpg
import redis.asyncio as redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("exit_monitor")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://cryptonite_api:api_dev_pass@127.0.0.1:5432/cryptonite")
PAPER_ACCOUNT_ID = "PAPER-ACCOUNT-001"
AI_INSTANCE = "AI_DEBATE_WORKER"
HEARTBEAT_KEY = "exit_monitor:heartbeat"
QUOTE_STREAM = "cryptonite:stream:ui_quote_tick"

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


@dataclass(frozen=True)
class Position:
    instrument_id: str
    side: str  # LONG | SHORT
    entry_price: float
    current_price: float  # live exit price: bid for longs, ask for shorts
    ai_owned: bool  # every open row on the instrument (either side) was opened by the AI
    opened_ts: int = 0  # identity of the position (earliest open row), for per-position state
    funding_rate: float | None = None
    owner_instance: str = AI_INSTANCE  # the one AI instance (legacy or a mandate's) that owns every row

    @property
    def pnl_pct(self) -> float:
        if not self.entry_price:
            return 0.0
        move = (self.current_price - self.entry_price) / self.entry_price * 100
        return move if self.side == "LONG" else -move

    @property
    def key(self) -> str:
        return f"{self.instrument_id}:{self.side}:{self.opened_ts}"


@dataclass(frozen=True)
class ExitAction:
    kind: str  # CLOSE | EXIT_DEBATE | ALERT
    reason: str


def evaluate_exit(pos: Position, best_price: float, debate_armed: bool, in_drawdown_episode: bool = False) -> list[ExitAction]:
    """Pure exit rules. ``best_price``: most favourable price since the position opened.
    ``debate_armed``: no exit debate within the debounce window. ``in_drawdown_episode``: a drawdown
    debate already ran and the loss has not recovered since."""
    long = pos.side == "LONG"
    stop_reason = None
    if pos.pnl_pct <= -STOP_LOSS_PCT:
        stop_reason = "HARD_STOP"
    elif pos.entry_price and best_price:
        best_gain = (best_price - pos.entry_price) / pos.entry_price * 100 * (1 if long else -1)
        give_back = (best_price - pos.current_price) / best_price * 100 * (1 if long else -1)
        if best_gain >= TRAIL_ACTIVATE_PCT and give_back >= TRAIL_PCT:
            stop_reason = "TRAILING_STOP"
    if stop_reason:
        return [ExitAction("CLOSE", stop_reason)] if pos.ai_owned else [ExitAction("ALERT", stop_reason)]

    funding_against = pos.funding_rate is not None and (pos.funding_rate if long else -pos.funding_rate) >= FUNDING_SPIKE
    drawdown = pos.pnl_pct <= -DEBATE_DRAWDOWN_PCT and not in_drawdown_episode
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


async def load_positions(conn, redis_client, quotes: QuoteBook) -> list[Position]:
    rows = await conn.fetch(
        """
        WITH open_rows AS (
            SELECT * FROM paper_positions WHERE account_id = $1 AND status = 'OPEN' AND quantity > 0
        ), owners AS (  -- CLOSE_POSITION flattens the whole instrument: ownership spans both sides.
            -- AI-owned: one owner, the legacy AI instance or a mandate's own instance (A5.2)
            SELECT instrument_id,
                   BOOL_AND(instance_id = $2 OR instance_id LIKE 'AI!_MANDATE!_%' ESCAPE '!') AND COUNT(DISTINCT instance_id) = 1 AS ai_owned,
                   MIN(instance_id) AS owner
            FROM open_rows GROUP BY instrument_id
        )
        SELECT r.instrument_id, r.side,
               SUM(r.quantity * r.entry_price) / NULLIF(SUM(r.quantity), 0) AS entry_price,
               MIN(r.opened_at) AS opened_at, o.ai_owned, o.owner
        FROM open_rows r JOIN owners o USING (instrument_id)
        GROUP BY r.instrument_id, r.side, o.ai_owned, o.owner
        """,
        PAPER_ACCOUNT_ID, AI_INSTANCE,
    )
    positions = []
    for r in rows:
        price = quotes.exit_price(r["instrument_id"], r["side"])
        if price is None:
            # A stop monitor without prices must fail loudly, not silently skip
            logger.warning("No fresh quote for %s: stops are not being evaluated", r["instrument_id"])
            if await redis_client.set(f"exit:alert:{r['instrument_id']}:NO_QUOTE", "1", nx=True, ex=ALERT_DEBOUNCE_S):
                await redis_client.publish("channel:system_alerts", json.dumps({
                    "type": "EXIT_MONITOR", "severity": "CRITICAL", "instrument_id": r["instrument_id"],
                    "action": "ALERT", "reason": "NO_FRESH_QUOTE",
                }))
            continue
        funding = None
        sym = r["instrument_id"].split(".")[0].replace("-PERP", "").upper()
        try:
            if raw := await redis_client.get(f"perp:mark:{sym}"):
                funding = float(json.loads(raw).get("funding_rate"))
        except Exception:
            pass
        positions.append(Position(
            r["instrument_id"], r["side"], float(r["entry_price"] or 0), price, bool(r["ai_owned"]),
            int(r["opened_at"].timestamp()) if r["opened_at"] else 0, funding,
            r["owner"] if r["ai_owned"] else AI_INSTANCE,
        ))
    return positions


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
        uuid.uuid4(), f"{prefix}:{attempts}", pos.owner_instance,
        json.dumps({"instrument_id": pos.instrument_id, "reason": reason}), PAPER_ACCOUNT_ID,
    )
    return True


async def act(conn, redis_client, pos: Position, action: ExitAction) -> None:
    if action.kind == "CLOSE":
        if await enqueue_close(conn, pos, action.reason):
            logger.warning("%s: closing %s %s (pnl %.2f%%)", action.reason, pos.side, pos.instrument_id, pos.pnl_pct)
    elif action.kind == "EXIT_DEBATE":
        run_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at) VALUES ($1, $2, 'RUNNING', $3, now())",
            run_id, pos.instrument_id, f"exit_monitor:{action.reason}",
        )
        await redis_client.xadd("stream:exit_debate_queue", {"instrument_id": pos.instrument_id, "run_id": str(run_id)})
        await redis_client.set(f"debounce:exit:{pos.instrument_id}", "1", ex=DEBATE_DEBOUNCE_S)
        logger.info("Exit debate for %s (%s, pnl %.2f%%)", pos.instrument_id, action.reason, pos.pnl_pct)
    if await redis_client.set(f"exit:alert:{pos.key}:{action.kind}:{action.reason}", "1", nx=True, ex=ALERT_DEBOUNCE_S):
        await redis_client.publish("channel:system_alerts", json.dumps({
            "type": "EXIT_MONITOR", "severity": "INFO" if action.kind == "EXIT_DEBATE" else "CRITICAL",
            "instrument_id": pos.instrument_id, "action": action.kind, "reason": action.reason,
            "pnl_pct": round(pos.pnl_pct, 2), "ai_owned": pos.ai_owned,
        }))


async def monitor_once(pool, redis_client, quotes: QuoteBook) -> None:
    async with pool.acquire() as conn:
        for pos in await load_positions(conn, redis_client, quotes):
            best = await best_price_since_open(redis_client, pos)
            episode_key = f"exit:dd_episode:{pos.key}"
            if pos.pnl_pct > -DEBATE_DRAWDOWN_PCT * REARM_FRACTION:
                await redis_client.delete(episode_key)
            armed = not await redis_client.exists(f"debounce:exit:{pos.instrument_id}")
            in_episode = bool(await redis_client.exists(episode_key))
            for action in evaluate_exit(pos, best, armed, in_episode):
                if action.reason == "DRAWDOWN":
                    await redis_client.set(episode_key, "1", ex=30 * 86400)
                await act(conn, redis_client, pos, action)


async def monitor_loop():
    logger.info("Starting exit monitor (stop %.1f%%, trail %.1f%% after +%.1f%%)", STOP_LOSS_PCT, TRAIL_PCT, TRAIL_ACTIVATE_PCT)
    redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=30)  # redis-py 8 times a blocking read out at exactly its block time (LE-3)
    pool = await asyncpg.create_pool(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"), min_size=1, max_size=2)
    quotes = QuoteBook()
    follower = asyncio.create_task(quotes.follow(redis_client))
    while True:
        if follower.done():  # supervise the quote feed: never run stops on a dead feed
            logger.error("Quote follower stopped (%s); restarting", follower.exception() if not follower.cancelled() else "cancelled")
            follower = asyncio.create_task(quotes.follow(redis_client))
        try:
            await monitor_once(pool, redis_client, quotes)
            # Brain order approvals are refused unless this is fresh: AI-owned positions rely on it for stops
            await redis_client.set(HEARTBEAT_KEY, json.dumps({
                "stop_loss_pct": STOP_LOSS_PCT, "trail_activate_pct": TRAIL_ACTIVATE_PCT, "trail_pct": TRAIL_PCT,
            }), ex=int(max(90, 3 * INTERVAL_S)))
        except Exception as e:
            logger.error(f"Error in exit monitor loop: {e}")
        await asyncio.sleep(INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(monitor_loop())
