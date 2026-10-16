"""HF-DUCKDB: the model-callable DuckDB tool and the tearsheet reader cannot read, write or load outside the catalog, and
the scanner and outcome loops survive one bad instrument or row.

Every probe runs in pytest tmp_path. CATALOG_PATH and the working directory are monkeypatched there, and HOME too (so an
extension install could only land in tmp_path). Each probe asserts that no file appeared anywhere under tmp_path except
what the test seeded, and that no secret seeded outside the catalog came back in the output. Nothing here touches /app,
the dev DB or data/.
"""

import asyncio
import datetime
import json
import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import asyncpg
import duckdb
import polars as pl
import pytest
from cryptonite_agents import tools
from cryptonite_scanner.resampler import ONE_MINUTE_NS
from cryptonite_scanner_service import worker as worker_mod
from cryptonite_scanner_service.worker import ScannerWorker
from migrated_db import migrated_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "debate_worker"))
import outcomes  # noqa: E402

SECRET_MARKERS = ("TOPSECRET", "LOCALSECRET", "PWNED")  # file contents that must never come back
REFUSED_NOT_A_SELECT = "Security Error: only a single SELECT is allowed"


@dataclass
class Sandbox:
    root: Path      # everything the test may create lives under here
    catalog: Path   # CATALOG_PATH: the only directory the tool may read
    outside: Path   # secrets that sit outside the catalog
    cwd: Path       # the working directory (a relative 'local.env' lives here)


