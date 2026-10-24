"""Synthetic contract tests for the read-only preset replay (ADR 0011, P1.9)."""

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest
from cryptonite_scanner.models import (
    RiskConfig,
    RuleCondition,
    RuleGroup,
    RuleOperator,
    ScanSpecCreate,
    UniverseConfig,
)
from cryptonite_scanner.preset_stats import (
    ReplayTrade,
    _trade_exit,
    replay_entries,
    report_document,
    summarize_replay,
)
from cryptonite_scanner.presets import _PROFILE_PRESETS

sys.path.insert(0, str(Path(__file__).parents[1]))
from scripts.preset_replay import LakeReader, main


def _preset(slug: str, side: str) -> ScanSpecCreate:
    """Build a scanner-engine preset that emits one long or short signal on the first synthetic bar."""
    operator = RuleOperator.GT if side == "LONG" else RuleOperator.LT
    return ScanSpecCreate(
        slug=slug,
        name=slug,
        universe=UniverseConfig(instrument_ids=["BTCUSDT.BINANCE"], timeframes=["1h"]),
        rules=[
            RuleGroup(
                category="trigger",
                conditions=[RuleCondition(field="close", operator=operator, value="open")],
            )
        ],
        risk=RiskConfig(direction=side, min_atr_mult=0.5, max_atr_mult=4.0),
    )


def _bars(side: str) -> pl.DataFrame:
    """Create three closed bars with a structural stop and a known 2R scale-out outcome."""
    if side == "LONG":
        rows = [
            (99.5, 100.5, 99.5, 100, 99, 99, 101),
            (102, 102.5, 100.5, 102, 99, 99, 103),
            (103, 104, 103, 103, 99, 99, 105),
        ]
    else:
        rows = [
            (100.5, 100.5, 99.5, 100, 101, 99, 101),
            (98, 99.5, 97.5, 98, 101, 99, 101),
            (97, 97, 96, 97, 101, 99, 101),
        ]
    return pl.DataFrame(
        {
            "ts_event_ns": [i * 3_600_000_000_000 for i in range(1, 4)],
            "bar_open_ns": [(i - 1) * 3_600_000_000_000 for i in range(1, 4)],
            "bar_close_inclusive_ns": [i * 3_600_000_000_000 - 1 for i in range(1, 4)],
            "interval_end_exclusive_ns": [i * 3_600_000_000_000 for i in range(1, 4)],
            "open": [row[0] for row in rows],
            "high": [row[1] for row in rows],
            "low": [row[2] for row in rows],
            "close": [row[3] for row in rows],
            "volume": [10.0, 10.0, 10.0],
            "rsi": [50.0, 50.0, 50.0],
            "ema_200": [90.0, 90.0, 90.0],
            "ema_50": [90.0, 90.0, 90.0],
            "atr": [1.0, 1.0, 1.0],
            "donchian_lower": [row[5] for row in rows],
            "donchian_upper": [row[6] for row in rows],
        }
    )


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_replay_engine_entries_for_both_directions_have_known_net_r(side: str) -> None:
    preset = _preset(f"synthetic-{side.lower()}", side)
    trades = replay_entries(_bars(side), preset, "BTCUSDT", "1h")

    assert len(trades) == 1
    assert trades[0].entry_ns == 3_600_000_000_000
    assert trades[0].exit_ns == 10_800_000_000_000
    expected_net_r = 2.2975 if side == "LONG" else 2.3025
    assert trades[0].net_r == pytest.approx(expected_net_r)


def test_summary_uses_net_r_for_win_average_median_and_drawdown() -> None:
    trades = [
        ReplayTrade(1, 2, 1.0),
        ReplayTrade(2, 3, -2.0),
        ReplayTrade(3, 4, 3.0),
    ]

    result = summarize_replay("test", "btcusdt", trades, 1, 4)

    assert result["trades"] == 3
    assert result["win_rate"] == pytest.approx(2 / 3)
    assert result["avg_r"] == pytest.approx(2 / 3)
    assert result["median_r"] == 1.0
    assert result["max_drawdown_r"] == 2.0
    assert result["first_bar"].endswith("+00:00")
    assert result["last_bar"].endswith("+00:00")


