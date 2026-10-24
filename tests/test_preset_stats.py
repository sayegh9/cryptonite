"""Synthetic contract tests for the read-only preset replay (ADR 0011, P1.9)."""

import json

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
    replay_entries,
    report_document,
    summarize_replay,
)


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
