"""B1 / F-C1: lake 1m bars are stamped at their CLOSE; the scanner's higher-timeframe windows must be built
from their OPEN.

The lake stores ``ts_event_ns = ts_init_ns = bar close`` (``EnrichedBar.from_binance_raw``). Everything here
goes through the production read path: Binance kline -> ``EnrichedBar`` -> ``AnalyticsLakeWriter`` ->
``AnalyticsQueryEngine.query_bars_df`` -> ``resample_1m_bars``. A window [W, W + tf) holds the minutes that
OPEN in it, i.e. the lake rows with ``W < ts_event_ns <= W + tf``, and it exists only once the minute closing
at ``W + tf`` is in the lake.

The fixture test reads one real day (2026-10-04, BTC and ETH) exported from the local lake by
``tests/fixtures/b1_lake/export_b1_fixture.py``; it fails, never skips, when the fixture is missing.
"""

from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from cryptonite_scanner.resampler import ONE_MINUTE_NS, resample_1m_bars
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_schemas.identifiers import to_nautilus_bar_type, to_spot_instrument_id
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.queries import AnalyticsQueryEngine

HOUR_NS = 60 * ONE_MINUTE_NS
DAY_NS = 24 * HOUR_NS
T0_NS = 1_791_104_400 * 10**9  # 2026-10-04 09:00:00 UTC, an hour boundary
FIXTURE_LAKE = Path(__file__).parent / "fixtures" / "b1_lake"
FIXTURE_DAY_NS = 1_791_072_000 * 10**9  # 2026-10-04 00:00:00 UTC
OHLC = ("open", "high", "low", "close")


# --- synthetic lake ---------------------------------------------------------------------------------

def _minute(i: int) -> dict[str, float]:
    """Minute i after T0: distinct, exactly representable values so every aggregate is hand-checkable."""
    base = 1000.0 + i
    return {"open": base, "high": base + 0.75, "low": base - 0.25, "close": base + 0.5, "volume": float(i + 1)}


def _synthetic_minutes(n: int = 180) -> list[dict[str, float]]:
    """Minutes opening 09:00 .. 11:59 with extremes planted on the hour boundaries, where a one-minute
    shift moves them into the wrong window."""
    rows = [_minute(i) for i in range(n)]
    rows[59]["high"] = 5000.0   # opens 09:59, closes 10:00: belongs to 09:00
    rows[60]["low"] = 10.0      # opens 10:00, closes 10:01: belongs to 10:00
    rows[119]["high"] = 6000.0  # opens 10:59, closes 11:00: belongs to 10:00
    return rows


def _write_lake(lake: Path, rows: list[dict[str, float]], first_open_ns: int = T0_NS, symbol: str = "BTCUSDT") -> None:
    """Write minutes the way ingest does: a closed Binance kline converted by ``EnrichedBar.from_binance_raw``."""
    inst = to_spot_instrument_id(symbol)
    bar_type = str(to_nautilus_bar_type(inst, "1m"))
    bars = []
    for i, r in enumerate(rows):
        open_ms = (first_open_ns + i * ONE_MINUTE_NS) // 1_000_000
        raw = RawBinanceKline(
            open_time_ms=open_ms, close_time_ms=open_ms + 59_999,
            open=str(r["open"]), high=str(r["high"]), low=str(r["low"]), close=str(r["close"]),
            volume=str(r["volume"]), quote_volume=str(r["volume"] * r["close"]), trades_count=i + 1,
            taker_buy_base_volume="0", taker_buy_quote_volume="0", is_closed=True,
        )
        # live klines arrive a few hundred ms before the rounded close (F-C1: received 02:34:59.675, ts 02:35:00)
        received = (open_ms + 59_675) * 1_000_000
        bars.append(EnrichedBar.from_binance_raw(raw, inst, bar_type, 2, 5, received_at_ns=received))
    AnalyticsLakeWriter(lake).write_batch(bars, symbol=symbol, timeframe="1m", batch_id="b1-synthetic")


