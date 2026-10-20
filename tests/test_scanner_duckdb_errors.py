"""A failed lake read (DuckDB out of memory, a corrupt or half-written file) skips that instrument for that run. The
other instruments and every later spec still run, and the closed-bar event is acknowledged. Before, only ValueError
was caught around query_bars_df, so a duckdb.Error escaped before XACK and the later specs never ran
(docs/runbooks/host-stability.md §6). No DB, Redis or lake: the pool, Redis and lake are stand-ins."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

import duckdb
import polars as pl
import pytest
from cryptonite_scanner.resampler import ONE_MINUTE_NS
from cryptonite_scanner_service import worker as worker_mod
from cryptonite_scanner_service.worker import ScannerWorker

NOW_NS = 1_791_201_600 * 10**9 + 14 * ONE_MINUTE_NS + 30 * 10**9  # 2026-10-05 12:14:30 UTC


def _closed_minutes() -> pl.DataFrame:
    t0 = 1_791_201_600 * 10**9
    opens = [t0 - 15 * ONE_MINUTE_NS + i * ONE_MINUTE_NS for i in range(30)]
    return pl.DataFrame({
        "bar_open_ns": opens, "ts_event_ns": [o + ONE_MINUTE_NS for o in opens],
        "open": [100.0] * 30, "high": [101.0] * 30, "low": [99.0] * 30, "close": [100.5] * 30, "volume": [1.0] * 30,
    })


class _FailingLake:
    """The lake read for ``failing`` raises ``error``; every other symbol gets a frame."""

    def __init__(self, failing: str, error: Exception):
        self.failing, self.error, self.reads = failing, error, []

    def query_bars_df(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
        self.reads.append(symbol)
        if symbol == self.failing:
            raise self.error
        return _closed_minutes()


class _Conn:
    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, sql, *args):
        return {"id": args[0]}

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        return "OK"


class _Pool:
    def __init__(self, specs):
        self.specs = specs

    @asynccontextmanager
    async def acquire(self):
        yield _Conn()

    async def fetch(self, sql, *args):
        return self.specs if "FROM scan_specs" in sql else []

    async def fetchval(self, sql, *args):
        return None


class _Redis:
    def __init__(self):
        self.acked = []

    async def xack(self, stream, group, msg_id):
        self.acked.append((stream, group, msg_id))


def _spec(slug: str, instruments: list[str]) -> dict:
    return {
        "slug": slug, "active_version_id": uuid4(), "version_id": uuid4(), "spec_hash": f"hash-{slug}",
        "universe": json.dumps({"instrument_ids": instruments, "timeframes": ["15m"]}),
        "rules": json.dumps([{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}]),
    }


@pytest.mark.parametrize("error", [
    duckdb.OutOfMemoryException("Out of Memory Error: failed to allocate data of size 256.0 MiB (1.0 GiB/1.0 GiB used)"),
    duckdb.InvalidInputException("Invalid Input Error: File '.tmp.ab.parquet' too small to be a Parquet file"),
    duckdb.IOException("IO Error: Cannot open file: No such file or directory"),
])
def test_a_duckdb_error_skips_the_instrument_and_the_event_is_still_acked(monkeypatch, caplog, error):
    monkeypatch.setattr(worker_mod.time, "time_ns", lambda: NOW_NS)
    evaluated: list[tuple[str, str]] = []
    run_specs: list[str] = []

    def evaluate(cls, df, rule_groups, risk_config, instrument_id, timeframe, spec_version_id, *a, **k):
        evaluated.append((str(spec_version_id), instrument_id))
        return []

    monkeypatch.setattr(worker_mod.ScannerEngine, "evaluate", classmethod(evaluate))
    monkeypatch.setattr(worker_mod.ScannerEngine, "enrich_indicators", classmethod(lambda cls, df: df))
    # spec 1 scans ETH (its read fails) and then BTC; spec 2 scans BTC only
    first, second = _spec("first", ["ETHUSDT.BINANCE", "BTCUSDT.BINANCE"]), _spec("second", ["BTCUSDT.BINANCE"])
    w = ScannerWorker(analytics_lake_path="/nonexistent-lake")
    w.query_engine = _FailingLake("ETHUSDT", error)
    w.pool = _Pool([first, second])
    w.redis = _Redis()
    real_execute = w._execute_scan

    async def tracking_execute(**kw):
        run_specs.append(str(kw["spec_version_id"]))
        return await real_execute(**kw)

    w._execute_scan = tracking_execute
    event = {"event_id": "evt-1", "instrument_id": "BTCUSDT.BINANCE", "timeframe": "15m",
             "bar_close_inclusive_ns": NOW_NS - 30 * 10**9}
    with caplog.at_level(logging.WARNING):
        asyncio.run(w._handle_stream_message("cryptonite:stream:closed_bar_committed", "1-0",
                                             {"data": json.dumps(event)}))

    assert run_specs == [str(first["version_id"]), str(second["version_id"])]  # the later spec ran
    assert w.query_engine.reads == ["ETHUSDT", "BTCUSDT", "BTCUSDT"]
    assert evaluated == [(str(first["version_id"]), "BTCUSDT.BINANCE"), (str(second["version_id"]), "BTCUSDT.BINANCE")]
    assert w.redis.acked == [("cryptonite:stream:closed_bar_committed", "scanner-worker", "1-0")]
    assert w.processed_events_count == 1
    assert any("Skipping ETHUSDT.BINANCE" in r.getMessage() and type(error).__name__ in r.getMessage()
               for r in caplog.records)


def test_an_error_that_is_not_duckdb_still_propagates(monkeypatch):
    """Only lake read errors are absorbed: a bug elsewhere must not be silently skipped (and the event stays unacked)."""
    monkeypatch.setattr(worker_mod.time, "time_ns", lambda: NOW_NS)
    w = ScannerWorker(analytics_lake_path="/nonexistent-lake")
    w.query_engine = _FailingLake("BTCUSDT", RuntimeError("bug"))
    w.pool = _Pool([_spec("only", ["BTCUSDT.BINANCE"])])
    w.redis = _Redis()
    event = {"event_id": "evt-2", "instrument_id": "BTCUSDT.BINANCE", "timeframe": "15m"}
    with pytest.raises(RuntimeError):
        asyncio.run(w._handle_stream_message("s", "2-0", {"data": json.dumps(event)}))
    assert w.redis.acked == []
