"""Exact Decimal checks for isolated spot-margin sizing, interest and close accounting."""

from datetime import UTC, datetime
from decimal import Decimal

from cryptonite_execution.spot_margin import (
    account_asset_value,
    close_cash_return,
    close_pnl,
    hourly_rate_from_percent,
    interest_for_period,
    interest_schedule,
    liquidation_price,
    margin_level,
    pro_rata,
    repay_amount,
    size_margin_borrow,
    sum_interest,
)
from hypothesis import given
from hypothesis import strategies as st

D = Decimal


def test_long_and_short_borrow_sizing_conserve_the_margin_ratio():
    long = size_margin_borrow("LONG", D("10"), D("100"), D("3"))
    assert long.base_qty == D("0.3")
    assert long.quote_qty == D("0")
    assert long.borrowed_base == D("0")
    assert long.borrowed_quote == D("20")
    assert margin_level(long.base_qty, long.quote_qty, long.borrowed_base, long.borrowed_quote, D("0"), D("0"), D("100")) == D("1.5")

    short = size_margin_borrow("SHORT", D("10"), D("100"), D("3"))
    assert short.base_qty == D("0")
    assert short.quote_qty == D("30")
    assert short.borrowed_base == D("0.2")
    assert short.borrowed_quote == D("0")
    assert margin_level(short.base_qty, short.quote_qty, short.borrowed_base, short.borrowed_quote, D("0"), D("0"), D("100")) == D("1.5")


def test_binance_percent_rate_converts_to_a_fraction_without_float_rounding():
    assert hourly_rate_from_percent(D("0.00065600")) == D("0.0000065600")
    assert interest_for_period(D("1000"), hourly_rate_from_percent(D("0.00065600")), D("3600")) == D("0.0065600000")


def test_margin_level_and_liquidation_prices_match_the_documented_examples():
    assert account_asset_value(D("0.2"), D("0"), D("100")) == D("20")
    long_level = margin_level(D("0.2"), D("0"), D("0"), D("10"), D("0"), D("0"), D("100"))
    assert long_level == D("2")
    long_liquidation = liquidation_price(
        "LONG", base_qty=D("0.2"), quote_qty=D("0"), borrowed_base=D("0"), borrowed_quote=D("10"),
        interest_base=D("0"), interest_quote=D("0"), liquidation_level=D("1.05"), entry_price=D("100"),
    )
    assert long_liquidation == D("52.5")

    short_level = margin_level(D("0"), D("20"), D("0.1"), D("0"), D("0"), D("0"), D("100"))
    assert short_level == D("2")
    short_liquidation = liquidation_price(
        "SHORT", base_qty=D("0"), quote_qty=D("20"), borrowed_base=D("0.1"), borrowed_quote=D("0"),
        interest_base=D("0"), interest_quote=D("0"), liquidation_level=D("1.05"), entry_price=D("100"),
    )
    assert short_liquidation == D("190.4761904761904761904761905")


def test_interest_schedule_charges_prorated_first_period_then_full_hours():
    borrow = datetime(2026, 10, 29, 12, 20, tzinfo=UTC)
    charges = interest_schedule(D("1500"), D("0.00000656"), borrow,
                               datetime(2026, 10, 29, 14, 0, tzinfo=UTC), "USDT")
    assert [(row.period_start, row.period_end) for row in charges] == [
        (borrow, datetime(2026, 10, 29, 13, 0, tzinfo=UTC)),
        (datetime(2026, 10, 29, 13, 0, tzinfo=UTC), datetime(2026, 10, 29, 14, 0, tzinfo=UTC)),
        (datetime(2026, 10, 29, 14, 0, tzinfo=UTC), datetime(2026, 10, 29, 15, 0, tzinfo=UTC)),
    ]
    assert charges[0].amount == D("0.00656")  # 1,500 * rate * 40/60
    assert charges[1].amount == D("0.00984")
    assert charges[2].amount == D("0.00984")


def test_mid_hour_repay_keeps_already_settled_full_hour_and_does_not_prorate_it():
    borrow = datetime(2026, 10, 29, 12, 20, tzinfo=UTC)
    repay = datetime(2026, 10, 29, 13, 20, tzinfo=UTC)
    charges = interest_schedule(D("1500"), D("0.00000656"), borrow, repay, "USDT", repaid_at=repay)
    assert [row.amount for row in charges] == [D("0.00656"), D("0.00984")]
    assert charges[-1].period_start == datetime(2026, 10, 29, 13, 0, tzinfo=UTC)
    assert charges[-1].period_end == datetime(2026, 10, 29, 14, 0, tzinfo=UTC)
    assert sum_interest(charges) == D("0.01640")
    assert repay_amount(D("1500"), sum_interest(charges)) == D("1500.01640")


