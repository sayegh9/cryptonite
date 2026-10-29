"""Exact Decimal runtime settings and risk-tier behavior for paper spot margin."""

from decimal import Decimal

import pytest
from cryptonite_api.settings_registry import (
    REGISTRY,
    SYSTEM_STORE,
    WRITE_PATHS,
    Apply,
    Tier,
    Tighter,
    validate_value,
    write_needs_totp,
)
from cryptonite_schemas.runtime_settings import (
    MARGIN_INTEREST_KEYS,
    MARGIN_PAIR_SETTINGS,
    MARGIN_SETTING_BOUNDS,
    default_margin_settings,
    read_decimal,
    read_margin_settings,
)

D = Decimal


def by_key(key: str):
    return next(setting for setting in REGISTRY if setting.key == key)


def test_margin_defaults_preserve_the_public_rate_digits_exactly():
    settings = default_margin_settings()
    assert settings.interest_hourly_pct == {
        "BTC": D("0.00004933"),
        "ETH": D("0.00022700"),
        "XRP": D("0.00039871"),
        "WLD": D("0.00140158"),
        "USDT": D("0.00065600"),
    }
    assert settings.liquidation_fee_pct == D("2")


def test_margin_runtime_reader_is_decimal_exact_and_keeps_last_good_on_bad_values():
    base = default_margin_settings()
    applied, problems = read_margin_settings({
        MARGIN_INTEREST_KEYS["BTC"]: '"0.00012345"',
        "margin.liquidation_fee_pct": '"2.25"',
    }, base)
    assert applied.interest_hourly_pct["BTC"] == D("0.00012345")
    assert applied.liquidation_fee_pct == D("2.25")
    assert problems == []

    kept, problems = read_margin_settings({
        MARGIN_INTEREST_KEYS["BTC"]: '"0.000123456"',  # more than 8 places
        "margin.liquidation_fee_pct": '"6"',  # above its bound
    }, applied)
    assert kept == applied
    assert len(problems) == 2


def test_margin_decimal_reader_refuses_non_finite_values_and_uses_default_when_missing():
    bound = MARGIN_SETTING_BOUNDS[MARGIN_INTEREST_KEYS["BTC"]]
    assert read_decimal(None, bound, D("0.0001")) == (bound.default, None)
    for raw in ('"NaN"', '"Infinity"', '"1.1"', '"0.000000001"', '"oops"'):
        value, problem = read_decimal(raw, bound, D("0.0001"))
        assert value == D("0.0001") and problem, raw


def test_every_margin_runtime_key_has_exact_bounds_and_a_hot_registry_entry():
    assert set(MARGIN_SETTING_BOUNDS) == set(WRITE_PATHS).intersection(MARGIN_SETTING_BOUNDS)
    for key, bound in MARGIN_SETTING_BOUNDS.items():
        setting = by_key(key)
        assert bound.low <= bound.default <= bound.high, key
        assert setting.store == SYSTEM_STORE and setting.apply is Apply.HOT and setting.tier is Tier.T_L, key
        assert setting.default == bound.default and setting.minimum == bound.low and setting.maximum == bound.high, key
        assert setting.decimals == bound.decimals, key


def test_interest_and_fee_updates_apply_their_risk_direction_for_totp():
    rate = by_key(MARGIN_INTEREST_KEYS["BTC"])
    assert rate.tighter is Tighter.HIGHER
    assert write_needs_totp(rate, D("0.0001"), D("0.0002")) is False
    assert write_needs_totp(rate, D("0.0002"), D("0.0001")) is True

    fee = by_key("margin.liquidation_fee_pct")
    assert fee.tighter is Tighter.LOWER
    assert write_needs_totp(fee, D("2"), D("1.5")) is False
    assert write_needs_totp(fee, D("2"), D("2.5")) is True


def test_margin_values_are_validated_at_their_decimal_quantum():
    rate = by_key(MARGIN_INTEREST_KEYS["BTC"])
    validate_value(rate, D("0.00004933"))
    for bad in (D("0.000049331"), D("1.01"), D("-0.0001")):
        with pytest.raises(ValueError):
            validate_value(rate, bad)


def test_pair_defaults_use_displayed_borrow_caps_and_faq_levels():
    assert MARGIN_PAIR_SETTINGS == {
        "BTCUSDT": {"max_leverage": D("10"), "call_level": D("1.10"), "liquidation_level": D("1.05"),
                    "max_borrow_BTC": D("80"), "max_borrow_USDT": D("450000")},
        "ETHUSDT": {"max_leverage": D("10"), "call_level": D("1.10"), "liquidation_level": D("1.05"),
                    "max_borrow_ETH": D("1600"), "max_borrow_USDT": D("450000")},
        "XRPUSDT": {"max_leverage": D("10"), "call_level": D("1.10"), "liquidation_level": D("1.05"),
                    "max_borrow_XRP": D("584000"), "max_borrow_USDT": D("450000")},
        "WLDUSDT": {"max_leverage": D("5"), "call_level": D("1.19"), "liquidation_level": D("1.15"),
                    "max_borrow_WLD": D("66600"), "max_borrow_USDT": D("119000")},
    }
    assert {k for k in MARGIN_SETTING_BOUNDS if k.startswith("margin.pair.")} == {
        f"margin.pair.{pair}.{field}" for pair, fields in MARGIN_PAIR_SETTINGS.items() for field in fields
    }


def test_pair_settings_reader_defaults_caps_to_zero_per_missing_side_and_rejects_unknown_pairs():
    base = default_margin_settings()
    applied, problems = read_margin_settings({"margin.pair.BTCUSDT.max_borrow_BTC": '"0"'}, base)
    assert problems == []
    assert applied.pair_settings["BTCUSDT"]["max_borrow_BTC"] == D("0")
    assert applied.pair_settings["BTCUSDT"]["max_borrow_USDT"] == D("450000")
    assert "DOGEUSDT" not in applied.pair_settings


def test_unverified_pair_borrow_caps_are_tightened_for_free_and_raised_with_totp():
    cap = by_key("margin.pair.BTCUSDT.max_borrow_BTC")
    assert cap.tighter is Tighter.LOWER
    assert "regular-user displayed limit, not tier-identified" in cap.note
    assert write_needs_totp(cap, D("80"), D("40")) is False
    assert write_needs_totp(cap, D("40"), D("80")) is True
    validate_value(cap, D("100"))
