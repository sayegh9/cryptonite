"""The Brain's ranked trade ideas.

Asked "what should I trade?", the Brain must always answer with concrete ideas, and it must not let "can this be
executed right now?" decide "is there an idea worth showing?". This module keeps the two apart:

- ``ideas_for_frame`` is pure price action on closed bars: a trend continuation, a range reversion or a range break,
  each with an entry, a structural stop, a target and what invalidates it. Every level is computed here from the bars,
  so the model never supplies one.
- ``executability`` says, for one idea, whether nothing known blocks an entry (POTENTIAL: the card's dry run still
  has to pass, so it is never called executable), what blocks it (BLOCKED,
  with every reason: a profile paused for stale data is named as temporary, never as "no mandate"), or that the
  profiles could not be read (UNKNOWN). An ON_BREAK idea is information only until its trigger has traded: it
  never gets an order card (``card_possible`` false, ``trigger`` WAITING).

An idea is a research observation, not a recommendation with a measured edge: nothing here grades, sizes or places an
order. A card for an idea still goes through ``propose_order``, the card's dry run and every gate behind it.
"""

import asyncio
import datetime
import json
import logging
from typing import Any

from cryptonite_agents.tools import (
    SCAN_MIN_RR,
    SCAN_TIMEFRAMES,
    _fed_symbols,
    _readonly_rows,
    _setup_route,
    _strip_venue_preserve_quote,
    recent_bars,
)
from cryptonite_schemas.exit_rules import DEFAULT_THRESHOLDS
from cryptonite_schemas.profile import underlying_spot
from cryptonite_schemas.reward_risk import meets_min_rr

logger = logging.getLogger(__name__)

IDEA_BARS = 60  # closed bars read per coin and timeframe
MIN_BARS = 30  # fewer readable bars: no idea from that frame
ATR_BARS = 14
SWING_BARS = 10  # the recent swing a continuation's stop sits beyond
RANGE_BARS = 40  # the window a range is measured over
TRIGGER_BARS = 5  # a continuation triggers on a break of this many bars' extreme
STOP_ATR_BUFFER = 0.25  # the stop sits this many ATRs beyond the structure
TREND_MIN_ATR_GAP = 0.5  # the 20 and 50 bar averages closer than this many ATRs: a range
TARGET_R = 2.0  # a continuation or break targets 2R, the exit monitor's TP1 distance
RANGE_EDGE = 0.35  # the lower/upper share of a range where a reversion is offered
STALE_BAR_MULTIPLE = 2.0  # the newest closed bar older than this many bar lengths: the idea's data is stale
TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240}
TF_WEIGHT = {"15m": 0.0, "1h": 3.0, "4h": 5.0}
MAX_IDEAS = 10
DEFAULT_IDEAS = 3
HARD_STOP_PCT = DEFAULT_THRESHOLDS.stop_loss_pct  # the exit monitor's default hard stop, percent from entry
STALE_PAUSE_PREFIX = "BREAKER_STALE_DATA"


def _ema(values: list[float], n: int) -> float:
    k = 2.0 / (n + 1)
    ema = sum(values[:n]) / n
    for v in values[n:]:
        ema = v * k + ema * (1 - k)
    return ema


def _atr(bars: list[dict[str, Any]], n: int = ATR_BARS) -> float:
    trs = [max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"]))
           for a, b in zip(bars[:-1], bars[1:], strict=True)]
    tail = trs[-n:]
    return sum(tail) / len(tail) if tail else 0.0


def _precision(price: float) -> int:
    return 6 if price < 0.1 else (4 if price < 10 else 2)


def frame_trend(bars: list[dict[str, Any]]) -> str:
    """UP, DOWN or RANGE from the close against its 20 and 50 bar averages. The averages must stand at least
    TREND_MIN_ATR_GAP ATRs apart: a swing to the edge of a range is not a trend."""
    try:
        rows = [{k: float(b[k]) for k in ("high", "low", "close")} for b in bars]
    except (KeyError, TypeError, ValueError):
        return "UNKNOWN"
    if len(rows) < MIN_BARS:
        return "UNKNOWN"
    closes = [b["close"] for b in rows]
    fast, slow = _ema(closes, 20), _ema(closes, min(50, len(closes)))
    if abs(fast - slow) < TREND_MIN_ATR_GAP * _atr(rows):
        return "RANGE"
    if closes[-1] > fast > slow:
        return "UP"
    if closes[-1] < fast < slow:
        return "DOWN"
    return "RANGE"


