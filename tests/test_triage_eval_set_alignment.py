"""B1 / F-C1: the triage eval-set builder renders briefs from correctly aligned bars. At a sampled 15m
close, the last 15m bar ends exactly there and closes with the 1m row stamped at that close (before B1 it
was built one minute early, from the previous window's last minute)."""

import importlib.util
from pathlib import Path

import pytest
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.queries import AnalyticsQueryEngine

MIN_NS = 60 * 10**9
T0_NS = 1_791_072_000 * 10**9  # 2026-10-04 00:00:00 UTC


def load_builder():
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_triage_eval_set.py"
    spec = importlib.util.spec_from_file_location("build_triage_eval_set_b1", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_frames_at_ends_on_the_sampled_close(tmp_path):
    bars = []
    for i in range(62 * 60):  # 62 h of minutes: frames_at needs >= 60 bars on 15m and on 1h
        open_ms = (T0_NS + i * MIN_NS) // 10**6
        price = 100 + (i % 97) * 0.5 + i * 0.01
        raw = RawBinanceKline(open_time_ms=open_ms, close_time_ms=open_ms + 59_999, open=f"{price:.2f}",
                              high=f"{price + 1:.2f}", low=f"{price - 1:.2f}", close=f"{price + 0.25:.2f}",
                              volume="1", quote_volume="1", trades_count=1, taker_buy_base_volume="0",
                              taker_buy_quote_volume="0")
        bars.append(EnrichedBar.from_binance_raw(raw, "BTCUSDT.BINANCE", "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL",
                                                 2, 5, received_at_ns=open_ms * 10**6 + MIN_NS))
    AnalyticsLakeWriter(tmp_path).write_batch(bars, symbol="BTCUSDT", batch_id="b1")
    df = AnalyticsQueryEngine(tmp_path).query_bars_df("BTCUSDT", "1m").sort("bar_open_ns")

    end_ns = T0_NS + 61 * 3600 * 10**9 + 45 * MIN_NS  # a 15m close: day 3, 13:45
    frames = load_builder().frames_at(df, end_ns)
    assert frames is not None
    last15 = frames["15m"].tail(1).to_dicts()[0]
    assert last15["bar_open_ns"] == end_ns - 15 * MIN_NS
    assert last15["interval_end_exclusive_ns"] == end_ns
    close_at_end = float(df.filter(df["ts_event_ns"].cast(int) == end_ns)["close"][0])
    assert last15["close"] == pytest.approx(close_at_end, rel=1e-12)  # decimal->float casts may differ by 1 ulp
    last1h = frames["1h"].tail(1).to_dicts()[0]
    assert last1h["interval_end_exclusive_ns"] == T0_NS + 61 * 3600 * 10**9


def test_frames_at_refuses_a_sample_that_is_not_an_exact_15m_close(tmp_path):
    """No slack: 30 s after a 15m close is not a 15m close (the old 60 s tolerance accepted it)."""
    bars = []
    for i in range(62 * 60):
        open_ms = (T0_NS + i * MIN_NS) // 10**6
        raw = RawBinanceKline(open_time_ms=open_ms, close_time_ms=open_ms + 59_999, open="100", high="101",
                              low="99", close="100.5", volume="1", quote_volume="1", trades_count=1,
                              taker_buy_base_volume="0", taker_buy_quote_volume="0")
        bars.append(EnrichedBar.from_binance_raw(raw, "BTCUSDT.BINANCE", "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL",
                                                 2, 5, received_at_ns=open_ms * 10**6 + MIN_NS))
    AnalyticsLakeWriter(tmp_path).write_batch(bars, symbol="BTCUSDT", batch_id="b1")
    df = AnalyticsQueryEngine(tmp_path).query_bars_df("BTCUSDT", "1m").sort("bar_open_ns")
    close_ns = T0_NS + 61 * 3600 * 10**9 + 45 * MIN_NS
    builder = load_builder()
    assert builder.frames_at(df, close_ns) is not None
    assert builder.frames_at(df, close_ns + 30 * 10**9) is None
