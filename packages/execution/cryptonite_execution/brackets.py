"""Live USD-M leverage brackets (leverage plan 1.2).

Binance publishes each symbol's leverage and maintenance-margin tiers at the signed
``GET /fapi/v1/leverageBracket``. Paper trades on mainnet prices, so the tables come from mainnet with a
read-only key (testnet tables can differ):

- ``refresh_brackets`` (the derivatives feed, daily): fetch every symbol's table and cache it in Redis at
  ``perp:brackets:{SYMBOL}`` with the fetch time.
- ``load_brackets`` (orchestrator, API): install the cached tables into ``margin`` so every margin and
  liquidation calculation uses them. ``margin.bracket_source`` then tells an entry whether its table is live and
  fresh, the BTC/ETH reference snapshot, or missing (``NO_LEVERAGE_BRACKETS``); a table older than
  ``margin.BRACKETS_MAX_AGE_S`` refuses new entries (``STALE_LEVERAGE_BRACKETS``).

Env (derivatives feed): ``BINANCE_FUTURES_READONLY_API_KEY`` and ``BINANCE_FUTURES_READONLY_PRIVATE_KEY_PATH``
(an Ed25519 PEM, like the DEMO executor's). Without them nothing is fetched: BTCUSDT and ETHUSDT use the
reference snapshot and every other symbol refuses perp entries.
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from cryptonite_execution.margin import MaintenanceBracket, set_live_brackets

logger = logging.getLogger("cryptonite.execution.brackets")

BRACKETS_KEY = "perp:brackets:{symbol}"
BRACKETS_KEY_PATTERN = "perp:brackets:*"
BRACKETS_REFRESH_INTERVAL_S = 24 * 3600
BRACKETS_KEY_TTL_S = 7 * 24 * 3600  # an old table stays visible (and is refused as stale) rather than vanishing
READONLY_KEY_ENV = "BINANCE_FUTURES_READONLY_API_KEY"
READONLY_PEM_ENV = "BINANCE_FUTURES_READONLY_PRIVATE_KEY_PATH"


def parse_symbol_brackets(entry: dict[str, Any]) -> tuple[str, tuple[MaintenanceBracket, ...]]:
    """One ``leverageBracket`` element -> (symbol, brackets sorted by floor). Binance's own ``cum`` is kept."""
    symbol = str(entry["symbol"]).upper()
    rows = sorted(entry["brackets"], key=lambda b: Decimal(str(b["notionalFloor"])))
    brackets = tuple(
        MaintenanceBracket(
            notional_floor=Decimal(str(b["notionalFloor"])),
            notional_cap=Decimal(str(b["notionalCap"])),
            max_leverage=int(b["initialLeverage"]),
            mmr=Decimal(str(b["maintMarginRatio"])),
            cum=Decimal(str(b.get("cum", 0))),
        )
        for b in rows
    )
    if not brackets:
        raise ValueError(f"no brackets for {symbol}")
    for prev, nxt in zip(brackets, brackets[1:], strict=False):
        if prev.notional_cap != nxt.notional_floor:
            raise ValueError(f"{symbol}: bracket gap between {prev.notional_cap} and {nxt.notional_floor}")
    if brackets[0].notional_floor != 0 or any(b.max_leverage < 1 or b.mmr <= 0 for b in brackets):
        raise ValueError(f"{symbol}: malformed bracket table")
    return symbol, brackets


def parse_leverage_brackets(payload: Any) -> dict[str, tuple[MaintenanceBracket, ...]]:
    """The whole response (a list, or one object when asked for one symbol). A malformed symbol is skipped."""
    entries = payload if isinstance(payload, list) else [payload]
    out: dict[str, tuple[MaintenanceBracket, ...]] = {}
    for entry in entries:
        try:
            symbol, brackets = parse_symbol_brackets(entry)
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            logger.warning("Skipping malformed leverage bracket entry %r: %s", (entry or {}).get("symbol"), e)
            continue
        out[symbol] = brackets
    return out


def brackets_to_json(symbol: str, brackets: tuple[MaintenanceBracket, ...], fetched_at_ms: int) -> str:
    return json.dumps({
        "symbol": symbol,
        "fetched_at_ms": fetched_at_ms,
        "brackets": [
            {"floor": str(b.notional_floor), "cap": str(b.notional_cap) if b.notional_cap is not None else None,
             "max_leverage": b.max_leverage, "mmr": str(b.mmr), "cum": str(b.cum)}
            for b in brackets
        ],
    })


def brackets_from_json(raw: str | bytes) -> tuple[str, tuple[MaintenanceBracket, ...], int]:
    data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    brackets = tuple(
        MaintenanceBracket(Decimal(b["floor"]), Decimal(b["cap"]) if b["cap"] is not None else None,
                           int(b["max_leverage"]), Decimal(b["mmr"]), Decimal(b["cum"]))
        for b in data["brackets"]
    )
    return str(data["symbol"]).upper(), brackets, int(data["fetched_at_ms"])


async def refresh_brackets(fetch: Callable[[], Awaitable[Any]], redis, now_ms: int | None = None) -> int:
    """Fetch every symbol's table (``fetch`` is e.g. ``BinanceFuturesAdapter.get_leverage_brackets``) and cache
    each in Redis with the fetch time. Returns the number of symbols cached; a failed fetch caches nothing, so
    the previous tables stay (and age into stale)."""
    payload = await fetch()
    parsed = parse_leverage_brackets(payload)
    fetched_at_ms = int(time.time() * 1000) if now_ms is None else now_ms
    for symbol, brackets in parsed.items():
        await redis.set(BRACKETS_KEY.format(symbol=symbol), brackets_to_json(symbol, brackets, fetched_at_ms),
                        ex=BRACKETS_KEY_TTL_S)
    logger.info("Cached leverage brackets for %d symbols", len(parsed))
    return len(parsed)


async def load_brackets(redis) -> int:
    """Install every cached table into ``margin`` (the newest one per symbol wins). Returns the count."""
    loaded = 0
    async for key in redis.scan_iter(match=BRACKETS_KEY_PATTERN):
        raw = await redis.get(key)
        if not raw:
            continue
        try:
            symbol, brackets, fetched_at_ms = brackets_from_json(raw)
            set_live_brackets(symbol, brackets, fetched_at_ms * 1_000_000)
            loaded += 1
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            logger.warning("Ignoring malformed cached brackets at %r: %s", key, e)
    return loaded
