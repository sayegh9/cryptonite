"""The exit monitor's pure exit rules, and a bar-by-bar replay of them (H11 live-parity labels).

``evaluate_exit`` and its helpers are what ``services/scanner/cryptonite_scanner_service/exit_monitor.py`` applies to
every open paper position each cycle (it re-exports them). ``simulate_live_exit`` walks ordered bars through the
same ``evaluate_exit`` so the candidate labeller and the preset replay close a trade the way the live monitor would:

- the fill is the open of the first bar (the bar after the signal);
- the structural stop (the entry's own stop; the monitor uses the tightest same-side entry stop, #147) closes the
  whole remainder; R is the fill-to-structural-stop distance;
- TP1 (``apply_profile_exit_levels``: the profile's ``exit_tp_r`` times R, else the entry's target) scales out
  ``exit_tp1_scale_pct`` (default 50%) once; the remainder runs on;
- the trailing stop (profile trail values, else the global ones), the hard stop (``stop_loss_pct`` from entry,
  the backstop) and the profile's max hold (closed at the close of the bar that reaches it);
- inside a bar the price is assumed to go open -> adverse extreme -> favourable extreme -> close, so a bar that
  touches both a stop and TP1 is a stop first (the same-bar stop-first rule). A stop is filled at its level, or at
  the open when the bar opens beyond it (a gap).

Exit debates (paid, event driven) and margin / liquidation proximity are not replayed: a replayed position has no
debate and no leverage. Pure: imports only this package.
"""

import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from cryptonite_schemas.exit_thresholds import ExitThresholds

AI_INSTANCE = "AI_DEBATE_WORKER"
LIQ_ALERT_BUFFER_USED = 0.5  # stop-to-liquidation buffer used: CRITICAL alert
LIQ_CLOSE_BUFFER_USED = 0.75  # ... and a reduce-only close of an AI-owned perp (backup to the resting stop)
LIVE_EXIT_MODEL_VERSION = "live-v1"
# The exit monitor's code defaults (EXIT_* env unset, nothing stored): what a replay applies by default.
DEFAULT_THRESHOLDS = ExitThresholds(stop_loss_pct=5.0, trail_activate_pct=3.0, trail_pct=2.0,
                                    debate_drawdown_pct=2.0, funding_spike=0.0005)
_EPS = 1e-12


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
    margin_level: float | None = None
    margin_call_level: float | None = None
    margin_liquidation_level: float | None = None
    profile_tp_r: Decimal | None = None
    profile_tp1_scale_pct: Decimal | None = None
    profile_trail_activate_pct: Decimal | None = None
    profile_trail_pct: Decimal | None = None
    profile_max_hold_hours: int | None = None

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


@dataclass(frozen=True)
class EnforcedTp1:
    """The TP1 the exit monitor enforces: ``price`` (None: no TP1), the share it scales out and where it comes from:
    PROFILE_R (entry +/- ``tp_r`` x the entry-to-structural-stop distance) or ENTRY_TARGET (the entry's own target)."""

    price: float | None
    scale_pct: float
    basis: str  # PROFILE_R | ENTRY_TARGET | NONE
    tp_r: float | None = None

    def text(self, card_target: float | None = None, *, from_fill: bool = True) -> str:
        """One line for cards and system messages, e.g. "TP1 (50% at 2R): 76100.18 from the fill; card target
        76388.88 is informational"."""
        if self.price is None and self.basis == "PROFILE_R":
            tail = f"; card target {card_target:.10g} is informational" if card_target is not None else ""
            return (f"TP1 ({self.scale_pct:g}% at {self.tp_r:g}R): set from the fill and the structural stop when the "
                    f"order fills{tail}")
        if self.price is None:
            return "No TP1 is enforced (no usable stop or target)."
        where = "from the fill" if from_fill else "estimated from the entry price; recomputed from the fill"
        if self.basis == "PROFILE_R":
            head = f"TP1 ({self.scale_pct:g}% at {self.tp_r:g}R): {self.price:.10g} {where}"
            same = card_target is not None and abs(card_target - self.price) <= abs(self.price) * 1e-9
            tail = "" if card_target is None or same else f"; card target {card_target:.10g} is informational"
            return head + tail
        return f"TP1 ({self.scale_pct:g}% at the card target): {self.price:.10g}"


