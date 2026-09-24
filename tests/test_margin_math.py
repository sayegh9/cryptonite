"""Static test vectors for perpetual margin math (cryptonite_execution.margin).

Expected liquidation prices were computed independently from the isolated closed forms
(WB = Q*EP/L):  long = (Q*EP - WB - cum) / (Q*(1 - MMR)),  short = (Q*EP + WB + cum) / (Q*(1 + MMR)).
"""

from decimal import Decimal

import pytest
from cryptonite_execution.derivatives import MarginMode, OrderSide
from cryptonite_execution.margin import (
    REFERENCE_BRACKETS,
    calculate_initial_margin,
    calculate_liquidation_distance,
    calculate_liquidation_price,
    calculate_maintenance_margin,
    calculate_margin_ratio,
    calculate_unrealized_pnl,
    get_bracket,
)

BTC = "BTCUSDT-PERP.BINANCE"
ETH = "ETHUSDT-PERP.BINANCE"
Q8 = Decimal("0.00000001")


def d(value: str | int) -> Decimal:
    return Decimal(str(value))


def isolated_lp(inst, side, qty, entry, leverage):
    wallet = calculate_initial_margin(qty * entry, leverage)
    return calculate_liquidation_price(inst, side, qty, entry, wallet, MarginMode.ISOLATED)


# --- Brackets --------------------------------------------------------------

@pytest.mark.parametrize("symbol", sorted(REFERENCE_BRACKETS))
def test_brackets_are_contiguous_and_maintenance_margin_is_continuous(symbol):
    brackets = REFERENCE_BRACKETS[symbol]
    assert brackets[0].notional_floor == 0 and brackets[0].cum == 0
    assert brackets[-1].notional_cap is None
    for lower, upper in zip(brackets, brackets[1:], strict=False):
        assert lower.notional_cap == upper.notional_floor
        assert upper.mmr > lower.mmr and upper.max_leverage <= lower.max_leverage
        boundary = upper.notional_floor
        assert boundary * lower.mmr - lower.cum == boundary * upper.mmr - upper.cum


@pytest.mark.parametrize(
    ("inst", "notional", "mmr", "cum"),
    [
        (BTC, "0", "0.004", "0"),
        (BTC, "49999.99", "0.004", "0"),
        (BTC, "50000", "0.005", "50"),  # floor is inclusive
        (BTC, "1000000", "0.025", "16300"),
        (ETH, "30000", "0.0065", "15"),
        (ETH, "-30000", "0.0065", "15"),  # sign-agnostic
    ],
)
def test_bracket_lookup(inst, notional, mmr, cum):
    bracket = get_bracket(inst, d(notional))
    assert (bracket.mmr, bracket.cum) == (d(mmr), d(cum))


def test_bracket_lookup_rejects_spot_and_unknown_symbols():
    with pytest.raises(ValueError, match="not a perpetual"):
        get_bracket("BTCUSDT.BINANCE", d(1000))
    with pytest.raises(ValueError, match="no maintenance brackets"):
        get_bracket("DOGEUSDT-PERP.BINANCE", d(1000))


# --- Initial / maintenance margin -------------------------------------------

def test_initial_margin():
    assert calculate_initial_margin(d(50000), 10) == d(5000)
    assert calculate_initial_margin(d(-30000), 20) == d(1500)
    with pytest.raises(ValueError):
        calculate_initial_margin(d(1000), 0)


@pytest.mark.parametrize(
    ("inst", "notional", "expected"),
    [(BTC, "40000", "160"), (BTC, "100000", "450"), (ETH, "30000", "180")],
)
def test_maintenance_margin(inst, notional, expected):
    assert calculate_maintenance_margin(inst, d(notional)) == d(expected)


# --- Unrealized PnL ----------------------------------------------------------

@pytest.mark.parametrize(
    ("side", "mark", "expected"),
    [
        (OrderSide.BUY, "51000", "500"),  # long, favourable
        (OrderSide.BUY, "50000", "0"),
        (OrderSide.BUY, "49000", "-500"),  # long, adverse
        (OrderSide.SELL, "49000", "500"),  # short, favourable
        (OrderSide.SELL, "50000", "0"),
        (OrderSide.SELL, "51000", "-500"),  # short, adverse
    ],
)
def test_unrealized_pnl(side, mark, expected):
    assert calculate_unrealized_pnl(side, d(50000), d(mark), d("0.5")) == d(expected)


def test_unrealized_pnl_rejects_signed_quantity():
    with pytest.raises(ValueError):
        calculate_unrealized_pnl(OrderSide.SELL, d(50000), d(49000), d("-0.5"))


# --- Liquidation price (isolated) -------------------------------------------