def test_report_json_has_versioned_schema_and_exit_assumptions() -> None:
    result = summarize_replay("preset-x", "BTCUSDT", [], None, None)
    encoded = json.dumps(report_document([result]))
    report = json.loads(encoded)

    assert set(report) == {"schema_version", "assumptions", "results"}
    assert report["schema_version"] == 1
    assert report["assumptions"]["market"] == "spot"
    assert report["assumptions"]["funding"] == "ignored"
    assert report["assumptions"]["fee_per_side"] == 0.001
    assert set(report["results"][0]) == {
        "preset",
        "coin",
        "trades",
        "win_rate",
        "avg_r",
        "median_r",
        "max_drawdown_r",
        "first_bar",
        "last_bar",
    }
    assert report["results"][0]["first_bar"] is None


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_ambiguous_same_bar_target_and_new_trail_uses_close_without_new_trail_fill(
    side: str,
) -> None:
    if side == "LONG":
        bars = [
            {"high": 100.0, "low": 100.0, "close": 100.0},
            {"high": 104.0, "low": 101.5, "close": 101.6},
        ]
        entry, stop = 100.0, 99.0
        expected = 1.5982
    else:
        bars = [
            {"high": 100.0, "low": 100.0, "close": 100.0},
            {"high": 98.5, "low": 96.0, "close": 98.4},
        ]
        entry, stop = 100.0, 101.0
        expected = 1.6018

    exit_index, net_r = _trade_exit(bars, 0, entry, stop, side)

    assert exit_index == 1
    assert net_r == pytest.approx(expected)


def _market_bars(timeframe: str, count: int = 260) -> pl.DataFrame:
    """Create realistic synthetic lake rows with enough closed bars for EMA-200 warmup."""
    units = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    step_ns = units[timeframe] * 1_000_000_000
    start_ns = int(datetime(2026, 4, 14, tzinfo=UTC).timestamp()) * 1_000_000_000
    rows = []
    for index in range(count):
        close = 100 + index * 0.02 + (index % 9) * 0.03
        open_price = close - (0.02 if index % 2 else -0.02)
        event_ns = start_ns + (index + 1) * step_ns - 1_000_000
        rows.append((event_ns, open_price, close + 0.2, close - 0.2, close, 10.0))
    return pl.DataFrame(
        {
            "ts_event_ns": [row[0] for row in rows],
            "ts_init_ns": [row[0] for row in rows],
            "received_at_ns": [row[0] + 2_000_000_000 for row in rows],
            "ingest_batch_id": [f"batch-{index}" for index in range(count)],
            "payload_checksum": [f"checksum-{index}" for index in range(count)],
            "open": [row[1] for row in rows],
            "high": [row[2] for row in rows],
            "low": [row[3] for row in rows],
            "close": [row[4] for row in rows],
            "volume": [row[5] for row in rows],
        }
    )


@pytest.mark.parametrize(
    ("slug", "timeframe", "higher_timeframe"),
    [
        (slug, timeframe, {"15m": "1h", "1h": "4h", "4h": "1d"}[timeframe])
        for slug, preset in _PROFILE_PRESETS.items()
        for timeframe in preset.universe.timeframes
        if any(
            condition.field.startswith("htf_")
            or (isinstance(condition.value, str) and condition.value.startswith("htf_"))
            for group in preset.rules
            for condition in group.conditions
        )
    ],
)
def test_registered_swing_presets_receive_aligned_closed_higher_timeframe_bars(
    slug: str, timeframe: str, higher_timeframe: str
) -> None:
    preset = _PROFILE_PRESETS[slug]
    base = _market_bars(timeframe)
    higher = _market_bars(higher_timeframe)
    base_close = base["ts_event_ns"][-1] + 1_000_000
    closed_higher = higher.filter(pl.col("ts_event_ns") + 1_000_000 <= base_close)

    result = replay_entries(
        base,
        preset,
        "BTCUSDT",
        timeframe,
        htf_bars=closed_higher,
    )

    assert isinstance(result, list)