def _expected_window(rows: list[dict[str, float]], first: int, n: int) -> dict[str, float]:
    w = rows[first:first + n]
    return {"open": w[0]["open"], "high": max(r["high"] for r in w), "low": min(r["low"] for r in w),
            "close": w[-1]["close"], "volume": sum(r["volume"] for r in w)}


@pytest.fixture
def synthetic_lake(tmp_path):
    rows = _synthetic_minutes()
    _write_lake(tmp_path, rows)
    return AnalyticsQueryEngine(tmp_path), rows


# --- 1. hand-computed 1h bars through the lake path --------------------------------------------------

def test_f_c1_lake_rows_are_stamped_at_the_close(synthetic_lake):
    """Precondition the fix relies on (unchanged by it): ingest stamps ts_event/ts_init at the close."""
    engine, _ = synthetic_lake
    df = engine.query_bars_df("BTCUSDT", "1m")
    assert df.height == 180
    assert int(df["ts_event_ns"][0]) == T0_NS + ONE_MINUTE_NS
    assert int(df["ts_init_ns"][0]) == T0_NS + ONE_MINUTE_NS


def test_f_c1_query_bars_df_returns_the_open_of_each_1m_lake_row(synthetic_lake):
    """B1 change 1: ``query_bars_df`` gives ``bar_open_ns = ts_event_ns - 60 s`` so callers never guess."""
    engine, _ = synthetic_lake
    df = engine.query_bars_df("BTCUSDT", "1m")
    assert "bar_open_ns" in df.columns, f"query_bars_df returns no bar_open_ns; columns: {df.columns}"
    assert df["bar_open_ns"].cast(pl.Int64).to_list() == [T0_NS + i * ONE_MINUTE_NS for i in range(180)]
    # close-time semantics stay for the callers that use them (outcomes, reflection, betas)
    assert df["ts_event_ns"].cast(pl.Int64).to_list() == [T0_NS + (i + 1) * ONE_MINUTE_NS for i in range(180)]


def test_f_c1_resampler_refuses_a_frame_without_bar_open_ns(synthetic_lake):
    """B1 change 2: no silent fallback to ``ts_init_ns`` / ``ts_event_ns`` (both are the CLOSE) as the open."""
    engine, _ = synthetic_lake
    lake_cols = engine.query_bars_df("BTCUSDT", "1m").drop("bar_open_ns", strict=False)
    with pytest.raises(ValueError, match="bar_open_ns"):
        resample_1m_bars(lake_cols, "1h")


def test_f_c1_lake_1h_bars_equal_hand_computed_ohlc_with_the_correct_open_time(synthetic_lake):
    engine, rows = synthetic_lake
    res = resample_1m_bars(engine.query_bars_df("BTCUSDT", "1m"), "1h")
    got = res.select("bar_open_ns", "bar_close_inclusive_ns", "interval_end_exclusive_ns", *OHLC, "volume").to_dicts()
    expected = [
        {"bar_open_ns": T0_NS + h * HOUR_NS, "bar_close_inclusive_ns": T0_NS + (h + 1) * HOUR_NS - 1_000_000,
         "interval_end_exclusive_ns": T0_NS + (h + 1) * HOUR_NS, **_expected_window(rows, 60 * h, 60)}
        for h in range(3)
    ]
    # spelled out for the planted extremes: 09:00 = open 1000, high 5000 (minute 09:59), close 1059.5;
    # 10:00 = open 1060, high 6000 (minute 10:59), low 10 (minute 10:00), close 1119.5
    assert expected[0] == {**expected[0], "open": 1000.0, "high": 5000.0, "low": 999.75, "close": 1059.5, "volume": 1830.0}
    assert expected[1] == {**expected[1], "open": 1060.0, "high": 6000.0, "low": 10.0, "close": 1119.5, "volume": 5430.0}
    stamps = ("bar_open_ns", "bar_close_inclusive_ns", "interval_end_exclusive_ns")
    assert [{k: g[k] for k in stamps} for g in got] == [{k: e[k] for k in stamps} for e in expected]
    # Polars' decimal(38,18) -> Float64 cast is not always correctly rounded (999.75 -> 999.7500000000001)
    values = (*OHLC, "volume")
    assert [{k: g[k] for k in values} for g in got] == [pytest.approx({k: e[k] for k in values}, rel=1e-12)
                                                       for e in expected]


