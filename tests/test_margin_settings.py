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


def test_no_pair_is_accepted_until_a_complete_tier_one_row_is_sourced():
    assert MARGIN_PAIR_SETTINGS == {}
    assert not any(key.startswith("margin.pair.") for key in MARGIN_SETTING_BOUNDS)
