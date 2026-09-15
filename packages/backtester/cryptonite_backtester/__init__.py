"""Canonical Backtesting Engine for Cryptonite."""

from cryptonite_backtester.cadence import (
    DataGapError,
    DataIntegrityError,
    InsufficientWarmupError,
    validate_bar_cadence,
)
from cryptonite_backtester.execution_profiles import (
    PROFILE_METADATA,
    VALID_PROFILES,
    resolve_execution_profile,
)
from cryptonite_backtester.metrics import calculate_performance_metrics
from cryptonite_backtester.runner import BacktestRunRequest, BacktestRunner
from cryptonite_backtester.storage import BacktestArtifactStore

__all__ = [
    "BacktestArtifactStore",
    "BacktestRunner",
    "BacktestRunRequest",
    "DataGapError",
    "DataIntegrityError",
    "InsufficientWarmupError",
    "PROFILE_METADATA",
    "VALID_PROFILES",
    "calculate_performance_metrics",
    "resolve_execution_profile",
    "validate_bar_cadence",
]
