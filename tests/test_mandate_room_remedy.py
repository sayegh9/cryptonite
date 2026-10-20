"""The mandate edit an order card offers when an entry has no room left (``room_remedy``), as pure policy.

The position limit is raised first, to what the holding plus the request needs and at most the 25% cap; the budget
only when the limit is already at the cap or the budget, not the limit, is what is full. The room after the edit is the
room the entry sizing (``entry_room_pct``) would then find.
"""

from decimal import Decimal as D

from cryptonite_schemas.mandate import MIN_MANDATE_ENTRY_PCT, room_pct, room_remedy
from cryptonite_schemas.marks import entry_room_pct

BTC = "BTCUSDT.BINANCE"


def test_a_full_instrument_under_a_low_limit_raises_the_limit_to_what_the_request_needs():
    # The 2026-10-20 card: a $10,000 mandate at a 10% limit already holds $1,000 of XRP; the card asks 10% more
    fix = room_remedy(D(10000), D(10), D(1000), D(1000), D(10))
    assert (fix.field, fix.old, fix.new, fix.room_after_pct) == ("max_position_pct", D(10), D(20), D(10))


def test_the_new_limit_is_a_whole_percent_rounded_up():
    fix = room_remedy(D(10000), D(10), D(1037), D(1037), D(5))  # the holding grew to 10.37%
    assert fix.field == "max_position_pct" and fix.new == D(16) and fix.room_after_pct >= D(5)


def test_the_limit_stops_at_the_cap_and_the_entry_is_sized_to_what_the_cap_leaves():
    fix = room_remedy(D(10000), D(20), D(2000), D(2000), D(10))
    assert (fix.field, fix.new, fix.room_after_pct) == ("max_position_pct", D(25), D(5))


def test_a_limit_already_at_the_cap_raises_the_budget_instead():
    fix = room_remedy(D(10000), D(25), D(2500), D(2500), D(5))
    assert (fix.field, fix.old, fix.new) == ("capital_budget_usd", D(10000), D(12500))
    assert fix.room_after_pct == D(5)


def test_a_budget_full_of_other_holdings_raises_the_budget_even_under_the_cap():
    # Nothing held in BTC, but other instruments fill the whole budget: a higher BTC limit frees no room
    fix = room_remedy(D(10000), D(10), D(0), D(10000), D(5))
    assert fix.field == "capital_budget_usd" and fix.new == D(10600)
    assert fix.room_after_pct >= D(5)


def test_a_request_at_the_limit_aims_for_half_the_limit_not_an_unbounded_budget():
    fix = room_remedy(D(10000), D(25), D(2500), D(2500), D(25))
    assert (fix.field, fix.new, fix.room_after_pct) == ("capital_budget_usd", D(20000), D("12.5"))


def test_the_room_after_the_edit_is_what_the_entry_sizing_then_finds():
    positions = [{"instrument_id": BTC, "quantity": "0.02", "current_price": "50000"}]  # $1,000
    before = entry_room_pct(D(10000), D(10), positions, BTC, None)
    assert before < MIN_MANDATE_ENTRY_PCT
    fix = room_remedy(D(10000), D(10), D(1000), D(1000), D(5))
    assert entry_room_pct(D(10000), fix.new, positions, BTC, None) == fix.room_after_pct == room_pct(D(10000), fix.new, D(1000), D(1000))


def test_a_float_hair_over_the_holding_does_not_round_up_a_whole_step():
    inst = D(0.05) * 50000  # a float fill: 2500.000000000000138...
    assert room_remedy(D(10000), D(25), inst, inst, D(5)).new == D(12500)
    assert room_remedy(D(10000), D(10), D(0.02) * 50000, D(0.02) * 50000, D(5)).new == D(15)


def test_no_request_gets_no_remedy():
    assert room_remedy(D(10000), D(10), D(1000), D(1000), D(0)) is None