def _idea(inst: str, tf: str, payload: dict[str, Any], *, kind: str, direction: str, entry_type: str, entry: float,
          stop: float, target: float, basis: str, invalidation: str, trend: str) -> dict[str, Any] | None:
    prec = _precision(entry)
    entry, stop, target = round(entry, prec), round(stop, prec), round(target, prec)
    risk, reward = abs(entry - stop), abs(target - entry)
    long = direction == "LONG"
    if min(entry, stop, target) <= 0 or risk <= 0 or reward <= 0 or (stop < entry) != long or (target > entry) != long:
        return None  # the bars gave no valid geometry: no idea, never a patched-up one
    return {"instrument_id": inst, "coin": inst.split(".")[0], "timeframe": tf, "kind": kind, "direction": direction,
            "entry_type": entry_type, "entry": entry, "stop": stop, "target": target,
            "rr_ratio": round(reward / risk, 2), "risk_pct": round(risk / entry * 100, 2), "frame_trend": trend,
            "basis": basis, "invalidation": invalidation, "bar_close": payload.get("newest_bar_close"),
            "newest_bar_age_minutes": payload.get("newest_bar_age_minutes"),
            "data_complete": bool(payload.get("complete")), "bars_used": len(payload.get("bars") or []),
            "source": "lake closed bars",
            # An ON_BREAK entry lies beyond the newest close by construction: it has not traded yet
            "trigger": "WAITING" if entry_type == "ON_BREAK" else "NONE",
            "level_sources": {
                "entry": "newest close" if entry_type == "AT_MARKET" else f"break level from the {tf} bars",
                "stop": f"{tf} swing or range level with an ATR buffer",
                "target": "planning level at a fixed multiple of the risk or the range middle, not a measured target"}}