def enforced_tp1(side: str, entry: float | None, stop: float | None, target: float | None,
                 tp_r: float | Decimal | None = None, scale_pct: float | Decimal | None = None) -> EnforcedTp1:
    """The one TP1 rule (ADR 0011 D13), for the exit monitor and every display: with a profile R and a stop on the loss
    side of ``entry`` it is entry +/- tp_r x |entry - stop|; otherwise the entry's own target. Scale: the profile's
    ``exit_tp1_scale_pct``, else 50%."""
    scale = float(scale_pct) if scale_pct is not None else 50.0
    long = (side or "LONG").upper() in ("LONG", "BUY")
    if tp_r is not None and entry and stop and entry > 0 and (stop < entry if long else stop > entry):
        risk = abs(entry - stop)
        return EnforcedTp1(entry + risk * float(tp_r) * (1 if long else -1), scale, "PROFILE_R", float(tp_r))
    if tp_r is not None and stop and not entry:
        # no entry price yet (a market card): the monitor still sets TP1 from the fill, never the card target
        return EnforcedTp1(None, scale, "PROFILE_R", float(tp_r))
    if target:
        return EnforcedTp1(float(target), scale, "ENTRY_TARGET")
    return EnforcedTp1(None, scale, "NONE")


def apply_profile_exit_levels(pos: Position) -> Position:
    """Set profile TP1 from its structural stop, preserving legacy behavior without a usable profile R (ADR 0011 D13).
    The level is ``enforced_tp1``'s, the same number the Brain card and its messages show."""
    if not pos.ai_owned or pos.profile_tp_r is None or pos.stop_price is None or pos.entry_price <= 0:
        return pos
    tp1 = enforced_tp1(pos.side, pos.entry_price, pos.stop_price, None, pos.profile_tp_r, pos.profile_tp1_scale_pct)
    if tp1.basis != "PROFILE_R":
        return pos
    return replace(pos, take_profit_price=tp1.price)


def position_thresholds(pos: Position, global_thresholds: ExitThresholds) -> ExitThresholds:
    """Apply per-profile trail values, falling back field-by-field to global thresholds (ADR 0011 D13)."""
    activation = pos.profile_trail_activate_pct
    trail = pos.profile_trail_pct
    if activation is None and trail is None:
        return global_thresholds
    return replace(
        global_thresholds,
        trail_activate_pct=float(activation) if activation is not None else global_thresholds.trail_activate_pct,
        trail_pct=float(trail) if trail is not None else global_thresholds.trail_pct,
    )


def max_hold_action(pos: Position, now_ts: float | None = None) -> ExitAction | None:
    """Return the profile's reduce-only max-hold close when its holding deadline has passed (ADR 0011 D13)."""
    if not pos.ai_owned or pos.profile_max_hold_hours is None or pos.opened_ts <= 0:
        return None
    now_ts = time.time() if now_ts is None else now_ts
    if now_ts - pos.opened_ts >= pos.profile_max_hold_hours * 3600:
        return ExitAction("CLOSE", "MAX_HOLD")
    return None


def tp1_reduce_quantity(pos: Position) -> str:
    """Calculate a TP1 scale-out quantity using the profile percentage or the legacy 50% (ADR 0011 D13)."""
    pct = pos.profile_tp1_scale_pct if pos.profile_tp1_scale_pct is not None else Decimal("50")
    return format((Decimal(str(pos.quantity)) * Decimal(str(pct)) / Decimal("100")).normalize(), "f")


