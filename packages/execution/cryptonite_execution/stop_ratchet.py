"""Ratchet for the resting stop of an AI-owned perp position (auto-trade plan A4).

Pure: the orchestrator's mark loop feeds it the best mark since entry, the current stop and the exit monitor's trail
settings (``exit.trail_activate_pct``, ``exit.trail_pct``), and moves the resting stop to what it returns.

- Activation: the best mark's favourable move from entry reaches ``trail_activate_pct``, or the mandate met its
  daily profit target (``target_met``), which arms it at once.
- Long: max(current stop, best x (1 - trail_pct/100)), rounded down to the tick. Short: min(current stop,
  best x (1 + trail_pct/100)), rounded up to the tick. Rounding away from the mark keeps the trail at least
  ``trail_pct`` wide.
- Never loosens; moves only by at least one tick; never at or through the mark (a stop there could not rest: the
  fire path handles a mark that reached the current stop).
"""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

HUNDRED = Decimal("100")


def favourable_move_pct(long: bool, entry: Decimal, best: Decimal) -> Decimal:
    """How far ``best`` is from ``entry`` in the position's favour, percent (negative when against it)."""
    if entry <= 0:
        return Decimal("0")
    move = (best - entry) / entry * HUNDRED
    return move if long else -move


def to_tick(price: Decimal, tick: Decimal, long: bool) -> Decimal:
    """``price`` on the tick grid, rounded away from the mark: down for a long's stop, up for a short's."""
    if tick <= 0:
        return price
    steps = (price / tick).to_integral_value(rounding=ROUND_FLOOR if long else ROUND_CEILING)
    return steps * tick


def ratcheted_stop(*, long: bool, entry: Decimal, best: Decimal, mark: Decimal, current_stop: Decimal,
                   trail_activate_pct: Decimal, trail_pct: Decimal, tick: Decimal,
                   target_met: bool = False) -> Decimal | None:
    """The stop to move to, or None to leave the current one. ``best``: the best mark since entry (peak for a long,
    trough for a short), already including ``mark``."""
    if entry <= 0 or best <= 0 or mark <= 0 or tick <= 0 or trail_pct <= 0:
        return None
    if not target_met and favourable_move_pct(long, entry, best) < trail_activate_pct:
        return None
    if long:
        candidate = to_tick(best * (1 - trail_pct / HUNDRED), tick, long=True)
        if candidate - current_stop < tick:  # never loosens, and a move is at least one tick
            return None
        if candidate >= mark:  # would trigger at once: not a valid resting stop
            return None
    else:
        candidate = to_tick(best * (1 + trail_pct / HUNDRED), tick, long=False)
        if current_stop - candidate < tick:
            return None
        if candidate <= mark:
            return None
    return candidate
