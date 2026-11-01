"""H5: scan honesty. One regression per item: coverage notes are never setups (the 19:10 MUBARAKUSDT answer), the
scanner counts only the newest bar of each evaluation, and discovery matches on uncovered coins stay informational."""

import asyncio
import datetime as dt
import importlib.util
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import polars as pl
import pytest
from cryptonite_agents import tools
from cryptonite_agents.brain.widgets import widget_for_tool
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner_service import worker as worker_mod

ROOT = Path(__file__).resolve().parents[1]
SCOPES = [{"name": "BTC perp", "instruments": ["BTCUSDT-PERP.BINANCE"], "markets": ["PERP"], "direction": "BOTH"},
          {"name": "ETH spot", "instruments": ["ETHUSDT.BINANCE"], "markets": ["SPOT"], "direction": "LONG"}]
PRESET = "swing-trend-pullback-1h"


class NoLake:
    def __init__(self, **_):
        pass

    def load_bars(self, **_):
        return None

    def close(self):
        pass


def _candles(interval: str, n: int = 120) -> list[dict]:
    step = {"15m": 15, "1h": 60, "4h": 240}[interval] * 60_000
    last_open = (int(time.time() * 1000) // step - 1) * step  # the newest closed bar
    return [{"open_time_ms": last_open - (n - 1 - i) * step, "close_time_ms": last_open - (n - 1 - i) * step + step - 1,
             "open": 100 + (i % 3) * 0.1, "high": 101, "low": 99, "close": 100 + (i % 3) * 0.1, "volume": 1000}
            for i in range(n)]


@pytest.fixture
def scan_env(monkeypatch):
    monkeypatch.setattr(tools, "_active_scopes", lambda: SCOPES)
    monkeypatch.setattr(tools, "DuckDBLakeRepository", NoLake)
    monkeypatch.setattr(tools, "_recent_scanner_setups", lambda *_: [])
    monkeypatch.setattr(tools, "_stored_funding_8h", lambda *_: None)
    return monkeypatch


# 1. 1 Nov 19:10: the Brain said "the scanner found a potential long setup on MUBARAKUSDT" from a coverage note.
async def test_an_uncovered_coin_without_a_match_is_a_coverage_note_only(scan_env):
    scan_env.setattr(tools, "_fed_symbols", lambda: frozenset({"BTCUSDT", "ETHUSDT", "MUBARAKUSDT"}))
    scan_env.setenv(tools.DISCOVERY_ENV, "MUBARAKUSDT")
    scan_env.setattr(tools, "_spot_klines", lambda *a, **k: (None, "offline"))
    content = await tools.scan_swing_setups()
    out = json.loads(content)
    assert out["candidates"] == [] and out["untradeable_setups"] == []
    assert out["uncovered_coins"] == [{"coin": "MUBARAKUSDT", "reason": "no ACTIVE mandate covers MUBARAKUSDT"}]
    assert "not_tradeable" not in out and "uncovered_coins` are coverage notes, not setups" in out["note"]
    widget = widget_for_tool("scan_swing_setups", {}, call_id="c1", content=content)
    assert widget["candidates"] == [] and widget["untradeable_setups"] == []
    assert widget["uncovered_coins"] == out["uncovered_coins"] and widget["rejections"] == out["rejections"]


def test_the_brain_never_calls_a_coverage_note_a_setup():
    from cryptonite_agents.brain.agent import BRAIN_INSTRUCTIONS

    prompt = " ".join(BRAIN_INSTRUCTIONS.split())
    assert "Only its candidates and untradeable_setups are setups: quote each with its bar_close time" in prompt
    assert "A coin in uncovered_coins is a coverage note: never say a setup, signal or potential setup" in prompt


# 3. A discovery coin outside the feed and every mandate is searched over REST; its match is informational only.
async def test_a_discovery_match_on_an_uncovered_coin_is_an_untradeable_setup(scan_env):
    scan_env.setattr(tools, "_fed_symbols", lambda: frozenset({"BTCUSDT", "ETHUSDT"}))
    scan_env.setenv(tools.DISCOVERY_ENV, "SOLUSDT")
    scan_env.setattr(tools, "_spot_klines",
                     lambda sym, tf, *a, **k: ({"candles": _candles(tf)}, None) if sym == "SOLUSDT" else (None, "x"))

    def evaluate(df, **_):
        last = df.tail(1).to_dicts()[0]
        close = float(last["close"])
        return [SimpleNamespace(bar_close_inclusive_ns=int(last["bar_close_inclusive_ns"]), suggested_entry=close,
                                suggested_stop=close - 2, suggested_target=close + 4, matched_rules=["pullback"])]

    scan_env.setattr(ScannerEngine, "evaluate", staticmethod(evaluate))
    content = await tools.scan_swing_setups(preset_slug=PRESET)
    out = json.loads(content)
    assert out["candidates"] == []
    [setup] = out["untradeable_setups"]
    assert setup["instrument_id"] == "SOLUSDT.BINANCE" and setup["rr_ratio"] == 2.0
    assert setup["reason"] == "SOLUSDT is not in the live feed and no ACTIVE mandate covers it"
    last_close_ms = _candles(setup["timeframe"])[-1]["close_time_ms"]
    assert dt.datetime.fromisoformat(setup["bar_close"]) == dt.datetime.fromtimestamp(last_close_ms / 1000, dt.UTC)
    widget = widget_for_tool("scan_swing_setups", {}, call_id="c2", content=content)
    assert widget["untradeable_setups"][0]["bar_close"] == setup["bar_close"] and widget["candidates"] == []

    scan_env.setattr(ScannerEngine, "evaluate", staticmethod(lambda **_: []))
    out = json.loads(await tools.scan_swing_setups(preset_slug=PRESET))
    assert out["untradeable_setups"] == [] and out["rejections"]["counts"]["NO_TRIGGER"] == 1
    assert out["rejections"]["by_coin_preset"] == [{"coin": "SOLUSDT", "preset_slug": PRESET, "timeframe": "1h",
                                                    "outcome": "NO_TRIGGER"}]


# 2. The scanner counts the newest closed bar of an evaluation only, never the history it re-reads.
def _cand(bar_ns: int, tf: str, entry=100, stop=98, target=104):
    return SimpleNamespace(bar_close_inclusive_ns=bar_ns, interval_end_exclusive_ns=bar_ns + 1, timeframe=tf,
                           suggested_entry=Decimal(entry), suggested_stop=Decimal(stop),
                           suggested_target=Decimal(target))


def test_only_the_newest_bar_is_counted():
    hour = 3_600_000_000_000
    now = time.time_ns()
    newest = now - now % hour - 1  # the 1h bar that just closed
    df = pl.DataFrame({"bar_close_inclusive_ns": [newest - 2 * hour, newest - hour, newest]})
    old = [_cand(newest - 2 * hour, "1h"), _cand(newest - hour, "1h")]  # history matches never count
    assert worker_mod.latest_bar_outcome(df, old, now) == (newest, "NO_TRIGGER")
    assert worker_mod.latest_bar_outcome(df, [*old, _cand(newest, "1h")], now) == (newest, "MATCHED_FRESH")
    assert worker_mod.latest_bar_outcome(df, [_cand(newest, "1h", target=101)], now) == (newest, "REJECTED_RR")
    assert worker_mod.latest_bar_outcome(df, [_cand(newest, "1h")], now + 3 * hour) == (newest, "STALE")
    assert worker_mod.eval_bucket(newest) == dt.datetime.fromtimestamp(newest / 1e9, dt.UTC).replace(
        minute=0, second=0, microsecond=0)


def test_a_failed_count_write_never_blocks_the_scan():
    class Broken:
        async def executemany(self, *_):
            raise asyncpg.PostgresError("down")

    w = worker_mod.ScannerWorker.__new__(worker_mod.ScannerWorker)
    w.pool = Broken()
    asyncio.run(w._record_eval_counts([("b", "i", "1h", "p", "LONG", "NO_TRIGGER")]))


DB_URL = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif("cryptonite_ci" not in DB_URL, reason="needs the throwaway CI database")


@needs_db
def test_counts_upsert_and_the_funnel_report_shows_the_fire_rate():
    spec = importlib.util.spec_from_file_location("funnel_report_h5", ROOT / "scripts" / "funnel_report.py")
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    inst = f"H5{uuid.uuid4().hex[:8].upper()}USDT.BINANCE"
    bucket = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    row = (bucket, inst, "1h", PRESET, "LONG", "MATCHED_FRESH")

    async def go():
        pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
        w = worker_mod.ScannerWorker.__new__(worker_mod.ScannerWorker)
        w.pool = pool
        try:
            await w._record_eval_counts([row, (*row[:5], "NO_TRIGGER")])
            await w._record_eval_counts([row])
            got = dict(await pool.fetch("SELECT outcome, n FROM agent_safe_scan_eval_counts WHERE instrument_id=$1",
                                        inst))
            assert got == {"MATCHED_FRESH": 2, "NO_TRIGGER": 1}
            today = bucket.date()
            text = await report.report(DB_URL, today, today + dt.timedelta(days=1))
            assert "## Preset fire rate" in text
            assert f"| {inst.split('.')[0]} | {PRESET} | LONG | 1h | 3 | 2 | 0 | 0 | 1 | 66.7% |" in text
        finally:
            await pool.execute("DELETE FROM scan_eval_counts WHERE instrument_id=$1", inst)
            await pool.close()

    asyncio.run(go())
