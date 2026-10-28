"""The staging soak gate counts error-level logs without promoting recoverable warnings."""

import importlib.util
from pathlib import Path


def _soak_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "staging_soak_report.py"
    spec = importlib.util.spec_from_file_location("staging_soak_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_soak_error_classifier_keeps_recoverable_warning_out_of_failure_count():
    error_re = _soak_module().ERROR_RE
    warning = ("2026-10-27 22:20:34 [WARNING] cryptonite.stream: WebSocket disconnected "
               "(sent 1011 (internal error) keepalive ping timeout); reconnecting")
    assert not error_re.search(warning)
    assert error_re.search("2026-10-27 22:20:34 [ERROR] cryptonite.stream: failed to recover")
    assert error_re.search("2026-10-27 22:20:34 UTC [123] ERROR: relation missing")
    assert error_re.search("Traceback (most recent call last):")
    assert error_re.search("Unhandled exception in worker")
