"""B1 / F-C1: ``scripts/repair_lake_open_stamped_rows.py`` selects exactly the lake rows the API stamped at
the bar OPEN (and the older ``:59.999`` rows), leaves ingest's rows alone, changes nothing in a dry run,
and with ``--apply`` quarantines (never deletes) and rewrites them so the lake reads back aligned."""

import hashlib
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.queries import AnalyticsQueryEngine, timeframe_ns

MIN_NS = 60 * 10**9
T0_NS = 1_791_104_400 * 10**9  # 2026-10-04 09:00:00 UTC
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repair_lake_open_stamped_rows.py"


def load_script():
    spec = importlib.util.spec_from_file_location("repair_lake_open_stamped_rows", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # its dataclasses resolve annotations through the module
    spec.loader.exec_module(mod)
    return mod


def kline(open_ns: int, price: float) -> RawBinanceKline:
    open_ms = open_ns // 1_000_000
    return RawBinanceKline(open_time_ms=open_ms, close_time_ms=open_ms + 59_999, open=str(price),
                           high=str(price + 1), low=str(price - 1), close=str(price + 0.5), volume="1",
                           quote_volume="1", trades_count=1, taker_buy_base_volume="0", taker_buy_quote_volume="0")


def ingest_bar(symbol: str, open_ns: int, price: float) -> EnrichedBar:
    return EnrichedBar.from_binance_raw(kline(open_ns, price), f"{symbol}.BINANCE",
                                        f"{symbol}.BINANCE-1-MINUTE-LAST-EXTERNAL", 2, 5,
                                        received_at_ns=open_ns + MIN_NS - 325_000_000)


def old_api_bar(symbol: str, open_ns: int, price: float) -> EnrichedBar:
    """What routes/instruments.py wrote before B1: ts at the OPEN, received_at = Binance close_time."""
    k = kline(open_ns, price)
    return EnrichedBar(instrument_id=f"{symbol}.BINANCE", bar_type=f"{symbol}.BINANCE-1-MINUTE-LAST-EXTERNAL",
                       ts_event_ns=open_ns, ts_init_ns=open_ns, received_at_ns=k.close_time_ms * 1_000_000,
                       price_precision=2, size_precision=5, open=Decimal(k.open), high=Decimal(k.high),
                       low=Decimal(k.low), close=Decimal(k.close), volume=Decimal("1"), quote_volume=Decimal("1"),
                       taker_buy_base_volume=Decimal("0"), taker_buy_quote_volume=Decimal("0"), trades_count=1,
                       open_raw=k.open, high_raw=k.high, low_raw=k.low, close_raw=k.close, volume_raw="1")


def close_time_bar(symbol: str, open_ns: int, price: float, remainder_ns: int = 1_000_000) -> EnrichedBar:
    """The older writer: ts_event_ns = Binance close_time (xx:xx:59.999), received_at the same."""
    b = old_api_bar(symbol, open_ns, price)
    ts = open_ns + MIN_NS - remainder_ns
    return b.model_copy(update={"ts_event_ns": ts, "ts_init_ns": ts, "received_at_ns": ts})


@pytest.fixture
def lake(tmp_path):
    root = tmp_path / "analytics_lake"
    w = AnalyticsLakeWriter(root)
    # ingest: minutes opening 09:00 .. 09:59 (closes 09:01 .. 10:00)
    w.write_batch([ingest_bar("BTCUSDT", T0_NS + i * MIN_NS, 100 + i) for i in range(60)],
                  symbol="BTCUSDT", batch_id="ingest")
    # API before B1: minutes opening 09:30 .. 10:29, stamped at the open
    w.write_batch([old_api_bar("BTCUSDT", T0_NS + (30 + i) * MIN_NS, 500 + i) for i in range(60)],
                  symbol="BTCUSDT", batch_id="api")
    # older writer: 10 XRP minutes at :59.999, plus one misaligned by 5 s (no safe correction)
    w.write_batch([close_time_bar("XRPUSDT", T0_NS + i * MIN_NS, 2 + i) for i in range(10)]
                  + [close_time_bar("XRPUSDT", T0_NS + 10 * MIN_NS, 9, remainder_ns=5 * 10**9)],
                  symbol="XRPUSDT", batch_id="xrp-close-time")
    return root


def snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_selection_finds_only_the_api_and_close_time_rows(lake):
    mod = load_script()
    reports = {r.path.name: r for r in mod.scan(lake)}
    assert set(reports) == {"part-api.parquet", "part-xrp-close-time.parquet"}  # ingest's file is untouched

    api = reports["part-api.parquet"]
    assert (api.open_stamped, api.misaligned) == (60, 0)
    # closes 09:31 .. 10:00 already exist from ingest; 10:30 is the batch's newest (possibly forming) kline
    assert dict(api.disposition) == {"duplicate": 30, "rewrite": 29, "unclosed": 1}
    assert (api.first_close_ns, api.last_close_ns) == (T0_NS + 31 * MIN_NS, T0_NS + 90 * MIN_NS)

    xrp = reports["part-xrp-close-time.parquet"]
    assert (xrp.open_stamped, xrp.misaligned) == (0, 11)
    assert dict(xrp.disposition) == {"rewrite": 10, "unrecognized": 1}


def test_dry_run_changes_nothing(lake, monkeypatch, capsys):
    mod = load_script()
    before = snapshot(lake.parent)
    monkeypatch.setattr(sys, "argv", ["repair", "--lake", str(lake)])
    mod.main()
    assert snapshot(lake.parent) == before
    out = capsys.readouterr().out
    assert "2 file(s) affected" in out and "dry run: nothing changed" in out


def test_apply_quarantines_and_the_lake_reads_back_aligned(lake, monkeypatch):
    mod = load_script()
    monkeypatch.setattr(sys, "argv", ["repair", "--lake", str(lake), "--apply"])
    mod.main()

    quarantine = lake.parent / "catalog_quarantine" / "analytics_lake"
    moved = sorted(p.name for p in quarantine.rglob("part-*") if not p.name.endswith(".json"))
    assert [n.rsplit(".", 1)[0] for n in moved] == ["part-api.parquet", "part-xrp-close-time.parquet"]
    manifests = [json.loads(p.read_text()) for p in quarantine.rglob("*.json")]
    assert {m["original"].rsplit("/", 1)[1]: m["rows_written"] for m in manifests} == {
        "part-api.parquet": 29, "part-xrp-close-time.parquet": 10}

    engine = AnalyticsQueryEngine(lake)
    btc = engine.query_bars_df("BTCUSDT", "1m")
    closes = btc["ts_event_ns"].cast(int).to_list()
    assert closes == [T0_NS + (i + 1) * MIN_NS for i in range(89)]  # 09:01 .. 10:29, no gaps, no dupes
    assert (btc["bar_open_ns"] == btc["ts_event_ns"].cast(int) - MIN_NS).all()
    # 09:31 .. 10:00 keep ingest's values; 10:01 .. 10:29 are the API's klines, now at their close
    by_close = {int(r["ts_event_ns"]): float(r["open"]) for r in btc.to_dicts()}
    assert by_close[T0_NS + 45 * MIN_NS] == 100 + 44
    assert by_close[T0_NS + 61 * MIN_NS] == 500 + 30  # minute opening 10:00 was the API's 31st kline

    xrp = engine.query_bars_df("XRPUSDT", "1m")
    assert xrp["ts_event_ns"].cast(int).to_list() == [T0_NS + (i + 1) * MIN_NS for i in range(10)]

    assert mod.scan(lake) == []  # idempotent: nothing left to repair


def test_a_row_recorded_while_open_is_no_clean_copy(tmp_path):
    """The duplicate test follows query_bars_df's ranking: an ingest row recorded 30 s before its close
    (a forming kline) does not make the API's closed REST copy a duplicate; that copy is rewritten and wins."""
    root = tmp_path / "analytics_lake"
    w = AnalyticsLakeWriter(root)
    forming = ingest_bar("BTCUSDT", T0_NS, 100).model_copy(update={"received_at_ns": T0_NS + MIN_NS - 30 * 10**9})
    w.write_batch([forming], symbol="BTCUSDT", batch_id="forming")
    w.write_batch([old_api_bar("BTCUSDT", T0_NS + i * MIN_NS, 500 + i) for i in range(2)], symbol="BTCUSDT",
                  batch_id="api")
    mod = load_script()
    (api,) = mod.scan(root)
    assert api.decisions[0] == (T0_NS + MIN_NS, "rewrite")
    assert dict(api.disposition) == {"rewrite": 1, "unclosed": 1}
    mod.apply(api, root, tmp_path / "quarantine", "stamp")
    df = AnalyticsQueryEngine(root).query_bars_df("BTCUSDT", "1m")
    assert [(int(r["ts_event_ns"]), float(r["open"])) for r in df.to_dicts()] == [(T0_NS + MIN_NS, 500.0)]


def test_weekly_and_other_timeframes_are_never_touched(tmp_path, capsys):
    """Binance 1w klines open on Monday, so ``ts % 1w`` is never 0 for a correct weekly row: the repair
    refuses every timeframe but 1m and 1h instead of quarantining good rows as misaligned."""
    root = tmp_path / "analytics_lake"
    monday_ns = 1_790_985_600 * 10**9  # 2026-10-05 00:00 UTC, a Monday
    weekly = ingest_bar("BTCUSDT", monday_ns - 7 * 86400 * 10**9, 100).model_copy(
        update={"ts_event_ns": monday_ns, "ts_init_ns": monday_ns, "received_at_ns": monday_ns + 10**9})
    AnalyticsLakeWriter(root).write_batch([weekly], symbol="BTCUSDT", timeframe="1w", batch_id="weekly")
    assert monday_ns % timeframe_ns("1w") != 0  # the hazard the allowlist exists for
    assert load_script().scan(root) == []
    assert "skipped BTCUSDT 1w" in capsys.readouterr().out


@pytest.mark.parametrize("tf,ns", [("1m", MIN_NS), ("15m", 15 * MIN_NS), ("1h", 60 * MIN_NS),
                                   ("1d", 1440 * MIN_NS), ("1w", 7 * 1440 * MIN_NS)])
def test_timeframe_ns(tf, ns):
    assert timeframe_ns(tf) == ns


@pytest.mark.parametrize("tf", ["1M", "3M", "", "m", "0m", "1x"])
def test_timeframe_ns_rejects_months_and_junk(tf):
    """``1M`` is a calendar month on Binance; it must not be lowercased into one minute."""
    with pytest.raises(ValueError):
        timeframe_ns(tf)
