"""Static strategy registry and source code verification for Cryptonite."""

import hashlib
import inspect
from pathlib import Path
from typing import Any

from cryptonite_strategies.ema_cross import EMACrossoverConfig, EMACrossoverStrategy
from cryptonite_strategies.rsi_reversal import (
    RSIOversoldReversalConfig,
    RSIOversoldReversalStrategy,
)
from cryptonite_strategies.supertrend import (
    SupertrendBreakoutConfig,
    SupertrendBreakoutStrategy,
)

STRATEGY_REGISTRY: dict[str, dict[str, Any]] = {
    "ema-cross": {
        "slug": "ema-cross",
        "name": "EMA Crossover Strategy",
        "description": "Trend-following strategy that enters long when fast EMA crosses above slow EMA, and exits when fast EMA crosses below slow EMA.",
        "author": "Cryptonite Core",
        "version_num": 1,
        "strategy_cls": EMACrossoverStrategy,
        "config_cls": EMACrossoverConfig,
        "default_parameters": {
            "fast_period": 9,
            "slow_period": 21,
            "trade_size": 0.01,
        },
        "parameter_schema": {
            "fast_period": {
                "type": "integer",
                "default": 9,
                "minimum": 2,
                "maximum": 200,
                "title": "Fast EMA Period",
                "description": "Period for the fast exponential moving average",
            },
            "slow_period": {
                "type": "integer",
                "default": 21,
                "minimum": 3,
                "maximum": 500,
                "title": "Slow EMA Period",
                "description": "Period for the slow exponential moving average",
            },
            "trade_size": {
                "type": "number",
                "default": 0.01,
                "minimum": 0.0001,
                "title": "Trade Size (BTC)",
                "description": "Quantity per trade order",
            },
        },
    },
    "rsi-reversal": {
        "slug": "rsi-reversal",
        "name": "RSI Oversold Reversal",
        "description": "Mean-reversion strategy entering long on oversold recovery and closing when RSI returns to neutral or overbought levels.",
        "author": "Cryptonite Core",
        "version_num": 1,
        "strategy_cls": RSIOversoldReversalStrategy,
        "config_cls": RSIOversoldReversalConfig,
        "default_parameters": {
            "rsi_period": 14,
            "oversold_threshold": 30.0,
            "exit_threshold": 50.0,
            "trade_size": 0.01,
        },
        "parameter_schema": {
            "rsi_period": {
                "type": "integer",
                "default": 14,
                "minimum": 2,
                "maximum": 100,
                "title": "RSI Period",
                "description": "Period for calculating RSI",
            },
            "oversold_threshold": {
                "type": "number",
                "default": 30.0,
                "minimum": 1.0,
                "maximum": 50.0,
                "title": "Oversold Threshold",
                "description": "RSI level below which an asset is considered oversold",
            },
            "exit_threshold": {
                "type": "number",
                "default": 50.0,
                "minimum": 30.0,
                "maximum": 90.0,
                "title": "Exit Threshold",
                "description": "RSI level to close long position",
            },
            "trade_size": {
                "type": "number",
                "default": 0.01,
                "minimum": 0.0001,
                "title": "Trade Size (BTC)",
                "description": "Quantity per trade order",
            },
        },
    },
    "supertrend": {
        "slug": "supertrend",
        "name": "Supertrend Breakout",
        "description": "Volatility-based trend breakout strategy using Average True Range (ATR) dynamic bands.",
        "author": "Cryptonite Core",
        "version_num": 1,
        "strategy_cls": SupertrendBreakoutStrategy,
        "config_cls": SupertrendBreakoutConfig,
        "default_parameters": {
            "atr_period": 10,
            "multiplier": 3.0,
            "trade_size": 0.01,
        },
        "parameter_schema": {
            "atr_period": {
                "type": "integer",
                "default": 10,
                "minimum": 2,
                "maximum": 100,
                "title": "ATR Period",
                "description": "Lookback period for Average True Range",
            },
            "multiplier": {
                "type": "number",
                "default": 3.0,
                "minimum": 0.5,
                "maximum": 10.0,
                "title": "ATR Multiplier",
                "description": "Band offset multiplier of ATR",
            },
            "trade_size": {
                "type": "number",
                "default": 0.01,
                "minimum": 0.0001,
                "title": "Trade Size (BTC)",
                "description": "Quantity per trade order",
            },
        },
    },
}


def get_strategy_source_hash(slug: str) -> str:
    """Calculate SHA-256 hash of the strategy's source code file."""
    if slug not in STRATEGY_REGISTRY:
        raise KeyError(f"Strategy slug '{slug}' not found in STRATEGY_REGISTRY")
    cls = STRATEGY_REGISTRY[slug]["strategy_cls"]
    src_file = inspect.getsourcefile(cls)
    if not src_file or not Path(src_file).exists():
        raise FileNotFoundError(f"Source file for strategy '{slug}' could not be located")
    content = Path(src_file).read_bytes()
    return hashlib.sha256(content).hexdigest()


def get_source_tree_hash() -> str:
    """Calculate combined SHA-256 hash across all strategy source files."""
    pkg_dir = Path(__file__).parent
    files = sorted(pkg_dir.glob("*.py"))
    hasher = hashlib.sha256()
    for f in files:
        hasher.update(f.name.encode())
        hasher.update(f.read_bytes())
    return hasher.hexdigest()


def get_strategy_meta(slug: str) -> dict[str, Any]:
    """Retrieve metadata dict for a registered strategy slug."""
    if slug not in STRATEGY_REGISTRY:
        raise KeyError(f"Strategy '{slug}' not registered")
    entry = STRATEGY_REGISTRY[slug]
    return {
        "slug": entry["slug"],
        "name": entry["name"],
        "description": entry["description"],
        "author": entry["author"],
        "version_num": entry["version_num"],
        "default_parameters": entry["default_parameters"],
        "parameter_schema": entry["parameter_schema"],
        "code_hash": get_strategy_source_hash(slug),
    }


def list_strategies() -> list[dict[str, Any]]:
    """List all registered strategies with computed code hashes."""
    return [get_strategy_meta(slug) for slug in sorted(STRATEGY_REGISTRY.keys())]


def get_strategy_class(slug: str):
    """Retrieve Strategy class for slug."""
    if slug not in STRATEGY_REGISTRY:
        raise KeyError(f"Strategy '{slug}' not registered")
    return STRATEGY_REGISTRY[slug]["strategy_cls"]


def get_strategy_config_class(slug: str):
    """Retrieve StrategyConfig class for slug."""
    if slug not in STRATEGY_REGISTRY:
        raise KeyError(f"Strategy '{slug}' not registered")
    return STRATEGY_REGISTRY[slug]["config_cls"]