def evaluate_exit(pos: Position, best_price: float, debate_armed: bool, in_drawdown_episode: bool,
                  t: ExitThresholds, tp1_done: bool = False) -> list[ExitAction]:
    """Pure exit rules. ``best_price``: most favourable price since the position opened.
    ``debate_armed``: no exit debate within the debounce window. ``in_drawdown_episode``: a drawdown
    debate already ran and the loss has not recovered since. ``t``: the thresholds to apply (default: the
    exit monitor passes the set in force; ``exit_monitor.evaluate_exit`` defaults it)."""
    long = pos.side == "LONG"
    if (pos.margin_level is not None and pos.margin_call_level is not None
            and pos.margin_liquidation_level is not None):
        close_at = pos.margin_liquidation_level + (pos.margin_call_level - pos.margin_liquidation_level) * 0.5
        if pos.margin_level <= close_at:
            return ([ExitAction("CLOSE", "MARGIN_LEVEL_PROXIMITY")] if pos.ai_owned
                    else [ExitAction("ALERT", "MARGIN_LEVEL_PROXIMITY")])
        if pos.margin_level <= pos.margin_call_level:
            return [ExitAction("ALERT", "MARGIN_CALL")]
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


@dataclass(frozen=True)
class LiveExitSettings:
    """A profile's exit columns (``ai_mandates.exit_*``); None falls back as the monitor does."""

    tp_r: float | None = None
    tp1_scale_pct: float | None = None
    trail_activate_pct: float | None = None
    trail_pct: float | None = None
    max_hold_hours: float | None = None

    def model_name(self, t: ExitThresholds) -> str:
        """The label written next to a live-parity R, so a row says which exits made it."""
        p = position_thresholds(self.position("X", "LONG", 1.0, None, None), t)

        def g(v: float | None) -> str:
            return "-" if v is None else f"{float(v):g}"

        return (f"{LIVE_EXIT_MODEL_VERSION} tp{g(self.tp_r)}r/{g(self.tp1_scale_pct if self.tp1_scale_pct is not None else 50)}%"
                f" trail{g(p.trail_activate_pct)}/{g(p.trail_pct)} hard{g(t.stop_loss_pct)} hold{g(self.max_hold_hours)}")

    def position(self, instrument_id: str, side: str, entry: float, stop: float | None,
                 target: float | None) -> Position:
        """An AI-owned one-unit position with these exits, TP1 set as ``load_positions`` sets it."""
        def d(v: float | None) -> Decimal | None:
            return None if v is None else Decimal(str(v))

        pos = Position(instrument_id, side, entry, entry, True, opened_ts=1, stop_price=stop,
                       take_profit_price=target, quantity=1.0, profile_tp_r=d(self.tp_r),
                       profile_tp1_scale_pct=d(self.tp1_scale_pct), profile_trail_activate_pct=d(self.trail_activate_pct),
                       profile_trail_pct=d(self.trail_pct),
                       profile_max_hold_hours=None if self.max_hold_hours is None else int(self.max_hold_hours))
        return apply_profile_exit_levels(pos)


@dataclass
class LiveExit:
    """The replayed trade: ``fills`` are (fraction of the position, price, bars held), oldest first."""

    outcome: str  # STRUCTURAL_STOP | HARD_STOP | TRAILING_STOP | MAX_HOLD | TP1_FULL | EXPIRED | INVALID | NO_DATA
    entry: float | None = None
    fills: list[tuple[float, float, int]] = field(default_factory=list)
    tp1_bars: int | None = None

    @property
    def bars_to_exit(self) -> int | None:
        return self.fills[-1][2] if self.fills else None

    @property
    def exit_price(self) -> float | None:
        return self.fills[-1][1] if self.fills else None

    def gross_r(self, direction: str, stop: float) -> float | None:
        if self.entry is None or not self.fills or self.entry == stop:
            return None
        sign = 1.0 if direction == "LONG" else -1.0
        return sum(f * sign * (p - self.entry) for f, p, _ in self.fills) / abs(self.entry - stop)

    def held_bars(self) -> float:
        """Quantity-weighted bars held (carry such as funding accrues on what is still open)."""
        return sum(f * n for f, _, n in self.fills)


