"""Pure Decimal accounting for paper Binance-style isolated spot margin.

The functions in this module have no database, clock, or network dependencies. Callers
pass rates and timestamps explicitly so the hourly ledger can be persisted idempotently.
Hourly rates passed to the math functions are fractions (0.00000656 = 0.000656%/hour).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Literal

Side = Literal["LONG", "SHORT"]
ZERO = Decimal("0")
ONE = Decimal("1")
HOUR_SECONDS = Decimal("3600")


@dataclass(frozen=True)
class BorrowSizing:
    """Exact account inventory and debt amounts after the spot conversion."""

    base_qty: Decimal
    quote_qty: Decimal
    borrowed_base: Decimal
    borrowed_quote: Decimal


@dataclass(frozen=True)
class InterestCharge:
    """One immutable interest-ledger charge for a principal period."""

    period_start: datetime
    period_end: datetime
    asset: str
    principal: Decimal
    hourly_rate: Decimal
    amount: Decimal


def _positive(name: str, value: Decimal, *, allow_zero: bool = False) -> None:
    """Validate a finite non-negative or positive Decimal input."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if value < ZERO or (not allow_zero and value == ZERO):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {qualifier}")


def hourly_rate_from_percent(rate_percent: Decimal) -> Decimal:
    """Convert Binance's displayed percentage-per-hour setting to a fractional rate."""
    _positive("rate_percent", rate_percent, allow_zero=True)
    return rate_percent / Decimal("100")


def size_margin_borrow(side: Side, collateral_usdt: Decimal, price: Decimal, leverage: Decimal) -> BorrowSizing:
    """Size a margin entry using Binance's asset-to-liability leverage ratio.

    Longs borrow quote currency to buy `collateral * leverage` of base. Shorts borrow
    base worth `collateral * (leverage - 1)`, sell it, and retain the sale proceeds plus
    collateral in quote. Returned `base_qty`/`quote_qty` are account inventory after the
    conversion; the short's sold quantity is represented by `borrowed_base`.
    """
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    _positive("collateral_usdt", collateral_usdt)
    _positive("price", price)
    _positive("leverage", leverage)
    if leverage < ONE:
        raise ValueError("leverage must be at least 1")

    notional = collateral_usdt * leverage
    if side == "LONG":
        return BorrowSizing(
            base_qty=notional / price,
            quote_qty=ZERO,
            borrowed_base=ZERO,
            borrowed_quote=collateral_usdt * (leverage - ONE),
        )

    borrowed_base = collateral_usdt * (leverage - ONE) / price
    return BorrowSizing(
        base_qty=ZERO,
        quote_qty=collateral_usdt + borrowed_base * price,
        borrowed_base=borrowed_base,
        borrowed_quote=ZERO,
    )


def account_asset_value(base_qty: Decimal, quote_qty: Decimal, price: Decimal) -> Decimal:
    """Return total isolated-account assets in quote currency."""
    for name, value in (("base_qty", base_qty), ("quote_qty", quote_qty)):
        _positive(name, value, allow_zero=True)
    _positive("price", price)
    return base_qty * price + quote_qty


def account_liability_value(
    borrowed_base: Decimal,
    borrowed_quote: Decimal,
    interest_base: Decimal,
    interest_quote: Decimal,
    price: Decimal,
) -> Decimal:
    """Return borrowed principal and interest valued in quote currency."""
    for name, value in (
        ("borrowed_base", borrowed_base), ("borrowed_quote", borrowed_quote),
        ("interest_base", interest_base), ("interest_quote", interest_quote),
    ):
        _positive(name, value, allow_zero=True)
    _positive("price", price)
    return (borrowed_base + interest_base) * price + borrowed_quote + interest_quote


def margin_level(
    base_qty: Decimal,
    quote_qty: Decimal,
    borrowed_base: Decimal,
    borrowed_quote: Decimal,
    interest_base: Decimal,
    interest_quote: Decimal,
    price: Decimal,
) -> Decimal:
    """Calculate total asset value divided by liabilities plus outstanding interest."""
    assets = account_asset_value(base_qty, quote_qty, price)
    liabilities = account_liability_value(borrowed_base, borrowed_quote, interest_base, interest_quote, price)
    if liabilities == ZERO:
        return Decimal("Infinity")
    return assets / liabilities


