"""Stage 3.1 (leverage plan §2.3): the setup grade is deterministic, computed by code from stored inputs; the same
inputs always give the same grade, and a stored grade recomputes to itself."""

from dataclasses import replace

import pytest
from cryptonite_scanner.setup_grade import GradeInputs, SetupGrade, grade_setup, side_of
from hypothesis import given, settings
from hypothesis import strategies as st

# Every input aligned for a long: 3 timeframes BULL, reward 2.5x, ADX 30 with +DI on top, calm, normal funding,
# rising OI, a tight book
A_LONG = GradeInputs(side="LONG", entry=100.0, stop=96.0, target=110.0, trend_4h="BULL", trend_1h="BULL",
                     trend_15m="BULL", adx=30.0, plus_di=28.0, minus_di=12.0, atr_pct=1.0, atr_pct_q90=1.8,
                     funding_rate_8h=0.0001, oi_change_24h_pct=3.0, spread_pct=0.01, volume_24h_usd=900_000_000)


def test_every_input_aligned_is_a():
    g = grade_setup(A_LONG)
    assert g.grade == "A" and g.rr_ratio == 2.5
    assert "4h, 1h and 15m aligned" in g.reasons


@pytest.mark.parametrize("change,reason", [
    ({"trend_15m": "FLAT"}, "2 of 3 timeframes aligned"),
    ({"target": 107.0}, "below 2.0x"),  # 1.75x
    ({"adx": 20.0}, "no trend"),
    ({"funding_rate_8h": None}, "funding unknown"),
    ({"spread_pct": None}, "liquidity unknown"),
    ({"atr_pct": None}, "volatility unknown"),
])
def test_one_weak_input_is_b(change, reason):
    g = grade_setup(replace(A_LONG, **change))
    assert g.grade == "B", g.reasons
    assert any(reason in r for r in g.reasons)


def test_two_weak_inputs_are_c():
    assert grade_setup(replace(A_LONG, trend_15m="FLAT", adx=20.0)).grade == "C"
    assert grade_setup(replace(A_LONG, target=105.0)).grade == "C"  # 1.25x: weak twice
    assert grade_setup(replace(A_LONG, plus_di=10.0, minus_di=30.0)).grade == "C"  # trend against


@pytest.mark.parametrize("change,reason", [
    ({"stop": 101.0}, "either side"),
    ({"target": 99.0}, "either side"),
    ({"target": 103.0}, "below 1.0x"),  # 0.75x
    ({"trend_4h": "BEAR", "trend_1h": "BEAR"}, "both run against"),
])
def test_no_trade(change, reason):
    g = grade_setup(replace(A_LONG, **change))
    assert g.grade == "NO_TRADE" and reason in g.reasons[0]


def test_volatility_and_crowding_each_lower_one_step():
    assert grade_setup(replace(A_LONG, atr_pct=2.0)).grade == "B"
    assert grade_setup(replace(A_LONG, funding_rate_8h=0.0004)).grade == "B"
    assert grade_setup(replace(A_LONG, oi_change_24h_pct=-12.0)).grade == "B"
    assert grade_setup(replace(A_LONG, atr_pct=2.0, funding_rate_8h=0.0004)).grade == "C"
    # funding and OI together are one crowding step, not two
    assert grade_setup(replace(A_LONG, funding_rate_8h=0.0004, oi_change_24h_pct=-12.0)).grade == "B"
    # a C stepped down is NO_TRADE
    assert grade_setup(replace(A_LONG, trend_15m="FLAT", adx=20.0, atr_pct=2.0)).grade == "NO_TRADE"


def test_thin_book_caps_at_c():
    assert grade_setup(replace(A_LONG, volume_24h_usd=10_000_000)).grade == "C"
    assert grade_setup(replace(A_LONG, spread_pct=0.2)).grade == "C"


def test_short_reads_every_input_the_other_way():
    short = GradeInputs(side="SHORT", entry=100.0, stop=104.0, target=90.0, trend_4h="BEAR", trend_1h="BEAR",
                        trend_15m="BEAR", adx=30.0, plus_di=12.0, minus_di=28.0, atr_pct=1.0, atr_pct_q90=1.8,
                        funding_rate_8h=-0.0001, oi_change_24h_pct=3.0, spread_pct=0.01, volume_24h_usd=900_000_000)
    assert grade_setup(short).grade == "A"
    assert grade_setup(replace(short, funding_rate_8h=-0.0004)).grade == "B"  # shorts crowded
    assert grade_setup(replace(short, funding_rate_8h=0.0004)).grade == "A"  # longs paying: not against a short
    assert side_of("SELL") == "SHORT" and side_of("BUY") == "LONG"


def test_a_stored_grade_recomputes_to_itself():
    g = grade_setup(replace(A_LONG, trend_15m="FLAT"))
    again = grade_setup(GradeInputs(**g.as_dict()["inputs"]))
    assert again == g and isinstance(again, SetupGrade) and g.version == "setup-grade-v2"


opt = lambda s: st.one_of(st.none(), s)  # noqa: E731
floats = st.floats(min_value=0.01, max_value=1000, allow_nan=False, allow_infinity=False)
trend = opt(st.sampled_from(["BULL", "BEAR", "FLAT"]))


@settings(max_examples=500, deadline=None)
@given(side=st.sampled_from(["LONG", "SHORT"]), entry=floats, stop=floats, target=floats, t4=trend, t1=trend, t15=trend,
       adx=opt(st.floats(0, 80)), pdi=opt(st.floats(0, 60)), mdi=opt(st.floats(0, 60)), atr=opt(st.floats(0, 10)),
       q90=opt(st.floats(0, 10)), fund=opt(st.floats(-0.003, 0.003)), oi=opt(st.floats(-50, 50)),
       spread=opt(st.floats(0, 1)), vol=opt(st.floats(0, 5e9)))
def test_the_same_inputs_always_give_the_same_grade(side, entry, stop, target, t4, t1, t15, adx, pdi, mdi, atr, q90,
                                                    fund, oi, spread, vol):
    inputs = GradeInputs(side, entry, stop, target, t4, t1, t15, adx, pdi, mdi, atr, q90, fund, oi, spread, vol)
    first = grade_setup(inputs)
    assert first == grade_setup(inputs) == grade_setup(GradeInputs(**first.inputs))
    assert first.grade in ("A", "B", "C", "NO_TRADE")
    if first.grade == "A":  # A needs every input known
        assert None not in (t4, t1, t15, adx, pdi, mdi, atr, q90, fund, spread, vol)
