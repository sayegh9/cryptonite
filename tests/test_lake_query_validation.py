"""The lake query engine refuses a symbol or timeframe that is not a plain lake key, before any path or SQL is built.

DuckDB runs every statement in the string handed to read_parquet, so a value that reaches a file path is an injection
point (the reviewer's COPY payload wrote a file). Every entry point is covered: query_bars_df, count_bars,
query_derivatives_df, the bars route (which must answer 400, not 500), and the two Brain tools.
"""

from pathlib import Path

import pytest
from cryptonite_agents import tools
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
    "..",
    "BTC USDT",
    "BTCUSDT\n",
    " BTCUSDT",
    "BTC*",
    "BTCUSDT-PERP",  # a perpetual id is not a lake key
    "BTCUSDT.OTHER",
    "B",  # too short
    "B" * 31,  # too long
    "",
]
HOSTILE_TIMEFRAMES = ["1m'", "1m/../x", "2m", "1M", "1H", "", "1m; SELECT 1"]
# The tools first keep the text before the first dot or dash (perpetual ids map to their base symbol, as beta_symbol
# does), so whitespace and suffixes are normalised there. What reaches the lake from a tool is always a plain key.
TOOL_HOSTILE_SYMBOLS = [s for s in HOSTILE_SYMBOLS if s not in {" BTCUSDT", "BTCUSDT\n", "BTCUSDT.OTHER",
                                                                 "BTCUSDT-PERP"}]


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
    AnalyticsLakeWriter(lake).write_batch(bars, symbol=symbol, timeframe="1m", batch_id="ws2c-validation")


@pytest.fixture
def cwd(tmp_path, monkeypatch) -> Path:
    """The working directory a COPY ... TO 'pwned' would write into, empty before each test."""
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.mark.parametrize("symbol", HOSTILE_SYMBOLS)
def test_query_bars_df_refuses_a_hostile_symbol_before_any_sql(tmp_path, cwd, symbol):
    engine = AnalyticsQueryEngine(tmp_path / "lake")
    with pytest.raises(ValueError, match="^invalid symbol$"):
        engine.query_bars_df(symbol=symbol, timeframe="1m")
    assert list(cwd.iterdir()) == [] and not (tmp_path / "lake").exists()


@pytest.mark.parametrize("symbol", HOSTILE_SYMBOLS)
def test_count_bars_and_derivatives_refuse_a_hostile_symbol(tmp_path, symbol):
    engine = AnalyticsQueryEngine(tmp_path / "lake")
    with pytest.raises(ValueError, match="^invalid symbol$"):
        engine.count_bars(symbol)
    with pytest.raises(ValueError, match="^invalid symbol$"):
        engine.query_derivatives_df(symbol)


@pytest.mark.parametrize("timeframe", HOSTILE_TIMEFRAMES)
def test_query_bars_df_refuses_a_timeframe_outside_the_lake_set(tmp_path, cwd, timeframe):
    engine = AnalyticsQueryEngine(tmp_path / "lake")
    with pytest.raises(ValueError, match="^invalid timeframe$"):
        engine.query_bars_df(symbol="BTCUSDT", timeframe=timeframe)
    assert list(cwd.iterdir()) == []


def test_a_valid_symbol_is_normalised_and_reads_the_same_bars(tmp_path):
    lake = tmp_path / "lake"
    _write_minutes(lake, "BTCUSDT", 3)
    engine = AnalyticsQueryEngine(lake)
    full = engine.query_bars_df(symbol="BTCUSDT", timeframe="1m")
    assert full.height == 3
    assert engine.query_bars_df(symbol="btcusdt", timeframe="1m").equals(full)
    assert engine.query_bars_df(symbol="BTCUSDT.BINANCE", timeframe="1m").equals(full)
    assert engine.count_bars("btcusdt.binance") == 3


def test_an_empty_lake_answers_a_valid_symbol_with_no_rows(tmp_path):
    engine = AnalyticsQueryEngine(tmp_path / "lake")
    assert engine.query_bars_df(symbol="ethusdt", timeframe="1h").is_empty()


def test_the_bars_route_answers_400_for_a_hostile_symbol_and_serves_a_valid_one(tmp_path):
    lake = tmp_path / "lake"
    _write_minutes(lake, "BTCUSDT", 5)
    app = FastAPI()
    app.include_router(bars_router)
    app.state.query_engine = AnalyticsQueryEngine(lake)
    client = TestClient(app)

    bad = client.get("/api/v1/bars", params={"symbol": "BTC'USDT", "timeframe": "1m"})
    assert bad.status_code == 400 and bad.json()["detail"] == "invalid symbol"

    good = client.get("/api/v1/bars", params={"symbol": "btcusdt", "timeframe": "1m"})
    assert good.status_code == 200 and len(good.json()) == 5


@pytest.mark.parametrize("symbol", TOOL_HOSTILE_SYMBOLS)
def test_the_regime_tool_returns_invalid_symbol_and_reads_nothing(monkeypatch, cwd, symbol):
    def no_read(self, *args, **kwargs):
        raise AssertionError("the lake must not be read for an invalid symbol")

    monkeypatch.setattr(AnalyticsQueryEngine, "query_bars_df", no_read)
    assert tools.get_market_regime(symbol) == '{"error": "invalid symbol"}'
    assert list(cwd.iterdir()) == []


@pytest.mark.parametrize("symbol", TOOL_HOSTILE_SYMBOLS)
def test_the_derivatives_tool_returns_invalid_symbol_and_reads_nothing(monkeypatch, symbol):
    def no_redis(key):
        raise AssertionError("Redis must not be read for an invalid symbol")

    def no_sql(statement, params=None):
        raise AssertionError("SQL must not run for an invalid symbol")

    monkeypatch.setattr(tools, "_redis_get", no_redis)
    monkeypatch.setattr(tools, "_readonly_rows", no_sql)
    assert tools.get_derivatives_context(symbol) == '{"error": "invalid symbol"}'


def test_valid_symbols_still_work_through_the_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYTICS_LAKE_PATH", str(tmp_path / "lake"))
    regime = tools.get_market_regime("btcusdt")
    assert '"reason": "no 1h bars in the analytics lake for BTCUSDT"' in regime


def test_a_lake_read_failure_is_a_short_reason_with_no_paths_or_driver_text(monkeypatch, tmp_path):
    def broken(self, *args, **kwargs):
        raise OSError(f"IO Error: cannot open file {tmp_path}/secret/part.parquet")

    monkeypatch.setattr(AnalyticsQueryEngine, "query_bars_df", broken)
    monkeypatch.setenv("ANALYTICS_LAKE_PATH", str(tmp_path))
    out = tools.get_market_regime("BTCUSDT")
    assert out == '{"regime": null, "reason": "lake read failed"}'
    assert str(tmp_path) not in out and "IO Error" not in out