def liquidation_price(
    side: Side,
    *,
    base_qty: Decimal,
    quote_qty: Decimal,
    borrowed_base: Decimal,
    borrowed_quote: Decimal,
    interest_base: Decimal,
    interest_quote: Decimal,
    liquidation_level: Decimal,
    entry_price: Decimal | None = None,
) -> Decimal:
    """Solve the price at which the isolated margin level equals `liquidation_level`.

    The same linear equation handles long and short account inventories. When provided,
    `entry_price` also checks that liquidation lies on the adverse side of entry.
    """
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    _positive("liquidation_level", liquidation_level)
    for name, value in (
        ("base_qty", base_qty), ("quote_qty", quote_qty), ("borrowed_base", borrowed_base),
        ("borrowed_quote", borrowed_quote), ("interest_base", interest_base),
        ("interest_quote", interest_quote),
    ):
        _positive(name, value, allow_zero=True)
    if entry_price is not None:
        _positive("entry_price", entry_price)

    base_asset = base_qty
    quote_asset = quote_qty
    base_debt = borrowed_base + interest_base
    quote_debt = borrowed_quote + interest_quote
    denominator = base_asset - liquidation_level * base_debt
    numerator = liquidation_level * quote_debt - quote_asset
    if denominator == ZERO:
        raise ValueError("liquidation price is undefined for this asset/debt composition")
    price = numerator / denominator
    if not price.is_finite() or price <= ZERO:
        raise ValueError("the liquidation threshold has no positive price solution")
    if entry_price is not None and ((side == "LONG" and price >= entry_price)
                                    or (side == "SHORT" and price <= entry_price)):
        raise ValueError("liquidation price is not on the adverse side of entry")
    return price


def interest_for_period(principal: Decimal, hourly_rate: Decimal, seconds: Decimal) -> Decimal:
    """Calculate simple interest for an exact number of seconds, without float conversion."""
    _positive("principal", principal, allow_zero=True)
    _positive("hourly_rate", hourly_rate, allow_zero=True)
    _positive("seconds", seconds, allow_zero=True)
    if seconds > HOUR_SECONDS:
        raise ValueError("an interest period cannot exceed one hour")
    with localcontext() as context:
        context.prec = 38
        return principal * hourly_rate * seconds / HOUR_SECONDS


def interest_ceil_8(amount: Decimal) -> Decimal:
    """Apply Binance's documented upward rounding to 8 decimal places."""
    _positive("amount", amount, allow_zero=True)
    return amount.quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)


