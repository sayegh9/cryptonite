"""Stage 2.7 (leverage plan): the grade ceilings and the slippage allowance are operator settings (ADR 0007 T/L tier,
lowering a ceiling and raising the allowance free), read by the orchestrator every pass and fed into sizing and the
dispatcher; the fixed leverage guardrails are listed with their source."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptonite_api.settings_registry import REGISTRY, Tier, WriteRefused, entry_view, write_needs_totp
from cryptonite_orchestrator.mandate_gate import grade_ceiling
from cryptonite_orchestrator.ops_monitor import apply_leverage_settings, load_leverage_settings
from cryptonite_schemas.runtime_settings import default_leverage_settings, read_leverage_settings
from test_paper_risk_guardrails import make_perp_dispatcher

D = Decimal


def by_key(key):
    return next(s for s in REGISTRY if s.key == key)


def test_grade_ceilings_lower_free_raise_with_totp():
    a = by_key("leverage.grade_ceiling_a")
    assert (a.tier, a.adr, a.default, a.maximum) == (Tier.T_L, "0009", 10, 10)
    assert write_needs_totp(a, D("10"), D("5")) is False
    assert write_needs_totp(a, D("5"), D("8")) is True


def test_slippage_allowance_raise_free_lower_with_totp():
    s = by_key("leverage.slippage_allowance_pct")
    assert (s.default, s.decimals, s.tighter.value) == (D("0.30"), 2, "higher")
    assert write_needs_totp(s, D("0.30"), D("0.50")) is False  # a wider allowance prices the stop further: tighter
    assert write_needs_totp(s, D("0.30"), D("0.10")) is True


@pytest.mark.parametrize("key,value", [
    ("guard.mandate_max_leverage", 10), ("guard.account_max_leverage", 10), ("guard.max_risk_per_trade_pct", 2),
    ("guard.liquidation_buffer_pct", 50), ("guard.perp_notional_pct", 25),
])
def test_fixed_leverage_guardrails_are_listed_with_their_source(key, value):
    g = by_key(key)
    assert g.tier is Tier.FIXED and g.default == value and g.adr == "0009" and g.store
    view = entry_view(g, None)
    assert view["writable"] is False and view["store"] == g.store
    with pytest.raises(WriteRefused):
        write_needs_totp(g, D(value), D(value) - 1)


def test_read_leverage_settings_stored_default_and_refused():
    base = default_leverage_settings()
    assert base.grade_ceilings == {"A": 10, "B": 5, "C": 2, "NO_TRADE": 0} and base.slippage_pct == 0.3
    applied, problems = read_leverage_settings({"leverage.grade_ceiling_a": '"7"',
                                                "leverage.slippage_allowance_pct": '"0.50"'}, base)
    assert applied.grade_ceilings["A"] == 7 and applied.slippage_pct == 0.5 and problems == []
    kept, problems = read_leverage_settings({"leverage.grade_ceiling_b": '"25"'}, applied)  # above the 10x maximum
    assert kept.grade_ceilings["B"] == 5 and problems and "leverage.grade_ceiling_b" in problems[0]


async def test_the_orchestrator_applies_them_to_sizing_and_the_dispatcher():
    conn = MagicMock()

    async def fetch(sql, keys):
        return [{"key": "leverage.grade_ceiling_a", "value": '"4"'},
                {"key": "leverage.slippage_allowance_pct", "value": '"0.40"'}]

    conn.fetch = fetch
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    settings = await load_leverage_settings(pool, default_leverage_settings())
    engine = SimpleNamespace(dispatcher=make_perp_dispatcher())
    apply_leverage_settings(engine, settings)
    assert engine.dispatcher.slippage_allowance_pct == D("0.004")
    assert grade_ceiling({"setup_grade": "A"}, engine.grade_ceilings) == 4
    assert grade_ceiling({}, engine.grade_ceilings) == 2  # ungraded: C


async def test_a_failed_read_keeps_the_last_values():
    pool = MagicMock()
    pool.acquire.side_effect = RuntimeError("db down")
    last = default_leverage_settings()
    assert await load_leverage_settings(pool, last) is last