def _stop_levels(pos: Position, best: float, t: ExitThresholds) -> list[tuple[float, str]]:
    """The price levels at which ``evaluate_exit`` closes ``pos`` given ``best`` (the same three rules)."""
    long = pos.side == "LONG"
    sign = 1.0 if long else -1.0
    levels = [(pos.entry_price * (1 - sign * t.stop_loss_pct / 100), "HARD_STOP")]
    if pos.stop_price is not None and pos.stop_price > 0 and (
            pos.stop_price < pos.entry_price if long else pos.stop_price > pos.entry_price):
        levels.append((pos.stop_price, "STRUCTURAL_STOP"))
    best_gain = (best - pos.entry_price) / pos.entry_price * 100 * sign
    if best_gain >= t.trail_activate_pct:
        levels.append((best * (1 - sign * t.trail_pct / 100), "TRAILING_STOP"))
    return levels


def simulate_live_exit(bars: list[dict[str, Any]], direction: str, stop: float | None, target: float | None, *,
                       settings: LiveExitSettings | None = None, thresholds: ExitThresholds | None = None,
                       bar_seconds: float = 60.0, instrument_id: str = "REPLAY.BINANCE") -> LiveExit:
    """Replay the exit monitor over ordered ``bars`` (open / high / low / close; the first open is the fill).
    ``evaluate_exit`` decides every close and TP1; ``max_hold_action`` the max hold. Bars run out: EXPIRED at the
    last close. No structural stop on the loss side of the fill: INVALID (R is undefined)."""
    if not bars:
        return LiveExit("NO_DATA")
    settings = settings or LiveExitSettings()
    global_t = thresholds or DEFAULT_THRESHOLDS
    long = (direction or "LONG").upper() != "SHORT"
    side = "LONG" if long else "SHORT"
    entry = float(bars[0]["open"])
    if stop is None or entry <= 0 or not (stop < entry if long else stop > entry):
        return LiveExit("INVALID", entry)
    pos = settings.position(instrument_id, side, entry, stop, target)
    t = position_thresholds(pos, global_t)
    tp = pos.take_profit_price if pos.take_profit_price and (
        pos.take_profit_price > entry if long else pos.take_profit_price < entry) else None
    out = LiveExit("EXPIRED", entry)
    remaining, best, tp1_done, prev = 1.0, entry, False, entry
    for n, b in enumerate(bars, start=1):
        o, hi, lo, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        for k, p in enumerate((o, lo, hi, c) if long else (o, hi, lo, c)):
            best = max(best, p) if long else min(best, p)
            levels = _stop_levels(pos, best, t)
            nearest = max(lv for lv, _ in levels) if long else min(lv for lv, _ in levels)
            may_stop = p <= nearest * (1 + _EPS) if long else p >= nearest * (1 - _EPS)
            may_tp = not tp1_done and tp is not None and (p >= tp * (1 - _EPS) if long else p <= tp * (1 + _EPS))
            if not (may_stop or may_tp):
                prev = p
                continue
            actions = evaluate_exit(replace(pos, current_price=p), best, False, True, t, tp1_done=tp1_done)
            close = next((a for a in actions if a.kind == "CLOSE"), None)
            if close is not None:
                crossed = [(lv, r) for lv, r in levels if (p <= lv if long else p >= lv)] or [(p, close.reason)]
                level, reason = max(crossed) if long else min(crossed)
                # the path reaches the nearest crossed level first; a bar opening beyond it fills at the open, and
                # there the monitor sees the gapped price at once, so its own reason (HARD_STOP first) stands
                if k == 0:
                    price, reason = p, close.reason
                else:
                    price = min(prev, max(p, level)) if long else max(prev, min(p, level))
                out.outcome = reason
                out.fills.append((remaining, price, n))
                return out
            if any(a.kind == "REDUCE_TP1" for a in actions):
                frac = float(Decimal(tp1_reduce_quantity(pos)))
                price = p if k == 0 else (min(p, max(prev, tp)) if long else max(p, min(prev, tp)))
                out.fills.append((frac, price, n))
                out.tp1_bars = n
                remaining -= frac
                tp1_done = True
                if remaining <= 1e-12:  # a 100% scale-out closes the trade there
                    out.outcome = "TP1_FULL"
                    return out
            prev = p
        if max_hold_action(pos, now_ts=pos.opened_ts + n * bar_seconds) is not None:
            out.outcome = "MAX_HOLD"
            out.fills.append((remaining, c, n))
            return out
    out.fills.append((remaining, float(bars[-1]["close"]), len(bars)))
    return out
