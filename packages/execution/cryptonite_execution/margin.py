"""Pure margin math for USD-M linear perpetuals (one-way position mode).

No I/O and no state: every function maps Decimal inputs to Decimal outputs.
Formulas follow Binance's published USD-M definitions:

    IM   = notional / leverage
    MM   = notional * MMR - cum                  (tier chosen by notional)
    uPnL = side * qty * (mark - entry)           (side: +1 long, -1 short)
    LP   = (WB - TMM + UPNL + cum - side*qty*entry) / (qty*MMR - side*qty)
    MR   = MM / margin_balance                   (liquidation at MR >= 1)

``cum`` (maintenance amount) makes MM continuous across tiers, so it is derived from
each tier's floor and MMR rather than hard-coded.
"""

import time
from dataclasses import dataclass
from decimal import Decimal

from cryptonite_schemas.identifiers import parse_instrument_id

from cryptonite_execution.derivatives import MarginMode, OrderSide

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class MaintenanceBracket:
    notional_floor: Decimal
    notional_cap: Decimal | None  # None = unbounded top tier
    max_leverage: int
    mmr: Decimal
    cum: Decimal

    def contains(self, notional: Decimal) -> bool:
        return self.notional_floor <= notional and (self.notional_cap is None or notional < self.notional_cap)


def _build_brackets(tiers: list[tuple[str, str | None, int, str]]) -> tuple[MaintenanceBracket, ...]:
    """Build brackets from (floor, cap, max_leverage, mmr) rows, deriving cum.

    cum_n = cum_{n-1} + floor_n * (mmr_n - mmr_{n-1}), which keeps MM continuous at each floor.
    """
    brackets: list[MaintenanceBracket] = []
    cum, prev_mmr = ZERO, ZERO
    for floor, cap, max_leverage, mmr in tiers:
        floor_d, mmr_d = Decimal(floor), Decimal(mmr)
        if brackets:
            cum += floor_d * (mmr_d - prev_mmr)
        brackets.append(
            MaintenanceBracket(floor_d, Decimal(cap) if cap is not None else None, max_leverage, mmr_d, cum)
        )
        prev_mmr = mmr_d
    return tuple(brackets)


# Reference snapshot of Binance's published USD-M leverage/margin tables. Binance revises
# these without notice; the live source is GET /fapi/v1/leverageBracket (``brackets.py``). The snapshot is
# only a fallback, and only for these two symbols (leverage plan 1.2).
REFERENCE_BRACKETS: dict[str, tuple[MaintenanceBracket, ...]] = {
    "BTCUSDT": _build_brackets([
        ("0", "50000", 125, "0.004"),
        ("50000", "250000", 100, "0.005"),
        ("250000", "1000000", 50, "0.01"),
        ("1000000", "10000000", 20, "0.025"),
        ("10000000", "20000000", 10, "0.05"),
        ("20000000", "50000000", 5, "0.10"),
        ("50000000", "100000000", 4, "0.125"),
        ("100000000", "200000000", 3, "0.15"),
        ("200000000", "300000000", 2, "0.25"),
        ("300000000", None, 1, "0.50"),
    ]),
    "ETHUSDT": _build_brackets([
        ("0", "10000", 100, "0.005"),
        ("10000", "100000", 75, "0.0065"),
        ("100000", "500000", 50, "0.01"),
        ("500000", "1000000", 25, "0.02"),
        ("1000000", "2000000", 10, "0.05"),
        ("2000000", "5000000", 5, "0.10"),
        ("5000000", "10000000", 4, "0.125"),
        ("10000000", "20000000", 3, "0.15"),
        ("20000000", "50000000", 2, "0.25"),
        ("50000000", None, 1, "0.50"),
    ]),
}


def _side_mult(side: OrderSide) -> Decimal:
    """Position direction: BUY = long (+1), SELL = short (-1)."""
    return ONE if OrderSide(side) is OrderSide.BUY else -ONE


# Live tables from Binance (``brackets.load_brackets`` installs them from the Redis cache): symbol ->
# (brackets, fetch time ns). They win over REFERENCE_BRACKETS whatever their age, so open positions are always
# valued with the newest table; ``bracket_source`` tells entries whether the table is fresh enough to open on.
_LIVE_BRACKETS: dict[str, tuple[tuple[MaintenanceBracket, ...], int]] = {}
# A table is refreshed daily; older than this, new perp entries are refused (STALE_LEVERAGE_BRACKETS)
BRACKETS_MAX_AGE_S = 36 * 3600


def set_live_brackets(symbol: str, brackets: tuple[MaintenanceBracket, ...], fetched_at_ns: int) -> None:
    if not brackets:
        raise ValueError(f"empty bracket table for {symbol}")
    _LIVE_BRACKETS[symbol.upper()] = (tuple(brackets), int(fetched_at_ns))


def clear_live_brackets() -> None:
    _LIVE_BRACKETS.clear()


@dataclass(frozen=True)
class BracketSource:
    kind: str  # LIVE | REFERENCE | MISSING
    fetched_at_ns: int | None = None
    stale: bool = False

    @property
    def entry_reject(self) -> str | None:
        """Why a new perp entry may not open on this table, or None."""
        if self.kind == "MISSING":
            return "NO_LEVERAGE_BRACKETS"
        if self.stale:
            return "STALE_LEVERAGE_BRACKETS"
        return None


