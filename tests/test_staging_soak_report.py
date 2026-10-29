"""The staging soak report separates only documented fail-closed precision noise."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from staging_soak_report import ERROR_RE, is_known_scanner_precision_noise  # noqa: E402


def test_soak_error_classifier_keeps_recoverable_warning_out_of_failure_count():
    warning = (
        "2026-10-27 22:20:34 [WARNING] cryptonite.stream: WebSocket disconnected "
        "(sent 1011 (internal error) keepalive ping timeout); reconnecting"
    )

    assert not ERROR_RE.search(warning)
    assert ERROR_RE.search("2026-10-27 22:20:34 [ERROR] cryptonite.stream: failed to recover")
    assert ERROR_RE.search("2026-10-27 22:20:34 UTC [123] ERROR: relation missing")
    assert ERROR_RE.search("Traceback (most recent call last):")
    assert ERROR_RE.search("Unhandled exception in worker")


@pytest.mark.parametrize("symbol", ["BNBUSDT", "SOLUSDT"])
def test_documented_unsupported_pair_precision_noise_is_classified(symbol):
    line = (
        f"2026-10-29 20:30:11,241 [ERROR] cryptonite.scanner.engine: Missing or invalid "
        f"price_precision for instrument {symbol}.BINANCE (value=None). Failing closed."
    )

    assert ERROR_RE.search(line)
    assert is_known_scanner_precision_noise(line)


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT", "XRPUSDT", "WLDUSDT", "MUBARAKUSDT"])
def test_other_precision_errors_remain_fatal(symbol):
    line = (
        f"2026-10-29 20:30:11,241 [ERROR] cryptonite.scanner.engine: Missing or invalid "
        f"price_precision for instrument {symbol}.BINANCE (value=None). Failing closed."
    )

    assert ERROR_RE.search(line)
    assert not is_known_scanner_precision_noise(line)


def test_database_errors_remain_fatal():
    line = '2026-10-29 20:30:11 UTC [4777] ERROR: column \"unknown\" does not exist'

    assert ERROR_RE.search(line)
    assert not is_known_scanner_precision_noise(line)
