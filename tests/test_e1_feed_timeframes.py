"""E1 feed timeframes: ingest emits 1m, 15m, 1h, 4h closed bars so D3 automatic triggers can fire.

Static validation of ``infra/docker-compose.yml`` (nothing is started) — matches the
``test_compose_supervision.py`` pattern. Guards the config that lets the D3 structure
scanner's 15m/4h triggers receive committed bars, and asserts the perp-quote/spot config
that must stay untouched.

The one-line mapping under test is the ingest service's ``INGEST_TIMEFRAMES`` env:
recorder/stream subscribe ``kline_<tf>`` for every entry (``stream.py``), Binance delivers
the interval natively, and ``parse_timeframe_to_nautilus_spec`` resolves the bar step.
"""

from pathlib import Path

import pytest
import yaml
from cryptonite_schemas.identifiers import (
    parse_timeframe_to_nautilus_spec,
    to_nautilus_bar_type,
)
from nautilus_trader.model import BarAggregation

REPO = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((REPO / "infra" / "docker-compose.yml").read_text())
INGEST_ENV = COMPOSE["services"]["ingest"]["environment"]

# Binance kline intervals the WS *and* REST endpoints accept (''@kline_<interval>'').
BINANCE_KLINE_INTERVALS = {
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
}

# D3's scanner universe ("scalp-multi-tf" preset, 15m/1h/4h) — the intervals whose
# committed bars were absent before this change.
D3_TRIGGER_TIMEFRAMES = {"15m", "4h"}


def configured_timeframes() -> list[str]:
    raw = INGEST_ENV["INGEST_TIMEFRAMES"]
    return [t.strip() for t in raw.split(",") if t.strip()]


def test_ingest_timeframes_include_all_d3_trigger_intervals():
    assert set(configured_timeframes()) == {"1m", "15m", "1h", "4h"}
    assert D3_TRIGGER_TIMEFRAMES <= set(configured_timeframes())


def test_every_configured_timeframe_is_a_binance_interval():
    assert set(configured_timeframes()) <= BINANCE_KLINE_INTERVALS


@pytest.mark.parametrize(
    "timeframe,step,aggregation",
    [
        ("1m", 1, BarAggregation.MINUTE),
        ("15m", 15, BarAggregation.MINUTE),
        ("1h", 1, BarAggregation.HOUR),
        ("4h", 4, BarAggregation.HOUR),
    ],
)
def test_configured_timeframes_map_to_nautilus_bar_specs(timeframe, step, aggregation):
    spec = parse_timeframe_to_nautilus_spec(timeframe)
    assert (spec.step, spec.aggregation) == (step, aggregation)
    # full BarType resolution must succeed for the record/analytics-lake path
    assert to_nautilus_bar_type("BTCUSDT.BINANCE", timeframe) is not None


def test_ws_subscription_stream_names_for_configured_timeframes():
    expected = {f"kline_{tf}" for tf in configured_timeframes()}
    assert expected == {"kline_1m", "kline_15m", "kline_1h", "kline_4h"}


def test_perp_quote_and_spot_configuration_are_preserved():
    # Perp bookTicker quotes -> <SYM>-PERP.BINANCE (must not regress with the timeframe change)
    assert INGEST_ENV["INGEST_PERP_SYMBOLS"] == "BTCUSDT,ETHUSDT"
    # Spot quote throttling and the spot symbol set stay as-is.
    assert INGEST_ENV["INGEST_SPOT_QUOTE_HZ"] == "50"
    assert INGEST_ENV["INGEST_SYMBOLS"] == "BTCUSDT,ETHUSDT,XRPUSDT,WLDUSDT"
    # The perp quote series is quotes-only (no bars), unchanged by the kline timeframe list.
    assert INGEST_ENV["REDIS_URL"].endswith("/0")
