"""Retry policy: data-integrity errors fail a backtest run at once; other errors retry until attempts run out."""

import pytest

from cryptonite_backtester import DataGapError, DataIntegrityError
from cryptonite_backtester_service.worker import is_permanent_failure


@pytest.mark.parametrize("exc", [DataGapError("gap in window"), DataIntegrityError("bad bars")])
def test_data_integrity_errors_are_permanent_on_first_attempt(exc):
    assert is_permanent_failure(exc, attempt_count=1, max_attempts=3)


def test_transient_errors_retry_until_attempts_are_used():
    assert not is_permanent_failure(RuntimeError("redis timeout"), attempt_count=1, max_attempts=3)
    assert not is_permanent_failure(RuntimeError("redis timeout"), attempt_count=2, max_attempts=3)
    assert is_permanent_failure(RuntimeError("redis timeout"), attempt_count=3, max_attempts=3)