def test_lake_reader_yields_bounded_since_filtered_deduplicated_batches(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    frame = _market_bars("1h")
    duplicate = frame.row(100, named=True)
    duplicate["received_at_ns"] += 1_000_000
    duplicate["close"] += 1.0
    frame = pl.concat([frame, pl.DataFrame([duplicate])]).sort("ts_event_ns")
    path = (
        lake / "binance_klines_enriched" / "binance" / "spot" / "1h" / "BTCUSDT" / "date=2026-04-14"
    )
    path.mkdir(parents=True)
    frame.write_parquet(path / "part.parquet")

    with LakeReader(lake, batch_rows=17) as reader:
        batches = list(reader.iter_batches("BTCUSDT", "1h", _ns(date(2026, 4, 15))))
    loaded = pl.concat(batches)

    assert max(batch.height for batch in batches) <= 17
    assert loaded.height == 236
    assert loaded["ts_event_ns"].n_unique() == 236
    duplicate_event_ns = int(frame.row(100, named=True)["ts_event_ns"])
    selected = loaded.filter(pl.col("ts_event_ns") == duplicate_event_ns)
    assert selected.height == 1
    assert selected["close"][0] == duplicate["close"]


def test_cli_reads_synthetic_lake_deduplicates_since_and_writes_every_preset_coin_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lake = tmp_path / "lake"
    for coin in ("BTCUSDT", "ETHUSDT"):
        for timeframe in ("15m", "1h", "4h", "1d"):
            frame = _market_bars(timeframe)
            if timeframe == "1h" and coin == "BTCUSDT":
                duplicate = frame.row(100, named=True)
                duplicate["received_at_ns"] += 1_000_000
                duplicate["close"] += 1.0
                frame = pl.concat([frame, pl.DataFrame([duplicate])]).sort("ts_event_ns")
            partition_date = datetime.fromtimestamp(
                frame["ts_event_ns"][0] / 1_000_000_000, UTC
            ).date()
            path = (
                lake
                / "binance_klines_enriched"
                / "binance"
                / "spot"
                / timeframe
                / coin
                / f"date={partition_date}"
            )
            path.mkdir(parents=True)
            frame.write_parquet(path / "part.parquet")
    lake_before = {
        str(path.relative_to(lake)): path.read_bytes() for path in lake.rglob("*.parquet")
    }

    out = tmp_path / "report.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "preset_replay.py",
            "--lake",
            str(lake),
            "--coins",
            "BTCUSDT,ETHUSDT",
            "--since",
            "2026-04-15",
            "--out",
            str(out),
        ],
    )

    main()
    report = json.loads(out.read_text(encoding="utf-8"))
    results = report["results"]

    assert len(results) == len(_PROFILE_PRESETS) * 2
    assert {(row["preset"], row["coin"]) for row in results} == {
        (slug, coin) for slug in _PROFILE_PRESETS for coin in ("BTCUSDT", "ETHUSDT")
    }
    assert all(row["first_bar"] is not None and row["last_bar"] is not None for row in results)
    assert min(row["first_bar"] for row in results) >= "2026-04-15T00:00:00+00:00"
    lake_after = {
        str(path.relative_to(lake)): path.read_bytes() for path in lake.rglob("*.parquet")
    }
    assert lake_after == lake_before


def _ns(value: date) -> int:
    """Convert a UTC date to its starting epoch nanoseconds for lake query tests."""
    return int(datetime.combine(value, datetime.min.time(), UTC).timestamp()) * 1_000_000_000