def test_f_c1_1h_close_is_the_close_of_the_minute_ending_at_the_hour_boundary(synthetic_lake):
    engine, _ = synthetic_lake
    df_1m = engine.query_bars_df("BTCUSDT", "1m")
    res = resample_1m_bars(df_1m, "1h")
    closes_at = {int(r["ts_event_ns"]): float(r["close"]) for r in df_1m.select("ts_event_ns", "close").to_dicts()}
    assert res.height == 3
    for bar in res.to_dicts():
        end = bar["interval_end_exclusive_ns"]
        assert end % HOUR_NS == 0
        assert bar["close"] == closes_at[end], f"1h bar opening {bar['bar_open_ns']} closes with the minute ending {end}"


def test_f_c1_1m_target_reports_the_open_not_the_close(synthetic_lake):
    """``/api/v1/bars?timeframe=1m`` draws ``bar_open_ns``: the minute closing at 09:01 opened at 09:00."""
    engine, rows = synthetic_lake
    res = resample_1m_bars(engine.query_bars_df("BTCUSDT", "1m"), "1m")
    first = res.row(0, named=True)
    assert first["bar_open_ns"] == T0_NS
    assert first["interval_end_exclusive_ns"] == T0_NS + ONE_MINUTE_NS
    assert first["close"] == rows[0]["close"]


# --- 2. a window is complete only after its last minute closes ---------------------------------------

@pytest.mark.parametrize("tf,minutes", [("15m", 15), ("1h", 60)])
def test_f_c1_window_is_not_emitted_while_its_last_minute_is_open(tmp_path, tf, minutes):
    """The second window (09:15 for 15m, 10:00 for 1h): every minute but its last has closed, so it must not
    exist yet (F-C1: it was emitted when the second-to-last minute closed). Once the last minute closes, the
    window appears and its close is that minute's close."""
    rows = _synthetic_minutes()
    _write_lake(tmp_path, rows)
    engine = AnalyticsQueryEngine(tmp_path)
    window_open = T0_NS + minutes * ONE_MINUTE_NS
    last_close = window_open + minutes * ONE_MINUTE_NS

    before = resample_1m_bars(engine.query_bars_df("BTCUSDT", "1m", end_ns=last_close - ONE_MINUTE_NS), tf)
    early = before.filter(pl.col("interval_end_exclusive_ns") >= last_close)
    assert early.is_empty(), f"{tf} window(s) emitted before their last minute closed: {early.to_dicts()}"

    after = resample_1m_bars(engine.query_bars_df("BTCUSDT", "1m", end_ns=last_close), tf)
    window = after.filter(pl.col("bar_open_ns") == window_open)
    assert window.height == 1, f"{tf} window opening {window_open} missing once its last minute closed: {after.to_dicts()}"
    assert window["close"][0] == rows[2 * minutes - 1]["close"]
    assert window["interval_end_exclusive_ns"][0] == last_close


@pytest.mark.parametrize("tf,minutes", [("15m", 15), ("1h", 60)])
def test_f_c1_property_emitted_windows_are_exactly_the_closed_ones_at_every_minute(synthetic_lake, tf, minutes):
    """Replay the lake minute by minute (everything closed by ``now``): at every ``now`` the resampler emits
    exactly the windows [W, W + tf) with W >= 09:00 and W + tf <= now: none early, none missing."""
    engine, _ = synthetic_lake
    step = minutes * ONE_MINUTE_NS
    early, wrong = [], []
    for k in range(1, 181):
        now = T0_NS + k * ONE_MINUTE_NS
        res = resample_1m_bars(engine.query_bars_df("BTCUSDT", "1m", end_ns=now), tf)
        got = res["bar_open_ns"].to_list() if not res.is_empty() else []
        ends = res["interval_end_exclusive_ns"].to_list() if not res.is_empty() else []
        early += [(f"now=+{k}m", f"ends +{(e - T0_NS) // ONE_MINUTE_NS}m") for e in ends if e > now]
        if got != list(range(T0_NS, now - step + 1, step)):
            wrong.append(f"now=+{k}m")
    assert not early, f"{tf}: {len(early)} windows emitted before they closed, first {early[:3]}"
    assert not wrong, f"{tf}: emitted set differs from the closed windows at {len(wrong)} of 180 minutes, first {wrong[:3]}"


