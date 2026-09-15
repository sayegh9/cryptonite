"""Cryptonite Strategy Registry and implementations."""

from cryptonite_strategies.base import CryptoniteStrategy, CryptoniteStrategyConfig
from cryptonite_strategies.ema_cross import EMACrossoverConfig, EMACrossoverStrategy
from cryptonite_strategies.rsi_reversal import (
    RSIOversoldReversalConfig,
    RSIOversoldReversalStrategy,
)
from cryptonite_strategies.supertrend import (
    SupertrendBreakoutConfig,
    SupertrendBreakoutStrategy,
)
from cryptonite_strategies.registry import (
    STRATEGY_REGISTRY,
    get_source_tree_hash,
    get_strategy_class,
    get_strategy_config_class,
    get_strategy_meta,
    get_strategy_source_hash,
    list_strategies,
)

__all__ = [
    "CryptoniteStrategy",
    "CryptoniteStrategyConfig",
    "EMACrossoverConfig",
    "EMACrossoverStrategy",
    "RSIOversoldReversalConfig",
    "RSIOversoldReversalStrategy",
    "SupertrendBreakoutConfig",
    "SupertrendBreakoutStrategy",
    "STRATEGY_REGISTRY",
    "get_source_tree_hash",
    "get_strategy_class",
    "get_strategy_config_class",
    "get_strategy_meta",
    "get_strategy_source_hash",
    "list_strategies",
]
