"""The lake query engine refuses a symbol, timeframe, venue or market that is not a plain lake key, and a window bound or
step that is not an integer, before any path or SQL is built.

DuckDB runs every statement in the string handed to read_parquet, so a value that reaches a file path is an injection
point (the reviewer's COPY payload wrote a file). Every entry point is covered: query_bars_df, count_bars,
get_timestamp_range, detect_gaps, query_derivatives_df and the bars route (which must answer 400, not 500).
Every probe runs in tmp_path: the working directory is moved there, so a COPY ... TO 'pwned' would land in tmp_path.
"""

from pathlib import Path

import duckdb
import pytest
from cryptonite_api.routes.bars import router as bars_router
from cryptonite_scanner.resampler import ONE_MINUTE_NS
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_schemas.identifiers import to_nautilus_bar_type, to_spot_instrument_id
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.queries import AnalyticsQueryEngine
from fastapi import FastAPI
from fastapi.testclient import TestClient

DAY0 = 1_790_812_800 * 10**9  # 2026-10-01 00:00:00 UTC

REPORTER_PAYLOAD = "BTCUSDT/*/*'); COPY (SELECT 42 AS x) TO 'pwned'; SELECT * FROM read_parquet('NOPE"
HOSTILE_SYMBOLS = [
    REPORTER_PAYLOAD,
    "BTC'USDT",
    "BTCUSDT;DROP TABLE x",
    "BTCUSDT/../../etc",
    "BTCUSDT/*",  # a glob that matches every seeded BTCUSDT file
    "BTCUSDT/*/*",
    "..",
    "BTC USDT",
    "BTCUSDT\n",
    "BTCUSDT\x00",
    " BTCUSDT",
    "BTC*",
    "BTCUSDT​",  # zero-width space
    "BTCUSDT-PERP",  # a perpetual id is not a lake key
    "BTCUSDT-PERP.BINANCE",
    "BTCUSDT.OTHER",
    "BTCUSDT.BINANCE.BINANCE",
    "BTCUSDTТ",  # Cyrillic capital te: a look-alike for T
    "ＢTCUSDT",  # fullwidth capital B
    "BTCUSDı",  # dotless i: upper() turns it into ASCII I, so only a check before upper() refuses it
    "B",  # too short
    "B" * 31,  # too long
    "",
]
HOSTILE_TIMEFRAMES = ["1m'", "1m/../x", "2m", "1M", "1H", "", "1m; SELECT 1", "1w", "1m\x00"]


def _write_minutes(lake: Path, symbol: str, n: int) -> None:
    inst = to_spot_instrument_id(symbol)
    bar_type = str(to_nautilus_bar_type(inst, "1m"))
    bars = []
    for i in range(n):
        open_ms = (DAY0 + i * ONE_MINUTE_NS) // 1_000_000
        raw = RawBinanceKline(
            open_time_ms=open_ms, close_time_ms=open_ms + 59_999, open="100", high="101", low="99", close="100.5",
            volume="5", quote_volume="500", trades_count=1, taker_buy_base_volume="0", taker_buy_quote_volume="0",
            is_closed=True,
        )
        bars.append(EnrichedBar.from_binance_raw(raw, inst, bar_type, 2, 5, received_at_ns=(open_ms + 59_675) * 10**6))
    AnalyticsLakeWriter(lake).write_batch(bars, symbol=symbol, timeframe="1m", batch_id="hf-duckdb-validation")


def _seed_derivatives(lake: Path) -> None:
    """One derivatives file, so a payload that reaches the SQL gets past the first statement's file read."""
    d = lake / "derivatives" / "binance" / "perpetual" / "1m" / "BTCUSDT"
    d.mkdir(parents=True)
    duckdb.sql(f"COPY (SELECT 1::BIGINT AS ts_event_ns, 2.0::DOUBLE AS funding_rate) TO '{d}/part.parquet'")


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def lake(tmp_path) -> Path:
    """A lake with three BTCUSDT minutes, so a payload glob such as BTCUSDT/*/* would match real files."""
    path = tmp_path / "lake"
    _write_minutes(path, "BTCUSDT", 3)
    return path


@pytest.fixture
def cwd(tmp_path, monkeypatch) -> Path:
    """The working directory a COPY ... TO 'pwned' would write into, empty before each test."""
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.mark.parametrize("symbol", HOSTILE_SYMBOLS)
def test_query_bars_df_refuses_a_hostile_symbol_before_any_sql(tmp_path, lake, cwd, symbol):
    before = _files(tmp_path)
    engine = AnalyticsQueryEngine(lake)
    with pytest.raises(ValueError, match="^invalid symbol$"):
        engine.query_bars_df(symbol=symbol, timeframe="1m")
    assert _files(tmp_path) == before


@pytest.mark.parametrize("symbol", HOSTILE_SYMBOLS)
def test_count_bars_timestamp_range_gaps_and_derivatives_refuse_a_hostile_symbol(tmp_path, lake, cwd, symbol):
    before = _files(tmp_path)
    engine = AnalyticsQueryEngine(lake)
    with pytest.raises(ValueError, match="^invalid symbol$"):
        engine.count_bars(symbol)
    with pytest.raises(ValueError, match="^invalid symbol$"):
        engine.get_timestamp_range(symbol)
    with pytest.raises(ValueError, match="^invalid symbol$"):
        engine.detect_gaps(symbol)
    with pytest.raises(ValueError, match="^invalid symbol$"):
        engine.query_derivatives_df(symbol)
    assert _files(tmp_path) == before