def test_repay_before_first_settlement_uses_unverified_proration_to_repay_time():
    borrow = datetime(2026, 10, 29, 12, 20, tzinfo=UTC)
    repay = datetime(2026, 10, 29, 12, 50, tzinfo=UTC)
    charges = interest_schedule(D("1500"), D("0.00000656"), borrow, repay, "USDT", repaid_at=repay)
    assert len(charges) == 1
    assert charges[0].period_start == borrow and charges[0].period_end == repay
    assert charges[0].amount == D("0.00492")  # 30/60 hour; behavior marked unverified in the source spec


def test_partial_repayment_changes_principal_at_the_next_settlement_only():
    borrow = datetime(2026, 10, 29, 12, 20, tzinfo=UTC)
    first_full_hour = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)
    charges = interest_schedule(
        D("1500"), D("0.00000656"), borrow, datetime(2026, 10, 29, 14, 0, tzinfo=UTC), "USDT",
        principal_at_settlement=lambda settlement: D("1500") if settlement == first_full_hour else D("600"),
    )
    assert [row.principal for row in charges] == [D("1500"), D("1500"), D("600")]
    assert [row.amount for row in charges] == [D("0.00656"), D("0.00984"), D("0.003936")]
    assert sum_interest(charges) == D("0.020336")


def test_close_pnl_and_cash_return_conserve_exact_long_short_and_partial_close_amounts():
    scenarios = (
        ("LONG", D("100"), D("102"), D("2"), D("0.01"), D("0.02"), D("13.97")),
        ("SHORT", D("100"), D("98"), D("2"), D("0.01"), D("0.02"), D("13.97")),
    )
    for side, entry, exit_price, qty, interest, fees, expected_cash in scenarios:
        gross = (exit_price - entry) * qty * (D("1") if side == "LONG" else D("-1"))
        net = close_pnl(side, entry, exit_price, qty, interest=interest, fees=fees)
        assert net == gross - interest - fees
        cash = close_cash_return(D("10"), gross, interest, fees)
        assert cash == expected_cash
        assert cash - D("10") - gross + interest + fees == D("0")

    total_qty, closed_qty = D("1"), D("0.25")
    gross = close_pnl("LONG", D("100"), D("104"), total_qty)
    accrued = D("0.12")
    fees = D("0.08")
    partial_gross = pro_rata(gross, closed_qty, total_qty)
    partial_interest = pro_rata(accrued, closed_qty, total_qty)
    partial_fees = pro_rata(fees, closed_qty, total_qty)
    remaining = close_cash_return(D("10"), partial_gross, partial_interest, partial_fees)
    assert partial_gross == D("1.00") and partial_interest == D("0.030") and partial_fees == D("0.020")
    assert remaining == D("10.95")
    assert pro_rata(gross, total_qty - closed_qty, total_qty) + partial_gross == gross


@given(
    price=st.integers(min_value=101, max_value=199),
    base_qty=st.integers(min_value=1, max_value=100),
)
def test_long_margin_level_falls_as_price_moves_against_the_position(price, base_qty):
    p = D(price)
    qty = D(base_qty) / D("100")
    entry_level = margin_level(qty, D("0"), D("0"), D("50"), D("0"), D("0"), D("200"))
    adverse_level = margin_level(qty, D("0"), D("0"), D("50"), D("0"), D("0"), p)
    assert p < D("200") and adverse_level < entry_level


@given(price=st.integers(min_value=201, max_value=1000))
def test_short_margin_level_falls_as_price_moves_against_the_position(price):
    p = D(price)
    entry_level = margin_level(D("0"), D("200"), D("0.5"), D("0"), D("0"), D("0"), D("200"))
    adverse_level = margin_level(D("0"), D("200"), D("0.5"), D("0"), D("0"), D("0"), p)
    assert p > D("200") and adverse_level < entry_level


def test_interest_rounding_matches_binance_upward_eight_decimal_rule():
    from cryptonite_execution.spot_margin import interest_ceil_8

    assert interest_ceil_8(D("0.019455551")) == D("0.01945556")
    assert interest_ceil_8(D("0.01000000")) == D("0.01000000")