def _utc(value: datetime) -> datetime:
    """Require an aware timestamp and normalize it to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _elapsed_seconds(start: datetime, end: datetime) -> Decimal:
    """Convert a timedelta to Decimal seconds without a float intermediate."""
    delta = end - start
    whole_seconds = delta.days * 86_400 + delta.seconds
    return Decimal(whole_seconds) + Decimal(delta.microseconds) / Decimal(1_000_000)


def next_hour_settlement(value: datetime) -> datetime:
    """Return the next top-of-hour boundary strictly after `value`."""
    current = _utc(value)
    return current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def interest_schedule(
    principal: Decimal,
    hourly_rate: Decimal,
    borrowed_at: datetime,
    through: datetime,
    asset: str,
    *,
    repaid_at: datetime | None = None,
    principal_at_settlement: Callable[[datetime], Decimal] | None = None,
) -> tuple[InterestCharge, ...]:
    """Build charges through a time using the prorated-first-period Binance schedule.

    The first row spans borrow time to the next hour. Each later settlement charges a
    full next hourly period (for example, the 13:00 settlement creates a row for 13:00–
    14:00). A repayment inside that already-settled hour does not refund it. A repayment
    before the first settlement is prorated to the repayment time; Binance's FAQ is silent
    on this pre-settlement case, so the caller should mark such a row unverified.

    For partial repayments after the first settlement, `principal_at_settlement` supplies
    the remaining principal as of each settlement. It must return a finite non-negative
    Decimal. Rows with zero principal are omitted.
    """
    _positive("principal", principal)
    _positive("hourly_rate", hourly_rate, allow_zero=True)
    if not asset or asset != asset.upper():
        raise ValueError("asset must be a non-empty uppercase code")
    start = _utc(borrowed_at)
    end = _utc(through)
    if end < start:
        raise ValueError("through must not precede borrowed_at")
    repay = _utc(repaid_at) if repaid_at is not None else None
    effective_end = min(end, repay) if repay is not None else end
    if effective_end < start:
        raise ValueError("repaid_at must not precede borrowed_at")

    first_settlement = next_hour_settlement(start)
    charges: list[InterestCharge] = []
    if effective_end < first_settlement:
        if repay is not None and effective_end > start:
            seconds = _elapsed_seconds(start, effective_end)
            charges.append(InterestCharge(
                start, effective_end, asset, principal, hourly_rate,
                interest_for_period(principal, hourly_rate, seconds),
            ))
        return tuple(charges)

    first_seconds = _elapsed_seconds(start, first_settlement)
    charges.append(InterestCharge(
        start, first_settlement, asset, principal, hourly_rate,
        interest_for_period(principal, hourly_rate, first_seconds),
    ))

    settlement = first_settlement
    while settlement <= effective_end:
        settled_principal = principal_at_settlement(settlement) if principal_at_settlement else principal
        _positive("principal_at_settlement", settled_principal, allow_zero=True)
        if settled_principal > ZERO:
            next_settlement = settlement + timedelta(hours=1)
            charges.append(InterestCharge(
                settlement, next_settlement, asset, settled_principal, hourly_rate,
                interest_for_period(settled_principal, hourly_rate, HOUR_SECONDS),
            ))
        settlement += timedelta(hours=1)
    return tuple(charges)


def sum_interest(charges: Iterable[InterestCharge]) -> Decimal:
    """Sum ledger amounts exactly as Decimal values."""
    return sum((charge.amount for charge in charges), ZERO)


def repay_amount(principal: Decimal, accrued_interest: Decimal) -> Decimal:
    """Return principal plus accrued interest due in the borrowed asset."""
    _positive("principal", principal, allow_zero=True)
    _positive("accrued_interest", accrued_interest, allow_zero=True)
    return principal + accrued_interest


def pro_rata(amount: Decimal, closed_qty: Decimal, total_qty: Decimal) -> Decimal:
    """Allocate an amount to a partial close using an exact Decimal ratio."""
    _positive("amount", amount, allow_zero=True)
    _positive("closed_qty", closed_qty, allow_zero=True)
    _positive("total_qty", total_qty)
    if closed_qty > total_qty:
        raise ValueError("closed_qty must not exceed total_qty")
    return amount * closed_qty / total_qty


def close_pnl(
    side: Side,
    entry_price: Decimal,
    exit_price: Decimal,
    base_qty: Decimal,
    *,
    interest: Decimal = ZERO,
    fees: Decimal = ZERO,
) -> Decimal:
    """Return realized close P&L net of interest and fees."""
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    for name, value in (("entry_price", entry_price), ("exit_price", exit_price), ("base_qty", base_qty)):
        _positive(name, value)
    _positive("interest", interest, allow_zero=True)
    _positive("fees", fees, allow_zero=True)
    direction = ONE if side == "LONG" else -ONE
    return direction * (exit_price - entry_price) * base_qty - interest - fees


def close_cash_return(collateral_usdt: Decimal, realized_pnl: Decimal, interest: Decimal, fees: Decimal) -> Decimal:
    """Return collateral plus gross P&L less paid interest and fees; may be negative."""
    _positive("collateral_usdt", collateral_usdt, allow_zero=True)
    if not isinstance(realized_pnl, Decimal) or not realized_pnl.is_finite():
        raise ValueError("realized_pnl must be a finite Decimal")
    _positive("interest", interest, allow_zero=True)
    _positive("fees", fees, allow_zero=True)
    return collateral_usdt + realized_pnl - interest - fees
