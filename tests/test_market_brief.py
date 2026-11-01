"""Market brief (blueprint A2.2): deterministic, compact, built from enriched indicator frames."""

from datetime import UTC, datetime

import polars as pl
from cryptonite_scanner.brief import build_brief
from cryptonite_scanner.engine import ScannerEngine


def frame(n: int = 250, start: float = 100.0, step: float = 0.2) -> pl.DataFrame:
    closes = [start + i * step for i in range(n)]
    return ScannerEngine.enrich_indicators(pl.DataFrame({
        "open": closes, "high": [c + 0.5 for c in closes], "low": [c - 0.5 for c in closes],
        "close": closes, "volume": [1000.0 + (i % 7) * 50 for i in range(n)],
    }))


def test_brief_summarizes_each_timeframe_candidate_and_derivatives():
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    brief = build_brief(
        "BTCUSDT.BINANCE", {"1h": frame(), "4h": frame(step=-0.1)},
        candidate={"timeframe": "1h", "suggested_entry": "149.8", "suggested_stop": "148.8",
                   "suggested_target": "152.8", "matched_rules": ["ema_cross"]},
        derivatives={"mark_price": "149.9", "funding_rate": "0.0001", "open_interest": "85000"},
        last_bar_close_ns=int(now.timestamp() * 1e9) - 120 * 10**9,
        now=now,
    )
    lines = brief.splitlines()
    assert lines[0] == "MARKET BRIEF BTCUSDT.BINANCE (as of 2026-09-25 12:00 UTC)"
    assert "last closed bar 2 min ago" in brief
    assert any(line.startswith("1h:") and "trend up" in line for line in lines)
    assert any(line.startswith("4h:") and "trend down" in line for line in lines)
    assert "reward-to-risk 3.00" in brief and "ema_cross" in brief
    assert "funding 0.0100% per interval" in brief


def test_brief_stays_small():
    frames = {tf: frame() for tf in ("5m", "15m", "1h", "4h", "1d")}
    brief = build_brief("BTCUSDT.BINANCE", frames)
    assert len(brief) / 4 < 2000  # ~4 chars per token


def test_brief_is_deterministic_and_tolerates_empty_frames():
    now = datetime(2026, 9, 25, tzinfo=UTC)
    frames = {"1h": frame(), "4h": pl.DataFrame()}
    assert build_brief("X", frames, now=now) == build_brief("X", frames, now=now)
    assert "4h" not in build_brief("X", frames, now=now)


def test_candidate_direction_from_preset_side_then_stop_side():
    from cryptonite_scanner.brief import candidate_direction as d

    assert d({"preset_slug": "trend-pullback-short", "suggested_entry": "10", "suggested_stop": "9"}) == "SHORT"
    assert d({"spec_slug": "trend-pullback", "suggested_entry": "10", "suggested_stop": "11"}) == "LONG"
    assert d({"suggested_entry": "10", "suggested_stop": "11"}) == "SHORT"
    assert d({"suggested_entry": "10", "suggested_stop": "9"}) == "LONG"
    assert d({"suggested_entry": "10"}) is None and d(None) is None


def test_brief_has_direction_grade_and_replay_line():
    from cryptonite_scanner.brief import brief_data

    cand = {"timeframe": "1h", "spec_slug": "rsi-oversold-reversal", "suggested_entry": "149.8",
            "suggested_stop": "148.8", "suggested_target": "152.8", "matched_rules": []}
    frames = {"1h": frame(), "4h": frame()}
    data = brief_data("BTCUSDT.BINANCE", frames, candidate=cand)
    assert data["direction"] == "LONG" and data["setup_grade"]["grade"] in ("B", "C", "NO_TRADE")
    assert len(data["setup_grade"]["reasons"]) <= 5
    assert data["replay"]["preset"] == "rsi-oversold-reversal" and data["replay"]["trades"] > 0
    line = next(x for x in build_brief("BTCUSDT.BINANCE", frames, candidate=cand).splitlines() if x.startswith("Direction:"))
    assert "Setup grade:" in line and "Replay (rsi-oversold-reversal on BTCUSDT):" in line and "R" in line
    assert "Direction:" not in build_brief("BTCUSDT.BINANCE", frames)  # no candidate, no line


def test_timeframes_against_follows_the_direction():
    from cryptonite_scanner.brief import hard_rejects

    def data(direction, trend):
        return {"direction": direction, "candidate": {}, "timeframes": [
            {"timeframe": "1h", "trend": trend}, {"timeframe": "4h", "trend": trend}]}

    assert hard_rejects(data("LONG", "down")) == ["TIMEFRAMES_AGAINST"]
    assert hard_rejects(data(None, "down")) == ["TIMEFRAMES_AGAINST"]
    assert hard_rejects(data("LONG", "up")) == []
    assert hard_rejects(data("SHORT", "up")) == ["TIMEFRAMES_AGAINST"]
    assert hard_rejects(data("SHORT", "down")) == []