# --- 3. regression against ingest's native 1h bars (one real lake day) -------------------------------

def _fixture_engine() -> AnalyticsQueryEngine:
    files = list(FIXTURE_LAKE.glob("binance_klines_enriched/binance/spot/*/*/date=*/*.parquet"))
    assert files, f"B1 fixture missing under {FIXTURE_LAKE}; run tests/fixtures/b1_lake/export_b1_fixture.py"
    return AnalyticsQueryEngine(FIXTURE_LAKE)


def _native_1h(engine: AnalyticsQueryEngine, symbol: str) -> pl.DataFrame:
    """Ingest's native 1h bars (Binance 1h klines, also stamped at the close) keyed by their open."""
    nat = engine.query_bars_df(symbol, "1h")
    assert nat.height == 24, f"{symbol}: expected 24 native 1h bars in the fixture, got {nat.height}"
    # every native bar was recorded after it closed (an open kline would be no oracle)
    assert nat.filter(pl.col("received_at_ns") + 5 * 10**9 < pl.col("ts_event_ns")).is_empty()
    return nat.select(
        bar_open_ns=pl.col("ts_event_ns").cast(pl.Int64) - HOUR_NS,
        native_end_ns=pl.col("ts_event_ns").cast(pl.Int64),
        **{f"native_{c}": pl.col(c).cast(pl.Float64) for c in (*OHLC, "volume")},
    )


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT"])
def test_f_c1_lake_day_resampled_to_1h_matches_native_1h_bars(symbol):
    engine = _fixture_engine()
    df_1m = engine.query_bars_df(symbol, "1m")
    assert df_1m.height == 1500, f"{symbol}: expected 1500 1m rows in the fixture, got {df_1m.height}"
    native = _native_1h(engine, symbol)
    res = resample_1m_bars(df_1m, "1h")

    joined = native.join(res, on="bar_open_ns", how="left")
    missing = joined.filter(pl.col("close").is_null())["bar_open_ns"].to_list()
    assert not missing, f"{symbol}: no resampled bar for native 1h opens {missing}"
    assert joined["interval_end_exclusive_ns"].to_list() == joined["native_end_ns"].to_list()

    mismatches = {c: joined.filter(pl.col(c) != pl.col(f"native_{c}"))["bar_open_ns"].to_list() for c in OHLC}
    summary = {c: len(v) for c, v in mismatches.items()}
    assert summary == {c: 0 for c in OHLC}, f"{symbol} 2026-10-04: OHLC mismatches per field {summary}"
    vol = joined.filter((pl.col("volume") - pl.col("native_volume")).abs() > 1e-9 * pl.col("native_volume"))
    assert vol.is_empty(), f"{symbol}: volume mismatches at {vol['bar_open_ns'].to_list()}"
    print(f"{symbol}: equivalence cases executed: {joined.height} hours x {len(OHLC) + 1} fields")


def test_f_c1_fixture_day_starts_on_its_first_hour():
    """The lake day's first resampled 1h bar opens at 00:00 and ends with the 1m row stamped 01:00."""
    engine = _fixture_engine()
    df_1m = engine.query_bars_df("BTCUSDT", "1m")
    res = resample_1m_bars(df_1m, "1h").filter(pl.col("bar_open_ns") >= FIXTURE_DAY_NS)
    first = res.row(0, named=True)
    close_at_0100 = df_1m.filter(pl.col("ts_event_ns") == FIXTURE_DAY_NS + HOUR_NS)["close"][0]
    assert first["bar_open_ns"] == FIXTURE_DAY_NS
    assert Decimal(str(first["close"])) == close_at_0100
