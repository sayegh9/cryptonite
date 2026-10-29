"""D-P4-10: scalp presets are executable, mirror-symmetric and selective."""

from __future__ import annotations

import random
from uuid import uuid4

import polars as pl
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.presets import preset_by_slug
from cryptonite_schemas.profile import PRESETS


def _engineered_frame(*, downtrend: bool) -> pl.DataFrame:
    """Two indicator-enriched bars where only the matching scalp direction fires."""
    if downtrend:
        previous = {
            "close": 95.0, "open": 96.0, "rsi": 65.0, "ema_20": 97.0, "ema_50": 100.0,
            "ema_200": 110.0, "htf_close": 80.0, "htf_ema_50": 90.0,
        }
        current = {
            "close": 90.0, "open": 91.0, "rsi": 58.0, "ema_20": 95.0, "ema_50": 99.0,
            "ema_200": 109.0, "htf_close": 81.0, "htf_ema_50": 90.0,
        }
    else:
        previous = {
            "close": 105.0, "open": 104.0, "rsi": 35.0, "ema_20": 103.0, "ema_50": 100.0,
            "ema_200": 90.0, "htf_close": 120.0, "htf_ema_50": 110.0,
        }
        current = {
            "close": 110.0, "open": 109.0, "rsi": 42.0, "ema_20": 105.0, "ema_50": 101.0,
            "ema_200": 91.0, "htf_close": 119.0, "htf_ema_50": 110.0,
        }

    common = {
        "high": 112.0, "low": 88.0, "volume": 100.0, "quote_volume": 10_000.0,
        "trades_count": 10, "atr": 2.0, "volume_ratio": 1.0, "bb_lower": 80.0,
        "bb_upper": 120.0, "donchian_lower": 80.0, "donchian_upper": 120.0,
    }
    rows = []
    for i, values in enumerate((previous, current)):
        rows.append({
            **common,
            **values,
            "bar_open_ns": i * 3 + 1,
            "bar_close_inclusive_ns": i * 3 + 2,
            "interval_end_exclusive_ns": i * 3 + 3,
        })
    return pl.DataFrame(rows)


def _evaluate(preset, frame: pl.DataFrame):
    return ScannerEngine.evaluate(
        df=frame,
        rule_groups=preset.rules,
        risk_config=preset.risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        price_precision=2,
    )


def test_uptrend_fires_long_only_and_downtrend_fires_short_only():
    long_preset = preset_by_slug("scalp-multi-tf")
    short_preset = preset_by_slug("scalp-multi-tf-short")

    up = _engineered_frame(downtrend=False)
    down = _engineered_frame(downtrend=True)
    assert _evaluate(long_preset, up)
    assert _evaluate(short_preset, up) == []
    assert _evaluate(short_preset, down)
    assert _evaluate(long_preset, down) == []


def _random_walk_frame(*, seed: int, count: int, start_ns: int, interval_seconds: int,
                       mirror: bool = False) -> pl.DataFrame:
    """Generate reproducible OHLCV bars and optionally reflect every price around 1,000."""
    rng = random.Random(seed)
    close = 1_000.0
    rows = []
    for i in range(count):
        open_price = close
        close = max(1.0, close + rng.uniform(-5.0, 5.0))
        high = max(open_price, close) + rng.uniform(0.1, 2.0)
        low = min(open_price, close) - rng.uniform(0.1, 2.0)
        if mirror:
            open_price, close, high, low = 2_000 - open_price, 2_000 - close, 2_000 - low, 2_000 - high
        interval_end = start_ns + (i + 1) * interval_seconds * 1_000_000_000
        rows.append({
            "bar_open_ns": interval_end - interval_seconds * 1_000_000_000,
            "bar_close_inclusive_ns": interval_end - 1,
            "interval_end_exclusive_ns": interval_end,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 100.0 + rng.uniform(0.0, 100.0),
            "quote_volume": 100_000.0,
            "trades_count": 10,
        })
    return pl.DataFrame(rows)


def _candidate_count(preset, base_bars: pl.DataFrame, higher_bars: pl.DataFrame) -> int:
    results = ScannerEngine.evaluate(
        df=base_bars,
        rule_groups=preset.rules,
        risk_config=preset.risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        price_precision=2,
        htf_df=higher_bars,
    )
    cutoff = base_bars["interval_end_exclusive_ns"][-100]
    return sum(candidate.interval_end_exclusive_ns >= cutoff for candidate in results)


def test_symmetric_fixed_random_walk_is_selective_and_balanced():
    # 600 x 15m bars provide a 150-hour base frame. The 1h frame starts earlier so its
    # 200-period EMA and the base indicators are warmed up before the measured last 100.
    base_start = 1_700_000_000_000_000_000
    htf_start = base_start - 250 * 3_600 * 1_000_000_000
    base = _random_walk_frame(seed=2026, count=600, start_ns=base_start, interval_seconds=900)
    base_mirror = _random_walk_frame(seed=2026, count=600, start_ns=base_start, interval_seconds=900, mirror=True)
    htf = _random_walk_frame(seed=9026, count=400, start_ns=htf_start, interval_seconds=3_600)
    htf_mirror = _random_walk_frame(seed=9026, count=400, start_ns=htf_start,
                                    interval_seconds=3_600, mirror=True)

    long_count = _candidate_count(preset_by_slug("scalp-multi-tf"), base, htf)
    short_count = _candidate_count(preset_by_slug("scalp-multi-tf-short"), base_mirror, htf_mirror)
    assert long_count <= 3
    assert short_count <= 3
    assert max(long_count, short_count) <= 3 * max(1, min(long_count, short_count))


def test_profile_preset_registry_keeps_the_existing_twelve_slugs():
    expected = {slug for slugs in PRESETS.values() for slug in slugs}
    assert len(expected) == 12
    # Keep resolution for every contract entry and ensure both scalp slugs remain registered.
    assert {"scalp-multi-tf", "scalp-multi-tf-short"} <= expected
    for slug in expected:
        assert preset_by_slug(slug).slug == slug