def test_a_megabyte_symbol_is_refused_without_building_a_path(tmp_path, cwd):
    engine = AnalyticsQueryEngine(tmp_path / "lake")
    with pytest.raises(ValueError, match="^invalid symbol$"):
        engine.query_bars_df(symbol="B" * 1_000_000, timeframe="1m")
    assert list(cwd.iterdir()) == []


@pytest.mark.parametrize("timeframe", HOSTILE_TIMEFRAMES)
def test_query_bars_df_refuses_a_timeframe_outside_the_lake_set(tmp_path, lake, cwd, timeframe):
    before = _files(tmp_path)
    engine = AnalyticsQueryEngine(lake)
    with pytest.raises(ValueError, match="^invalid timeframe$"):
        engine.query_bars_df(symbol="BTCUSDT", timeframe=timeframe)
    with pytest.raises(ValueError, match="^invalid timeframe$"):
        engine.count_bars("BTCUSDT", timeframe)
    with pytest.raises(ValueError, match="^invalid timeframe$"):
        engine.detect_gaps("BTCUSDT", timeframe)
    assert _files(tmp_path) == before


@pytest.mark.parametrize("venue, market_type", [
    ("binance/../x", "spot"),
    ("binance", "spot/../../x"),
    ("kraken", "spot"),
    ("binance", "perpetual"),
])
def test_the_path_builder_refuses_a_venue_or_market_outside_the_lake_set(tmp_path, venue, market_type):
    engine = AnalyticsQueryEngine(tmp_path / "lake")
    with pytest.raises(ValueError):
        engine._get_glob_path("BTCUSDT", "1m", venue=venue, market_type=market_type)


def test_the_lake_venue_and_market_still_build_the_same_path(tmp_path):
    engine = AnalyticsQueryEngine(tmp_path / "lake")
    assert engine._get_glob_path("BTCUSDT", "1m", venue="BINANCE", market_type="SPOT") == engine._get_glob_path("BTCUSDT", "1m")


@pytest.mark.parametrize("start_ns, end_ns", [
    ("0; COPY (SELECT 42 AS x) TO 'deriv_pwned'; --", None),
    (None, "0 OR 1=1"),
    ("1.5e3", None),
])
def test_derivatives_bounds_must_be_integers(tmp_path, lake, cwd, start_ns, end_ns):
    _seed_derivatives(lake)
    before = _files(tmp_path)
    engine = AnalyticsQueryEngine(lake)
    with pytest.raises(ValueError):
        engine.query_derivatives_df("BTCUSDT", start_ns=start_ns, end_ns=end_ns)
    assert _files(tmp_path) == before


def test_detect_gaps_step_must_be_an_integer(tmp_path, lake, cwd):
    before = _files(tmp_path)
    engine = AnalyticsQueryEngine(lake)
    with pytest.raises(ValueError):
        engine.detect_gaps("BTCUSDT", "1m", step_seconds="60; COPY (SELECT 42 AS x) TO 'gap_pwned'; --")
    assert _files(tmp_path) == before


def test_a_valid_symbol_is_normalised_and_reads_the_same_bars(lake):
    engine = AnalyticsQueryEngine(lake)
    full = engine.query_bars_df(symbol="BTCUSDT", timeframe="1m")
    assert full.height == 3
    assert engine.query_bars_df(symbol="btcusdt", timeframe="1m").equals(full)
    assert engine.query_bars_df(symbol="BTCUSDT.BINANCE", timeframe="1m").equals(full)
    assert engine.query_bars_df(symbol="btcusdt.binance", timeframe="1m").equals(full)
    assert engine.count_bars("btcusdt.binance") == 3


def test_an_empty_lake_answers_a_valid_symbol_with_no_rows(tmp_path):
    engine = AnalyticsQueryEngine(tmp_path / "lake")
    assert engine.query_bars_df(symbol="ethusdt", timeframe="1h").is_empty()


# The route upper-cases the symbol before the engine sees it (routes/bars.py, unchanged by this hotfix), so the
# dotless-i look-alike becomes an ASCII 'BTCUSDI': a valid key that holds no data, answered 200 with no rows.
# The engine itself refuses it (test_query_bars_df_refuses_a_hostile_symbol_before_any_sql).
ROUTE_HOSTILE_SYMBOLS = [s for s in HOSTILE_SYMBOLS if s != "BTCUSDı"]


@pytest.mark.parametrize("symbol", ROUTE_HOSTILE_SYMBOLS)
def test_the_bars_route_answers_400_for_a_hostile_symbol_and_writes_nothing(tmp_path, lake, cwd, symbol):
    before = _files(tmp_path)
    app = FastAPI()
    app.include_router(bars_router)
    app.state.query_engine = AnalyticsQueryEngine(lake)
    client = TestClient(app)

    bad = client.get("/api/v1/bars", params={"symbol": symbol, "timeframe": "1m"})
    assert bad.status_code == 400 and bad.json()["detail"] == "invalid symbol"
    assert _files(tmp_path) == before


def test_the_bars_route_serves_a_valid_symbol(lake):
    app = FastAPI()
    app.include_router(bars_router)
    app.state.query_engine = AnalyticsQueryEngine(lake)
    client = TestClient(app)

    good = client.get("/api/v1/bars", params={"symbol": "btcusdt", "timeframe": "1m"})
    assert good.status_code == 200 and len(good.json()) == 3