def bracket_source(instrument_id: str, now_ns: int | None = None, max_age_s: float = BRACKETS_MAX_AGE_S) -> BracketSource:
    """Where this instrument's brackets come from: a live table (stale after ``max_age_s``), the reference
    snapshot (BTCUSDT/ETHUSDT only), or nowhere."""
    symbol = parse_instrument_id(instrument_id)[0].upper()
    live = _LIVE_BRACKETS.get(symbol)
    if live is not None:
        now_ns = time.time_ns() if now_ns is None else now_ns
        return BracketSource("LIVE", live[1], now_ns - live[1] > max_age_s * 1_000_000_000)
    if symbol in REFERENCE_BRACKETS:
        return BracketSource("REFERENCE")
    return BracketSource("MISSING")


def get_brackets(instrument_id: str) -> tuple[MaintenanceBracket, ...]:
    symbol, _venue, is_perp = parse_instrument_id(instrument_id)
    if not is_perp:
        raise ValueError(f"not a perpetual instrument: {instrument_id}")
    live = _LIVE_BRACKETS.get(symbol.upper())
    if live is not None:
        return live[0]
    try:
        return REFERENCE_BRACKETS[symbol]
    except KeyError:
        raise ValueError(f"no maintenance brackets for {symbol}") from None


def max_leverage_for_notional(instrument_id: str, notional: Decimal) -> int:
    """The exchange's maximum leverage for a position of ``notional`` (the bracket that contains it)."""
    return get_bracket(instrument_id, notional).max_leverage


def get_bracket(instrument_id: str, notional: Decimal) -> MaintenanceBracket:
    notional = abs(notional)
    for bracket in get_brackets(instrument_id):
        if bracket.contains(notional):
            return bracket
    raise ValueError(f"notional {notional} outside bracket table for {instrument_id}")


def calculate_initial_margin(notional: Decimal, leverage: int) -> Decimal:
    if leverage < 1:
        raise ValueError(f"leverage must be >= 1, got {leverage}")
    return abs(notional) / Decimal(leverage)


def calculate_maintenance_margin(instrument_id: str, notional: Decimal) -> Decimal:
    notional = abs(notional)
    bracket = get_bracket(instrument_id, notional)
    return notional * bracket.mmr - bracket.cum


def calculate_unrealized_pnl(side: OrderSide, entry_price: Decimal, mark_price: Decimal, quantity: Decimal) -> Decimal:
    if quantity < 0:
        raise ValueError("quantity must be absolute (>= 0); direction comes from side")
    return _side_mult(side) * quantity * (mark_price - entry_price)


def calculate_liquidation_price(
    instrument_id: str,
    side: OrderSide,
    quantity: Decimal,
    entry_price: Decimal,
    wallet_balance: Decimal,
    margin_mode: MarginMode,
    other_maintenance_margin: Decimal = ZERO,
    other_unrealized_pnl: Decimal = ZERO,
) -> Decimal | None:
    """Mark price at which this position's margin ratio reaches 100%.

    ISOLATED: ``wallet_balance`` is the position's isolated margin; other-position terms must be 0.
    CROSS: ``wallet_balance`` is the cross wallet; pass the maintenance margin and unrealized PnL
    of every *other* cross position (held at current mark).

    The MMR tier is chosen self-consistently: the tier must contain the notional *at the
    liquidation price*, not at entry. Using the entry tier can place a short's liquidation
    price above the true trigger when the price move crosses a tier floor.

    Returns None when no positive price liquidates the position (e.g. fully collateralised long).
    """
    if quantity <= 0:
        raise ValueError("quantity must be > 0")
    if MarginMode(margin_mode) is MarginMode.ISOLATED and (other_maintenance_margin or other_unrealized_pnl):
        raise ValueError("ISOLATED liquidation price does not depend on other positions")

    s = _side_mult(side)
    effective_wallet = wallet_balance - other_maintenance_margin + other_unrealized_pnl
    for bracket in get_brackets(instrument_id):
        denominator = quantity * bracket.mmr - s * quantity
        price = (effective_wallet + bracket.cum - s * quantity * entry_price) / denominator
        if price <= 0:
            continue
        if bracket.contains(quantity * price):
            return price
    return None


def calculate_funding_payment(position_size: Decimal, mark_price: Decimal, funding_rate: Decimal) -> Decimal:
    """Funding cash flow for a signed position: ``-size * mark * rate``.

    Positive rate: longs pay, shorts receive. Negative rate: shorts pay, longs receive.
    """
    return -position_size * mark_price * funding_rate


def calculate_margin_ratio(maintenance_margin: Decimal, margin_balance: Decimal) -> Decimal:
    """MM / margin balance; >= 1 means liquidation. A non-positive balance is infinitely unsafe."""
    if margin_balance <= 0:
        return Decimal("Infinity")
    return maintenance_margin / margin_balance


def calculate_liquidation_distance(mark_price: Decimal, liquidation_price: Decimal) -> Decimal:
    """|mark - liquidation| / mark as a fraction (0.05 == 5%)."""
    if mark_price <= 0:
        raise ValueError("mark_price must be > 0")
    return abs(mark_price - liquidation_price) / mark_price