def ideas_for_frame(instrument_id: str, timeframe: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The ideas one coin's closed bars on one timeframe support. ``payload`` is ``recent_bars``' answer. Fewer than
    MIN_BARS readable bars give no idea. Every level is measured from the bars."""
    try:
        bars = [{k: float(b[k]) for k in ("open", "high", "low", "close")} for b in payload.get("bars") or []]
    except (KeyError, TypeError, ValueError):
        return []
    if len(bars) < MIN_BARS:
        return []
    close, atr = bars[-1]["close"], _atr(bars)
    if close <= 0 or atr <= 0:
        return []
    trend = frame_trend(bars)
    buffer = STOP_ATR_BUFFER * atr
    swing_low = min(b["low"] for b in bars[-SWING_BARS:])
    swing_high = max(b["high"] for b in bars[-SWING_BARS:])
    window = bars[-RANGE_BARS:]
    range_low, range_high = min(b["low"] for b in window), max(b["high"] for b in window)
    width = range_high - range_low
    prec = _precision(close)

    def f(x: float) -> str:
        return f"{x:.{prec}f}"

    def make(**kw: Any) -> dict[str, Any] | None:
        return _idea(instrument_id, timeframe, payload, trend=trend, **kw)

    out: list[dict[str, Any] | None] = []
    if trend == "UP":
        entry = max(b["high"] for b in bars[-TRIGGER_BARS:])
        stop = swing_low - buffer
        out.append(make(
            kind="trend_continuation", direction="LONG", entry_type="ON_BREAK", entry=entry, stop=stop,
            target=entry + TARGET_R * (entry - stop),
            basis=(f"{timeframe} uptrend: close {f(close)} above its 20 and 50 bar averages; trigger is a break of the "
                   f"last {TRIGGER_BARS} bars' high {f(entry)}; stop under the {SWING_BARS}-bar swing low {f(swing_low)}"),
            invalidation=f"a {timeframe} close below {f(swing_low)} before the trigger, or the stop after it"))
    elif trend == "DOWN":
        entry = min(b["low"] for b in bars[-TRIGGER_BARS:])
        stop = swing_high + buffer
        out.append(make(
            kind="trend_continuation", direction="SHORT", entry_type="ON_BREAK", entry=entry, stop=stop,
            target=entry - TARGET_R * (stop - entry),
            basis=(f"{timeframe} downtrend: close {f(close)} below its 20 and 50 bar averages; trigger is a break of "
                   f"the last {TRIGGER_BARS} bars' low {f(entry)}; stop over the {SWING_BARS}-bar swing high "
                   f"{f(swing_high)}"),
            invalidation=f"a {timeframe} close above {f(swing_high)} before the trigger, or the stop after it"))
    elif width > 0:
        position = (close - range_low) / width
        where = f"{timeframe} range {f(range_low)} to {f(range_high)} over {len(window)} bars; close {f(close)}"
        if position <= RANGE_EDGE:
            out.append(make(
                kind="range_reversion", direction="LONG", entry_type="AT_MARKET", entry=close,
                stop=range_low - 2 * buffer, target=range_low + 0.75 * width,
                basis=f"{where} is in the lower part of the range; stop under the range low",
                invalidation=f"a {timeframe} close below the range low {f(range_low)}"))
        elif position >= 1 - RANGE_EDGE:
            out.append(make(
                kind="range_reversion", direction="SHORT", entry_type="AT_MARKET", entry=close,
                stop=range_high + 2 * buffer, target=range_high - 0.75 * width,
                basis=f"{where} is in the upper part of the range; stop over the range high",
                invalidation=f"a {timeframe} close above the range high {f(range_high)}"))
        else:  # mid-range: no edge to lean on, so the ideas are the two breaks
            long_stop, short_stop = range_high - 1.5 * atr, range_low + 1.5 * atr
            out.append(make(
                kind="range_break", direction="LONG", entry_type="ON_BREAK", entry=range_high, stop=long_stop,
                target=range_high + TARGET_R * (range_high - long_stop),
                basis=f"{where} is mid-range; trigger is a break of the range high",
                invalidation=f"a {timeframe} close back inside the range below {f(long_stop)}"))
            out.append(make(
                kind="range_break", direction="SHORT", entry_type="ON_BREAK", entry=range_low, stop=short_stop,
                target=range_low - TARGET_R * (short_stop - range_low),
                basis=f"{where} is mid-range; trigger is a break of the range low",
                invalidation=f"a {timeframe} close back inside the range above {f(short_stop)}"))
    return [i for i in out if i is not None]


def idea_score(idea: dict[str, Any], trends: dict[str, str]) -> float:
    """Ranks ideas against each other only: the higher timeframes' trend with the idea, a tradeable-now entry, the
    reward to risk and complete data. It is not a probability and not a measured edge."""
    with_trend, against = ("UP", "DOWN") if idea["direction"] == "LONG" else ("DOWN", "UP")
    score = 50.0 + TF_WEIGHT.get(idea["timeframe"], 0.0)
    for tf, gain in (("4h", 12.0), ("1h", 8.0), ("15m", 4.0)):
        if trends.get(tf) == with_trend:
            score += gain
        elif trends.get(tf) == against:
            score -= gain
    score += min(float(idea["rr_ratio"]), 3.0) * 3.0
    if idea["entry_type"] == "AT_MARKET":
        score += 4.0
    if not idea["data_complete"]:
        score -= 6.0
    if float(idea.get("risk_pct") or 0.0) > HARD_STOP_PCT:
        score -= 15.0  # the monitor's hard stop would close it before its own stop
    return round(max(0.0, min(100.0, score)), 1)


def all_scopes() -> list[dict[str, Any]] | None:
    """Every profile with its status and pause or halt reason, or None when they cannot be read."""
    try:
        return _readonly_rows(
            "SELECT name, status, paused_reason, halted_reason, instruments, markets, direction, max_leverage "
            "FROM agent_safe_mandate_health ORDER BY name, id LIMIT 50")
    except Exception as e:
        logger.warning("Could not read the profiles for trade ideas: %s", e)
        return None


def _is_stale_pause(scope: dict[str, Any]) -> bool:
    return (str(scope.get("status") or "").upper() == "PAUSED"
            and str(scope.get("paused_reason") or "").startswith(STALE_PAUSE_PREFIX))


def _reason(code: str, text: str) -> dict[str, str]:
    return {"code": code, "text": text}


def executability(idea: dict[str, Any], scopes: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Whether an entry on ``idea`` could be taken now. BLOCKED lists every reason. ``card_possible`` says whether an
    order card can be proposed for it. POTENTIAL means nothing checked here blocks it: the card's dry run and the
    submission still check the live price, size minimum, kill switch, session limits, grade and feed. An ON_BREAK
    idea never gets a card: a limit order at a break level would fill at once, before the break."""
    at_market = idea.get("entry_type") == "AT_MARKET"
    if scopes is None:
        return {"status": "UNKNOWN", "route": None, "card_possible": False,
                "reasons": [_reason("PROFILES_UNREADABLE", "the profiles could not be read, so it is unknown whether "
                                    "this can be executed: missing data, not a missing mandate")]}
    inst, side, coin = idea["instrument_id"], idea["direction"], idea["coin"]
    reasons: list[dict[str, str]] = []
    card_fixable = False
    by_status: dict[str, list[dict[str, Any]]] = {}
    for m in scopes:
        if any(underlying_spot(i) == inst for i in m.get("instruments") or []):
            by_status.setdefault(str(m.get("status") or "").upper(), []).append(m)
    route = None
    if by_status.get("ACTIVE"):
        route, why = _setup_route(inst, side, by_status["ACTIVE"])
        if route is None:
            reasons.append(_reason("SIDE_NOT_ALLOWED", why or f"no ACTIVE profile allows a {side.lower()} on {coin}"))
    elif by_status.get("PAUSED"):
        for m in by_status["PAUSED"]:
            if _is_stale_pause(m):
                reasons.append(_reason(
                    "PROFILE_PAUSED_STALE_DATA",
                    f"profile {m['name']} is temporarily paused because its data feed went stale "
                    f"({m.get('paused_reason')}); it resumes by itself once the feed is healthy"))
            else:
                reasons.append(_reason("PROFILE_PAUSED", f"profile {m['name']} is PAUSED"
                                       + (f" ({m['paused_reason']})" if m.get("paused_reason") else "")))
    elif by_status.get("HALTED"):
        reasons.extend(_reason("PROFILE_HALTED", f"profile {m['name']} is HALTED"
                               + (f" ({m['halted_reason']})" if m.get("halted_reason") else ""))
                       for m in by_status["HALTED"])
    else:
        card_fixable = side == "LONG"  # an order card can create the profile on approval; it cannot open a spot short
        reasons.append(_reason("NO_PROFILE", f"no profile covers {coin}"
                               + ("; an order card can create one on approval" if card_fixable else
                                  "; a short needs a profile with perps")))
    if not meets_min_rr(idea["rr_ratio"], SCAN_MIN_RR):
        reasons.append(_reason("RR_BELOW_MIN", f"reward is {idea['rr_ratio']} times the risk, below {SCAN_MIN_RR}"))
        card_fixable = False
    risk_pct = float(idea.get("risk_pct") or 0.0)
    if risk_pct > HARD_STOP_PCT:
        reasons.append(_reason("STOP_BEYOND_HARD_STOP",
                               f"the stop is {risk_pct}% from the entry, beyond the exit monitor's {HARD_STOP_PCT:g}% "
                               "hard stop, which would close the trade first: the stated reward to risk does not hold"))
        card_fixable = False
    age, tf_min = idea.get("newest_bar_age_minutes"), TF_MINUTES.get(idea["timeframe"], 60)
    if age is None or float(age) > STALE_BAR_MULTIPLE * tf_min:
        reasons.append(_reason("BARS_STALE", "the newest closed bar is "
                               + ("of unknown age" if age is None else f"{age} minutes old")
                               + f" on {idea['timeframe']}: the levels may be out of date"))
        card_fixable = False
    if reasons:
        return {"status": "BLOCKED", "route": None, "reasons": reasons,
                "card_possible": card_fixable and len(reasons) == 1 and at_market}
    # A Brain card opens a short on perps only; a margin short is Auto-Trade's
    card_possible = at_market and not (side == "SHORT" and route["market"] != "PERP")
    return {"status": "POTENTIAL", "route": route, "reasons": [], "card_possible": card_possible}


def _profile_summary(scopes: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if scopes is None:
        return None
    names = lambda status: sorted(m["name"] for m in scopes if str(m.get("status") or "").upper() == status)  # noqa: E731
    stale = sorted(m["name"] for m in scopes if _is_stale_pause(m))
    return {"active": names("ACTIVE"), "paused": [n for n in names("PAUSED") if n not in stale],
            "paused_stale_data": stale, "halted": names("HALTED")}


def _universe(symbol: str | None, scopes: list[dict[str, Any]] | None) -> tuple[list[str], str | None]:
    if symbol and symbol.strip():
        sym = _strip_venue_preserve_quote(symbol).split("-")[0].replace("/", "")
        return [f"{sym if sym.endswith(('USDT', 'USDC', 'FDUSD')) else sym + 'USDT'}.BINANCE"], None
    fed = _fed_symbols()
    if fed is not None:
        return sorted(f"{s}.BINANCE" for s in fed), None
    owned = sorted({underlying_spot(i) for m in scopes or [] for i in m.get("instruments") or []})
    return owned, "The live feed universe could not be read: only the coins your profiles cover were analysed."


def build_trade_ideas(symbol: str | None = None, max_results: int = DEFAULT_IDEAS) -> dict[str, Any]:
    max_results = max(1, min(int(max_results or DEFAULT_IDEAS), MAX_IDEAS))
    scopes = all_scopes()
    universe, feed_note = _universe(symbol, scopes)
    ideas: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    scanned: list[str] = []
    for inst in universe:
        frames = {tf: recent_bars(inst, tf, IDEA_BARS) for tf in SCAN_TIMEFRAMES}
        trends = {tf: frame_trend(p.get("bars") or []) for tf, p in frames.items()}
        found = [i for tf, p in frames.items() for i in ideas_for_frame(inst, tf, p)]
        if not found:
            why = next((str(p["reason"]) for p in frames.values() if p.get("reason")), "too few closed bars")
            unavailable.append({"coin": inst.split(".")[0], "reason": why})
            continue
        scanned.append(inst.split(".")[0])
        for i in found:
            ideas.append({**i, "trends": trends, "score": idea_score(i, trends), **executability(i, scopes)})
    # The best idea per coin and side, so three timeframes of one move are one idea; unblocked ideas win ties
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for i in sorted(ideas, key=lambda i: (-i["score"], i["status"] != "POTENTIAL")):
        best.setdefault((i["coin"], i["direction"]), i)
    ranked = sorted(best.values(), key=lambda i: -i["score"])[:max_results]
    for n, i in enumerate(ranked, start=1):
        i["rank"] = n

    result: dict[str, Any] = {
        "ideas": ranked, "scanned_coins": scanned, "scanned_timeframes": list(SCAN_TIMEFRAMES),
        "unavailable": unavailable, "profiles": _profile_summary(scopes),
        "as_of": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "method_note": ("Ideas are price-action observations on closed bars, ranked against each other. They are not "
                        "scanner signals and carry no measured edge. POTENTIAL means nothing checked here blocks the "
                        "idea as of as_of; it is not confirmed until an order card's dry run passes on the live price, "
                        "size minimum, grade, session limits, kill switch and feed. An ON_BREAK idea is information "
                        "only until price trades through its entry: it gets no order card before that. Targets are "
                        "planning levels (a fixed multiple of the risk, or the range middle), not measured market "
                        "targets."),
    }
    missing = [name for name, gone in (("profiles", scopes is None), ("bars", bool(unavailable))) if gone]
    if missing:
        result["missing_evidence"] = missing
    if feed_note:
        result["feed_note"] = feed_note
    notes: list[str] = []
    if not ranked:
        notes.append("No closed bars could be read for " + (", ".join(u["coin"] for u in unavailable) or "any coin")
                     + ": this is missing data, not a finding that the market has no trade.")
    stale = (result["profiles"] or {}).get("paused_stale_data") or []
    if stale:
        notes.append(f"As of {result['as_of']}, {', '.join(stale)} "
                     + ("is" if len(stale) == 1 else "are")
                     + " temporarily paused because the data feed went stale; this is not a missing mandate, and "
                     + ("it resumes by itself" if len(stale) == 1 else "they resume by themselves")
                     + " once the feed is healthy.")
    if scopes is None:
        notes.append("The profiles could not be read, so each idea's status is UNKNOWN.")
    if notes:
        result["note"] = " ".join(notes)
    return result


async def get_trade_ideas(symbol: str | None = None, max_results: int = DEFAULT_IDEAS) -> str:
    """Ranked trade ideas from closed-bar price action (15m, 1h and 4h) on every fed coin, or only `symbol`. Call this
    whenever the operator asks what to trade, for trade ideas, suggestions, setups or opportunities; it answers even
    when no scanner preset fired. Each idea has rank, coin, timeframe, direction, kind (trend_continuation,
    range_reversion, range_break), entry_type (AT_MARKET, or ON_BREAK: enter only once price trades through entry),
    entry, stop, target, rr_ratio, basis, invalidation, bar_close, `trigger` (WAITING for an ON_BREAK idea whose
    entry has not traded, NONE for AT_MARKET), and separately `status`: POTENTIAL (nothing checked here blocks it; a
    card's dry run must still pass, so never call it executable or confirmed), BLOCKED (with every reason in `reasons`, e.g. a profile temporarily paused for stale data,
    no profile, side not allowed, reward below 1.5 times the risk, stale bars) or UNKNOWN (profiles unreadable), plus
    `card_possible`. Show every idea returned, blocked ones included, with its status and reasons; never drop an idea
    because it is blocked, and never present a BLOCKED or UNKNOWN idea as possible. An idea with trigger WAITING is
    information only: no order card, market or limit, until price has traded through its entry. All levels come from this
    tool: quote them exactly and add none. Ideas are research observations with no measured edge: say so once.
    `unavailable` lists coins whose bars could not be read (missing data, not "no trade")."""
    return json.dumps(await asyncio.to_thread(build_trade_ideas, symbol, max_results), default=str)



# ---- scoring a saved idea afterwards ----------------------------------------------------------------------------

IDEA_HOLD_HOURS = {"15m": 24, "1h": 72, "4h": 168}  # the horizon candidate labels use per timeframe
IDEA_TRIGGER_BARS = 12  # an ON_BREAK idea must trigger within this many bars of its timeframe


def score_idea(idea: dict[str, Any], bars_1m: list[dict[str, Any]]) -> dict[str, Any]:
    """What a saved idea came to, replayed on the 1m bars that closed after it was shown (oldest first), with the exit
    monitor's own rules (``simulate_live_exit``) and cost model v1. An ON_BREAK idea fills at the open of the bar after
    price first trades through its entry; one whose stop trades first, or that never triggers, took no trade. A
    hypothetical outcome: never realized P&L."""
    from cryptonite_scanner.costs import cost_in_r, default_market, round_trip_cost_fraction
    from cryptonite_schemas.exit_rules import simulate_live_exit

    out: dict[str, Any] = {"outcome": "NO_DATA", "triggered": False, "entry_fill": None, "gross_r": None,
                           "net_r": None, "minutes_held": None}
    if not bars_1m:
        return out
    long = idea["direction"] == "LONG"
    entry, stop, target = float(idea["entry"]), float(idea["stop"]), float(idea["target"])
    tf_min = TF_MINUTES.get(idea["timeframe"], 60)
    start = 0
    if idea.get("entry_type") != "AT_MARKET":
        start = None
        for n, b in enumerate(bars_1m[:IDEA_TRIGGER_BARS * tf_min]):
            hi, lo = float(b["high"]), float(b["low"])
            if (lo <= stop) if long else (hi >= stop):
                return {**out, "outcome": "INVALIDATED_BEFORE_TRIGGER"}
            if (hi >= entry) if long else (lo <= entry):
                start = n + 1
                break
        if start is None:
            waited_out = len(bars_1m) >= IDEA_TRIGGER_BARS * tf_min
            return {**out, "outcome": "NOT_TRIGGERED" if waited_out else "PENDING"}
    hold_min = IDEA_HOLD_HOURS.get(idea["timeframe"], 72) * 60
    rows = bars_1m[start:start + hold_min]
    if not rows:
        return {**out, "outcome": "PENDING", "triggered": True}
    res = simulate_live_exit(rows, idea["direction"], stop, target, bar_seconds=60.0)
    out.update(triggered=True, entry_fill=res.entry, outcome=res.outcome, minutes_held=res.bars_to_exit)
    if res.outcome == "EXPIRED" and len(rows) < hold_min:
        out["outcome"] = "PENDING"  # still open: the horizon has not passed yet
        return out
    gross = res.gross_r(idea["direction"], stop) if res.fills else None
    if gross is not None:
        cost = cost_in_r(res.entry, stop, round_trip_cost_fraction(default_market(idea["direction"]),
                                                                    res.held_bars() / 60.0))
        out["gross_r"] = round(gross, 3)
        out["net_r"] = None if cost is None else round(gross - cost, 3)
    return out


from cryptonite_agents.gateway import RiskClass  # noqa: E402
from cryptonite_agents.gateway import tool as _register  # noqa: E402

# ---- the limit-order guard for order cards ----------------------------------------------------------------------

LIMIT_TOLERANCE = 0.001  # a limit within 0.1% of the market is an ordinary priced order
REFERENCE_PRICE_MAX_AGE_MIN = 5.0


def reference_price(instrument_id: str, now_ns: int | None = None) -> float | None:
    """The close of the newest 1m lake bar of ``instrument_id``, or None when it is older than
    REFERENCE_PRICE_MAX_AGE_MIN or cannot be read. Never raises."""
    import os
    import time
    from pathlib import Path

    from cryptonite_schemas.profile import underlying_spot
    from cryptonite_storage.queries import AnalyticsQueryEngine, lake_symbol

    now_ns = now_ns if now_ns is not None else time.time_ns()
    start_ns = now_ns - int(REFERENCE_PRICE_MAX_AGE_MIN * 60 * 10**9)
    try:
        lake = AnalyticsQueryEngine(Path(os.getenv("ANALYTICS_LAKE_PATH", "/app/data/analytics_lake")))
        try:
            df = lake.query_bars_df(lake_symbol(underlying_spot(instrument_id)), "1m", start_ns, now_ns)
        finally:
            lake._con.close()
        if df is None or df.is_empty():
            return None
        order = "bar_open_ns" if "bar_open_ns" in df.columns else "ts_event_ns"
        price = float(df.sort(order)["close"][-1])
        return price if price > 0 else None
    except Exception:
        return None


def limit_entry_refusal(side: str, limit_price: float, market_price: float | None) -> str | None:
    """Why an entry LIMIT order must not get a card, or None. A buy limit above the market, or a sell limit below it,
    fills at once: it is a market order, not a wait for a break. An unknown market price refuses too."""
    if market_price is None or market_price <= 0:
        return "the current price could not be read, so the limit price cannot be checked against the market"
    if side == "BUY" and limit_price > market_price * (1 + LIMIT_TOLERANCE):
        return (f"a buy limit at {limit_price} is above the market ({market_price}) and would fill at once, before "
                "any break of that level")
    if side == "SELL" and limit_price < market_price * (1 - LIMIT_TOLERANCE):
        return (f"a sell limit at {limit_price} is below the market ({market_price}) and would fill at once, before "
                "any break of that level")
    return None


TRADE_IDEAS = _register(RiskClass.READ)(get_trade_ideas)