@pytest.fixture
def sandbox(tmp_path, monkeypatch) -> Sandbox:
    catalog = tmp_path / "catalog"
    seg = catalog / "data" / "bars" / "1m" / "BTCUSDT"
    seg.mkdir(parents=True)
    duckdb.sql(f"COPY (SELECT 1700000000000000000::BIGINT AS ts_event_ns, 100.0::DOUBLE AS close) "
               f"TO '{seg}/part.parquet'")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOPSECRET")
    (outside / "secret.csv").write_text("a,b\n7,8\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "local.env").write_text("LOCALSECRET")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(tools, "CATALOG_PATH", str(catalog))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)
    return Sandbox(root=tmp_path, catalog=catalog, outside=outside, cwd=cwd)


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def chr47(path: Path) -> str:
    """A SQL expression for ``path`` with no '/' inside any string literal, so the literal path check has nothing to
    catch: every slash is chr(47)."""
    return "||".join(f"chr(47)||'{seg}'" for seg in str(path).split("/") if seg)


# Every write target is an absolute path under the sandbox: a payload that does run on an old connection must not leave
# a relative file behind in whatever directory a later test has as its cwd.
PAYLOAD_NAMES = [
    "read_text_literal", "read_text_chr47", "read_text_relative", "read_csv_auto_chr47", "glob_relative",
    "glob_chr47", "export_database", "attach", "set_log_query_path_then_select", "copy_to", "install_httpfs",
    "load_local_file", "two_statements_copy", "two_statements_attach", "cte_copy", "cte_insert",
]


def _payload(sb: Sandbox, name: str) -> str:
    return {
        "read_text_literal": f"SELECT content FROM read_text('{sb.outside / 'secret.txt'}')",
        "read_text_chr47": f"SELECT content FROM read_text({chr47(sb.outside / 'secret.txt')})",
        "read_text_relative": "SELECT content FROM read_text('local.env')",
        "read_csv_auto_chr47": f"SELECT * FROM read_csv_auto({chr47(sb.outside / 'secret.csv')})",
        "glob_relative": "SELECT file FROM glob('*')",
        "glob_chr47": f"SELECT file FROM glob({chr47(sb.outside / '*')})",
        "export_database": f"EXPORT DATABASE '{sb.cwd / 'exported'}'",
        "attach": f"ATTACH '{sb.cwd / 'attached.db'}' AS attached_db",
        "set_log_query_path_then_select": f"SET log_query_path = '{sb.cwd / 'querylog.txt'}'; SELECT 'PWNED' AS x",
        "copy_to": f"COPY (SELECT 42 AS x) TO '{sb.cwd / 'copied.csv'}'",
        "install_httpfs": "INSTALL httpfs",
        "load_local_file": f"LOAD '{sb.outside / 'evil.duckdb_extension'}'",
        "two_statements_copy": f"SELECT 1; COPY (SELECT 42 AS x) TO '{sb.cwd / 'chained.csv'}'",
        "two_statements_attach": f"SELECT 1; ATTACH '{sb.cwd / 'chained.db'}' AS chained_db",
        "cte_copy": f"COPY (WITH src AS (SELECT 42 AS x) SELECT * FROM src) TO '{sb.cwd / 'cte.csv'}'",
        "cte_insert": "WITH src AS (SELECT 42 AS x) INSERT INTO t SELECT * FROM src",
    }[name]


@pytest.mark.parametrize("name", PAYLOAD_NAMES)
def test_a_hostile_query_is_refused_or_fails_without_effect(sandbox, name):
    before = _files(sandbox.root)
    out = tools.query_parquet_duckdb(_payload(sandbox, name))
    # A refusal or a DuckDB error may name a path, so the leak check is on returned data: no JSON result, no secret.
    assert out.startswith(("Security Error", "DuckDB Error")), out
    assert not [m for m in SECRET_MARKERS if m in out], out
    assert _files(sandbox.root) == before, f"{name} wrote or created a file"


@pytest.mark.parametrize("query", [
    "SELECT 1; SELECT 2",
    "SET log_query_path = 'querylog.txt'; SELECT 'PWNED' AS x",
    "SET threads = 1",
    "LOAD 'x'",
    "",
    "   ;  ",
])
def test_only_one_select_statement_is_accepted(sandbox, query):
    before = _files(sandbox.root)
    assert tools.query_parquet_duckdb(query) == REFUSED_NOT_A_SELECT
    assert _files(sandbox.root) == before


def test_pragma_version_is_a_select_after_parsing_and_is_harmless(sandbox):
    # DuckDB parses PRAGMA version into SELECT * FROM pragma_version(): it reads no file and changes no setting
    out = tools.query_parquet_duckdb("PRAGMA version")
    assert json.loads(out)[0]["library_version"]


def test_a_normal_aggregate_over_the_catalog_still_returns_json(sandbox):
    out = tools.query_parquet_duckdb("SELECT count(*) AS n, max(close) AS top FROM read_parquet('data/bars/**/*.parquet')")
    assert json.loads(out) == [{"n": 1, "top": 100.0}]


def test_a_select_with_a_trailing_semicolon_is_one_statement(sandbox):
    out = tools.query_parquet_duckdb("SELECT count(*) AS n FROM read_parquet('data/bars/**/*.parquet');")
    assert json.loads(out) == [{"n": 1}]


def test_settings_do_not_persist_between_calls(sandbox):
    default_threads = duckdb.connect().execute("SELECT current_setting('threads')").fetchone()[0]
    changed = "1" if default_threads != "1" else "2"
    tools.query_parquet_duckdb(f"SET threads = {changed}; SELECT 1 AS x")
    out = tools.query_parquet_duckdb("SELECT current_setting('threads') AS t")
    assert json.loads(out) == [{"t": default_threads}]


@pytest.mark.parametrize("name, expected", [("enable_external_access", False), ("lock_configuration", True)])
def test_each_query_runs_in_a_locked_sandbox(sandbox, name, expected):
    out = tools.query_parquet_duckdb(f"SELECT current_setting('{name}') AS v")
    assert json.loads(out) == [{"v": expected}]


def test_the_result_is_capped_at_1000_rows(sandbox):
    rows = json.loads(tools.query_parquet_duckdb("SELECT range AS i FROM range(5000)"))
    assert len(rows) == 1000


UNSAFE_BACKTEST_IDS = [
    "../x'; COPY (SELECT 1) TO 'pwned.csv'",
    "x'; COPY (SELECT 1) TO 'pwned.csv'; --",
    "x' OR '1'='1",
    "a/b",
    "..",
    "x y",
    "x" * 65,
    "x\n/../y",  # surrounding whitespace is stripped; an inner newline and a traversal are not an id
    "x\x00",
]


@pytest.mark.parametrize("backtest_id", UNSAFE_BACKTEST_IDS)
def test_the_tearsheet_refuses_an_unsafe_backtest_id_and_writes_nothing(sandbox, backtest_id):
    before = _files(sandbox.root)
    out = tools.read_nautilus_tearsheet(backtest_id)
    assert out.startswith(("Error", "Security Error")), out
    assert _files(sandbox.root) == before


class _ScanLake:
    """Stands in for AnalyticsQueryEngine: the lake refuses one symbol (its path would be unsafe) and serves the other."""

    def __init__(self, refused: str, frame: pl.DataFrame):
        self.refused, self.frame = refused, frame

    def query_bars_df(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
        if symbol == self.refused:
            raise ValueError("invalid symbol")
        return self.frame


def _closed_minutes() -> pl.DataFrame:
    t0 = 1_791_201_600 * 10**9  # 2026-10-05 12:00:00 UTC
    opens = [t0 - 15 * ONE_MINUTE_NS + i * ONE_MINUTE_NS for i in range(30)]
    return pl.DataFrame({
        "bar_open_ns": opens, "ts_event_ns": [o + ONE_MINUTE_NS for o in opens],
        "open": [100.0] * 30, "high": [101.0] * 30, "low": [99.0] * 30, "close": [100.5] * 30, "volume": [1.0] * 30,
    })


class _ScanConn:
    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, sql, *args):
        return {"id": args[0]}

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        return "OK"


class _ScanPool:
    @asynccontextmanager
    async def acquire(self):
        yield _ScanConn()


def test_the_scanner_skips_one_refused_instrument_and_scans_the_rest(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(worker_mod.time, "time_ns", lambda: 1_791_201_600 * 10**9 + 14 * ONE_MINUTE_NS + 30 * 10**9)
    evaluated: list[str] = []

    def evaluate(cls, df, rule_groups, risk_config, instrument_id, timeframe, *a, **k):
        evaluated.append(instrument_id)
        return []

    monkeypatch.setattr(worker_mod.ScannerEngine, "evaluate", classmethod(evaluate))
    monkeypatch.setattr(worker_mod.ScannerEngine, "enrich_indicators", classmethod(lambda cls, df: df))
    w = ScannerWorker(analytics_lake_path=str(tmp_path))
    w.query_engine = _ScanLake("BAD-PERP", _closed_minutes())
    w.pool = _ScanPool()
    with caplog.at_level(logging.WARNING):
        asyncio.run(w._execute_scan(
            spec_version_id=uuid4(), spec_hash="h", trigger_reason="MANUAL_JOB", trigger_event_id=None,
            universe_data={"instrument_ids": ["BAD-PERP.BINANCE", "BTCUSDT.BINANCE"], "timeframes": ["15m"]},
            rules_data=[{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}],
        ))
    assert "BTCUSDT.BINANCE" in evaluated and "BAD-PERP.BINANCE" not in evaluated
    assert any("Skipping BAD-PERP.BINANCE" in r.getMessage() for r in caplog.records)


class _OutcomeLake:
    def __init__(self, refused: str, frames: dict[str, pl.DataFrame]):
        self.refused, self.frames = refused, frames

    def query_bars_df(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
        if symbol == self.refused:
            raise ValueError("invalid symbol")
        df = self.frames.get(symbol, pl.DataFrame())
        if df.is_empty():
            return df
        return df.filter((pl.col("ts_event_ns") >= (start_ns or 0)) & (pl.col("ts_event_ns") <= (end_ns or 2**63)))


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_hfoutcomes") as url:
        yield url


async def test_the_outcome_pass_skips_one_refused_row_and_labels_the_rest(db_url, caplog):
    minute = 60 * 10**9
    t0 = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.UTC)
    t0_ns = int(t0.timestamp() * 10**9)
    closes = [3000.0 + 0.05 * (i + 1) for i in range(61)]
    eth = pl.DataFrame({"ts_event_ns": [t0_ns + (i + 1) * minute for i in range(61)], "close": closes,
                        "high": [c + 0.5 for c in closes], "low": [c - 0.5 for c in closes]})
    lake = _OutcomeLake("BAD-PERP", {"ETHUSDT": eth})
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            run = await conn.fetchval("INSERT INTO debate_runs (instrument_id, trigger_source, status) "
                                      "VALUES ('ETHUSDT.BINANCE', 'test', 'COMPLETED') RETURNING id")
            for inst in ("BAD-PERP.BINANCE", "ETHUSDT.BINANCE"):
                await conn.execute("INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, "
                                   "allocation_pct, reasoning, created_at, decision_price) "
                                   "VALUES ($1, $2, 'BUY', 0.7, 5, 'trend', $3, 3000)", run, inst, t0)
        with caplog.at_level(logging.WARNING, logger="cryptonite.debate_worker.outcomes"):
            written = await outcomes.label_pending(pool, lake, t0 + datetime.timedelta(hours=2))
        assert written == 1  # the 1h horizon of the ETH decision; the BAD-PERP decision is skipped, not fatal
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT pd.instrument_id, o.horizon FROM ai_decision_outcomes o "
                                    "JOIN portfolio_decisions pd ON pd.id = o.decision_id")
        assert [(r["instrument_id"], r["horizon"]) for r in rows] == [("ETHUSDT.BINANCE", "1h")]
        assert any("BAD-PERP.BINANCE" in r.getMessage() for r in caplog.records)
    finally:
        await pool.close()