@pytest.mark.parametrize(
    ("inst", "side", "qty", "entry", "leverage", "expected"),
    [
        (BTC, OrderSide.BUY, "1", "50000", 10, "45180.72289157"),
        (BTC, OrderSide.SELL, "1", "50000", 10, "54776.11940299"),  # tier 2 at the trigger
        (ETH, OrderSide.BUY, "10", "3000", 20, "2867.13638651"),
        (ETH, OrderSide.SELL, "10", "3000", 20, "3131.14754098"),
        (BTC, OrderSide.BUY, "0.1", "60000", 10, "54216.86746988"),  # blueprint §2.2 example
        (BTC, OrderSide.SELL, "0.1", "60000", 10, "65737.05179283"),
    ],
)
def test_isolated_liquidation_price_vectors(inst, side, qty, entry, leverage, expected):
    lp = isolated_lp(inst, side, d(qty), d(entry), leverage)
    assert lp.quantize(Q8) == d(expected)


def test_short_tier_crossing_does_not_liquidate_late():
    # Entry notional 50,000 sits in tier 1, but the notional at the trigger (~54,776) is tier 2.
    # Using the entry tier would give 54,780.88: a later (unsafe) trigger for a short.
    lp = isolated_lp(BTC, OrderSide.SELL, d(1), d(50000), 10)
    naive_tier1 = (d(50000) + d(5000)) / (d(1) * (1 + d("0.004")))
    assert lp < naive_tier1
    assert get_bracket(BTC, lp).mmr == d("0.005")


@pytest.mark.parametrize("inst", [BTC, ETH])
@pytest.mark.parametrize("notional", ["1000", "40000", "200000", "2000000"])
def test_liquidation_price_is_on_the_losing_side_of_entry(inst, notional):
    entry = d(3000) if inst == ETH else d(50000)
    qty = d(notional) / entry
    max_leverage = get_bracket(inst, d(notional)).max_leverage
    for leverage in sorted({2, 5, 10, 20, max_leverage} & set(range(2, max_leverage + 1))):
        assert isolated_lp(inst, OrderSide.BUY, qty, entry, leverage) < entry
        assert isolated_lp(inst, OrderSide.SELL, qty, entry, leverage) > entry


def test_fully_collateralised_long_has_no_liquidation_price():
    assert isolated_lp(BTC, OrderSide.BUY, d(1), d(50000), 1) is None


def test_isolated_rejects_cross_terms():
    with pytest.raises(ValueError, match="ISOLATED"):
        calculate_liquidation_price(
            BTC, OrderSide.BUY, d(1), d(50000), d(5000), MarginMode.ISOLATED, other_maintenance_margin=d(10)
        )


# --- Liquidation price (cross) ----------------------------------------------

def test_cross_liquidation_price_accounts_for_other_positions():
    # 1 BTC long @ 50,000 on a 10,000 cross wallet; an ETH position elsewhere carries MM 180 and uPnL -500.
    alone = calculate_liquidation_price(BTC, OrderSide.BUY, d(1), d(50000), d(10000), MarginMode.CROSS)
    shared = calculate_liquidation_price(
        BTC, OrderSide.BUY, d(1), d(50000), d(10000), MarginMode.CROSS,
        other_maintenance_margin=d(180), other_unrealized_pnl=d(-500),
    )
    assert alone.quantize(Q8) == d("40160.64257028")  # (50000 - 10000) / 0.996
    assert shared.quantize(Q8) == d("40843.37349398")  # (50000 - 10000 + 180 + 500) / 0.996
    assert shared > alone  # other positions' losses and MM pull liquidation closer


# --- Margin ratio / distance --------------------------------------------------

@pytest.mark.parametrize(
    ("inst", "side", "qty", "entry", "leverage"),
    [
        (BTC, OrderSide.BUY, "1", "50000", 10),
        (BTC, OrderSide.SELL, "1", "50000", 10),
        (ETH, OrderSide.BUY, "10", "3000", 20),
        (ETH, OrderSide.SELL, "10", "3000", 20),
    ],
)
def test_margin_ratio_reaches_100_percent_exactly_at_liquidation_price(inst, side, qty, entry, leverage):
    qty, entry = d(qty), d(entry)
    wallet = calculate_initial_margin(qty * entry, leverage)
    lp = calculate_liquidation_price(inst, side, qty, entry, wallet, MarginMode.ISOLATED)

    def ratio_at(mark):
        balance = wallet + calculate_unrealized_pnl(side, entry, mark, qty)
        return calculate_margin_ratio(calculate_maintenance_margin(inst, qty * mark), balance)

    assert abs(ratio_at(lp) - 1) < d("1e-20")
    step = lp * d("0.0001")
    safer, riskier = (lp + step, lp - step) if side is OrderSide.BUY else (lp - step, lp + step)
    assert ratio_at(safer) < 1 < ratio_at(riskier)
    assert ratio_at(entry) < d("0.2")  # freshly opened position is far from the trigger


def test_margin_ratio_edge_cases():
    assert calculate_margin_ratio(d(450), d(900)) == d("0.5")
    assert calculate_margin_ratio(d(450), d(0)) == Decimal("Infinity")
    assert calculate_margin_ratio(d(450), d(-10)) == Decimal("Infinity")


def test_liquidation_distance():
    assert calculate_liquidation_distance(d(50000), d(45000)) == d("0.1")
    assert calculate_liquidation_distance(d(50000), d(55000)) == d("0.1")
    assert calculate_liquidation_distance(d(50000), d(50000)) == 0
    with pytest.raises(ValueError):
        calculate_liquidation_distance(d(0), d(45000))
