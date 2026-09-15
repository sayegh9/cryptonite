"""Execution profiles and venue resolution for Nautilus backtesting."""

from typing import Any
from nautilus_trader.backtest import BacktestVenueConfig
from nautilus_trader.execution import (
    DefaultFillModel,
    MakerTakerFeeModel,
    OneTickSlippageFillModel,
    ProbabilisticFillModel,
)
from nautilus_trader.model import AccountType, BookType, OmsType

VALID_PROFILES = ("optimistic", "adverse_slippage", "conservative")

PROFILE_METADATA: dict[str, dict[str, Any]] = {
    "optimistic": {
        "name": "optimistic",
        "label": "Baseline (Optimistic)",
        "description": "Baseline fill model with zero slippage, canonical Open->High->Low->Close path.",
        "fill_model": "DefaultFillModel",
        "book_type": "L1_MBP",
        "bar_adaptive_high_low_ordering": False,
        "prob_fill_on_limit": 1.0,
        "prob_slippage": 0.0,
        "maker_fee_bps": 0.0,
        "taker_fee_bps": 0.0,
    },
    "adverse_slippage": {
        "name": "adverse_slippage",
        "label": "Adverse Slippage (Stress)",
        "description": "Stress test with OneTickSlippageFillModel forcing 1 adverse tick per fill.",
        "fill_model": "OneTickSlippageFillModel",
        "book_type": "L1_MBP",
        "bar_adaptive_high_low_ordering": False,
        "prob_fill_on_limit": 1.0,
        "prob_slippage": 1.0,
        "maker_fee_bps": 0.0,
        "taker_fee_bps": 0.0,
    },
    "conservative": {
        "name": "conservative",
        "label": "Conservative Institutional",
        "description": "ProbabilisticFillModel with 80% limit fill probability, 50% slippage probability, and maker/taker fees.",
        "fill_model": "ProbabilisticFillModel",
        "book_type": "L1_MBP",
        "bar_adaptive_high_low_ordering": False,
        "prob_fill_on_limit": 0.8,
        "prob_slippage": 0.5,
        "maker_fee_bps": 5.0,
        "taker_fee_bps": 10.0,
    },
}


def resolve_execution_profile(
    profile_name: str,
    venue_name: str = "BINANCE",
    starting_balance: str = "10000 USDT",
    random_seed: int = 42,
) -> tuple[BacktestVenueConfig, dict[str, Any]]:
    """Resolve the BacktestVenueConfig and a deterministic configuration dictionary.
    
    The configuration dictionary is hashed into run_config_hash for complete reproducibility.
    """
    if profile_name not in PROFILE_METADATA:
        raise ValueError(
            f"Unknown execution profile '{profile_name}'. Must be one of {VALID_PROFILES}"
        )

    meta = dict(PROFILE_METADATA[profile_name])
    meta["venue_name"] = venue_name
    meta["starting_balance"] = starting_balance
    meta["random_seed"] = random_seed

    if profile_name == "optimistic":
        fill_model = DefaultFillModel()
    elif profile_name == "adverse_slippage":
        fill_model = OneTickSlippageFillModel()
    elif profile_name == "conservative":
        fill_model = ProbabilisticFillModel(
            prob_fill_on_limit=0.8,
            prob_slippage=0.5,
            random_seed=random_seed,
        )
    else:
        fill_model = DefaultFillModel()

    venue_cfg = BacktestVenueConfig(
        name=venue_name,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        starting_balances=[starting_balance],
        bar_execution=True,
        book_type=BookType.L1_MBP,
        bar_adaptive_high_low_ordering=False,
        fill_model=fill_model,
        fee_model=MakerTakerFeeModel(),
    )

    return venue_cfg, meta
