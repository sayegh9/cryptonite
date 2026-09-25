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
