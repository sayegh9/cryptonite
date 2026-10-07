"""B1 / F-C1 guard: the scanner never emits a candidate whose bar has not closed yet
(``interval_end_exclusive_ns`` after the scan's ``now``), even when the lake holds a minute recorded while
its kline was still open. Runs ``ScannerWorker._execute_scan`` against a fake lake and a fake pool."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import polars as pl
import pytest
from cryptonite_scanner.models import SignalCandidateRead
from cryptonite_scanner.resampler import ONE_MINUTE_NS
from cryptonite_scanner_service import worker as worker_mod
from cryptonite_scanner_service.worker import ScannerWorker, drop_unclosed_candidates

T_NS = 1_791_201_600 * 10**9  # 2026-10-05 12:00:00 UTC, a 15m boundary
NOW_NS = T_NS + 14 * ONE_MINUTE_NS + 30 * 10**9  # 12:14:30, inside the 12:14 minute


def lake_frame() -> pl.DataFrame:
    """What query_bars_df returns: minutes opening 11:45 .. 12:14. The last one closes at 12:15, after
    now: it was recorded while its kline was still open, so the 12:00 15m window looks complete."""
    opens = [T_NS - 15 * ONE_MINUTE_NS + i * ONE_MINUTE_NS for i in range(30)]
    return pl.DataFrame({
        "bar_open_ns": opens,
        "ts_event_ns": [o + ONE_MINUTE_NS for o in opens],
        "open": [100.0 + i for i in range(30)], "high": [101.0 + i for i in range(30)],
        "low": [99.0 + i for i in range(30)], "close": [100.5 + i for i in range(30)],
        "volume": [1.0] * 30,
    })


class FakeQueryEngine:
    def query_bars_df(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
        return lake_frame()


class FakeConn:
    def __init__(self, inserted):
        self.inserted = inserted

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, sql, *args):
        if "INSERT INTO signal_candidates" in sql:
            self.inserted.append({"timeframe": args[4], "bar_open_ns": args[5], "interval_end_exclusive_ns": args[7]})
            return {"id": args[0]}
        return {"id": args[0]}

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        return "OK"


class FakePool:
    def __init__(self):
        self.inserted: list[dict] = []

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self.inserted)


def candidate(tf: str, bar_open_ns: int, end_ns: int) -> SignalCandidateRead:
    return SignalCandidateRead(
        id=uuid4(), scan_run_id=uuid4(), spec_version_id=uuid4(), instrument_id="BTCUSDT.BINANCE", timeframe=tf,
        bar_open_ns=bar_open_ns, bar_close_inclusive_ns=end_ns - 1_000_000, interval_end_exclusive_ns=end_ns,
        matched_rules=["always"], metrics={}, suggested_entry=Decimal("1"), suggested_stop=Decimal("0.9"),
        suggested_target=Decimal("1.2"), created_at=datetime.now(UTC),
    )


def every_bar(df, rule_groups, risk_config, instrument_id, timeframe, spec_version_id, scan_run_id, price_precision=2):
    """Stand-in for ScannerEngine.evaluate: a rule that fires on every bar it is given."""
    return [candidate(timeframe, r["bar_open_ns"], r["interval_end_exclusive_ns"]) for r in df.to_dicts()]


def run_scan(tmp_path, monkeypatch, evaluate) -> list[dict]:
    monkeypatch.setattr(worker_mod.time, "time_ns", lambda: NOW_NS)
    monkeypatch.setattr(worker_mod.ScannerEngine, "evaluate", classmethod(lambda cls, *a, **k: evaluate(*a, **k)))
    monkeypatch.setattr(worker_mod.ScannerEngine, "enrich_indicators", classmethod(lambda cls, df: df))
    w = ScannerWorker(analytics_lake_path=str(tmp_path))
    w.query_engine = FakeQueryEngine()
    w.pool = FakePool()
    asyncio.run(w._execute_scan(
        spec_version_id=uuid4(), spec_hash="h", trigger_reason="MANUAL_JOB", trigger_event_id=None,
        universe_data={"instrument_ids": ["BTCUSDT.BINANCE"], "timeframes": ["1m", "15m"]},
        rules_data=[{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}],
    ))
    return w.pool.inserted


def test_no_candidate_for_a_bar_that_closes_after_now(tmp_path, monkeypatch):
    inserted = run_scan(tmp_path, monkeypatch, every_bar)
    late = [c for c in inserted if c["interval_end_exclusive_ns"] > NOW_NS]
    assert not late, f"candidates emitted before their bar closed: {late}"
    # the closed bars still come through: the 11:45 15m window and every minute up to 12:13
    assert [c["bar_open_ns"] for c in inserted if c["timeframe"] == "15m"] == [T_NS - 15 * ONE_MINUTE_NS]
    ones = [c["bar_open_ns"] for c in inserted if c["timeframe"] == "1m"]
    assert len(ones) == 29 and max(ones) == T_NS + 13 * ONE_MINUTE_NS


def test_final_guard_drops_a_future_candidate_from_the_rule_engine(tmp_path, monkeypatch):
    """Belt and braces: even if rule evaluation produced one, a candidate ending after now is not committed."""
    def future(**kw):
        return [candidate(kw["timeframe"], T_NS, T_NS + 15 * ONE_MINUTE_NS)]

    assert run_scan(tmp_path, monkeypatch, future) == []


@pytest.mark.parametrize("end_offset_ns,kept", [(-1, True), (0, True), (1, False)])
def test_drop_unclosed_candidates_boundary(end_offset_ns, kept):
    c = candidate("15m", T_NS, NOW_NS + end_offset_ns)
    assert drop_unclosed_candidates([c], NOW_NS) == ([c] if kept else [])
