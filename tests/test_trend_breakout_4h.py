"""H14: the 4h trend presets fire on a real break of the prior channel, with ATR levels the HTF cannot move."""
from uuid import uuid4

import polars as pl
import pytest
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.levels import derive_levels
from cryptonite_scanner.presets import preset_by_slug
from cryptonite_schemas.profile import PRESET_CATALOG

H4_NS = 4 * 3600 * 10**9


def bars(closes: list[float]) -> pl.DataFrame:
    rows = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        rows.append({"bar_open_ns": i * H4_NS, "bar_close_inclusive_ns": (i + 1) * H4_NS - 1, "interval_end_exclusive_ns": (i + 1) * H4_NS,
                     "open": prev, "high": max(prev, c) + 1, "low": min(prev, c) - 1, "close": c, "volume": 100.0})
    return pl.DataFrame(rows)


def evaluate(slug: str, closes: list[float]):
    p = preset_by_slug(slug)
    return ScannerEngine.evaluate(ScannerEngine.enrich_indicators(bars(closes)), p.rules, p.risk, "BTCUSDT.BINANCE", "4h",
                                  uuid4(), uuid4(), price_precision=2)


def test_the_prior_channel_leaves_out_the_bar_itself():
    df = ScannerEngine.enrich_indicators(bars([100.0 + i for i in range(30)]))
    assert df["donchian_upper"][-1] == df["high"][-1]
    assert df["donchian_upper_prior"][-1] == df["high"][-2]


def uptrend_then_flat_then_break():
    return [100.0 + i for i in range(260)] + [359.0, 358.0] * 15 + [380.0]


def test_a_long_fires_on_the_break_with_atr_levels():
    found = evaluate("trend-breakout-4h", uptrend_then_flat_then_break())
    assert found, "a close above the prior 20-bar high in an uptrend is the setup"
    c = found[-1]
    entry, stop, target = float(c.suggested_entry), float(c.suggested_stop), float(c.suggested_target)
    assert entry == 380.0
    assert target - entry == pytest.approx(2 * (entry - stop), abs=0.02)


def test_a_short_is_the_mirror():
    closes = [1000.0 - i for i in range(260)] + [741.0, 742.0] * 15 + [720.0]
    found = evaluate("trend-breakdown-short-4h", closes)
    assert found
    c = found[-1]
    assert float(c.suggested_stop) > float(c.suggested_entry) > float(c.suggested_target)


def test_no_long_against_the_trend():
    assert not evaluate("trend-breakout-4h", [1000.0 - i for i in range(260)] + [741.0, 742.0] * 15 + [800.0])


def test_atr_only_ignores_a_near_htf_level():
    p = preset_by_slug("trend-breakout-4h")
    row = {"close": 100.0, "atr": 2.0, "interval_end_exclusive_ns": 10}
    htf = pl.DataFrame({"interval_end_exclusive_ns": [5], "donchian_upper": [104.0], "bb_upper": [104.0]})
    entry, stop, target, stop_type, target_type, rr, _ = derive_levels(row, p.risk, "LONG", 2, htf)
    assert (float(stop), float(target), target_type) == (94.0, 112.0, "atr_fixed")
    assert rr == pytest.approx(2.0)
    assert stop_type == "atr_fixed"


@pytest.mark.parametrize("slug", ["trend-breakout-4h", "trend-breakdown-short-4h"])
def test_selectable_by_a_profile_on_4h_only(slug):
    assert PRESET_CATALOG[slug].timeframes == ("4h",)
