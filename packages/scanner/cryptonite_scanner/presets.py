"""Built-in production scanner presets."""

from cryptonite_scanner.models import (
    RiskConfig,
    RuleCondition,
    RuleGroup,
    RuleOperator,
    ScanSpecCreate,
    UniverseConfig,
)

PRESET_RSI_OVERSOLD = ScanSpecCreate(
    slug="rsi-oversold-reversal",
    name="RSI Oversold Reversal",
    description="Identifies oversold conditions with RSI < 30 and bounce above lower Bollinger Band with volume.",
    universe=UniverseConfig(
        instrument_ids=["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
        timeframes=["15m", "1h"],
    ),
    rules=[
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(field="rsi", operator=RuleOperator.LT, value=30.0),
            ],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.GT, value="bb_lower"),
                RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=1.0),
            ],
        ),
    ],
    risk=RiskConfig(stop_loss_atr_mult=2.0, take_profit_atr_mult=4.0, risk_reward_ratio=2.0),
)

PRESET_TREND_PULLBACK = ScanSpecCreate(
    slug="trend-pullback",
    name="Trend Pullback Bounce",
    description="Scans for uptrending assets (Close > EMA200, EMA20 > EMA50) that pulled back to RSI <= 45 with a bullish candle.",
    universe=UniverseConfig(
        instrument_ids=["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
        timeframes=["15m", "1h"],
    ),
    rules=[
        RuleGroup(
            category="trend",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.GT, value="ema_200"),
                RuleCondition(field="ema_20", operator=RuleOperator.GT, value="ema_50"),
            ],
        ),
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(field="rsi", operator=RuleOperator.LTE, value=45.0),
            ],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.GT, value="open"),
            ],
        ),
    ],
    risk=RiskConfig(stop_loss_atr_mult=2.0, take_profit_atr_mult=4.0, risk_reward_ratio=2.0),
)

PRESET_BOLLINGER_BREAKOUT = ScanSpecCreate(
    slug="bollinger-breakout",
    name="Bollinger Band Volatility Breakout",
    description="Detects momentum breakouts closing above upper Bollinger Band with >= 1.5x volume expansion.",
    universe=UniverseConfig(
        instrument_ids=["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
        timeframes=["5m", "15m", "1h"],
    ),
    rules=[
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.GT, value="bb_upper"),
            ],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[
                RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=1.5),
                RuleCondition(field="close", operator=RuleOperator.GT, value="open"),
            ],
        ),
    ],
    risk=RiskConfig(stop_loss_atr_mult=1.5, take_profit_atr_mult=3.0, risk_reward_ratio=2.0),
)

PRESET_MACD_MOMENTUM = ScanSpecCreate(
    slug="macd-momentum-shift",
    name="MACD Momentum Shift",
    description="Catches upward momentum inflection where MACD Histogram crosses above zero while above EMA50.",
    universe=UniverseConfig(
        instrument_ids=["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
        timeframes=["15m", "1h"],
    ),
    rules=[
        RuleGroup(
            category="trend",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.GT, value="ema_50"),
            ],
        ),
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(field="macd_hist", operator=RuleOperator.GT, value=0.0),
            ],
        ),
    ],
    risk=RiskConfig(stop_loss_atr_mult=2.0, take_profit_atr_mult=4.0, risk_reward_ratio=2.0),
)

PRESET_SCALP_MULTI_TF = ScanSpecCreate(
    slug="scalp-multi-tf",
    name="Execution Strategist Multi-TF",
    description="Captures 5m, 15m, 1H, 4H, and 1D bar close states for the Execution Strategist Engine.",
    universe=UniverseConfig(
        instrument_ids=["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
        timeframes=["5m", "15m", "1h", "4h", "1d"],
    ),
    rules=[
        RuleGroup(
            category="universe",
            logic="AND",
            conditions=[],  # Unconditional capture - engine handles the logic
        ),
    ],
    risk=RiskConfig(stop_loss_atr_mult=0.5, take_profit_atr_mult=2.0, risk_reward_ratio=2.0),
)

BUILTIN_PRESETS = [
    PRESET_RSI_OVERSOLD,
    PRESET_TREND_PULLBACK,
    PRESET_BOLLINGER_BREAKOUT,
    PRESET_MACD_MOMENTUM,
    PRESET_SCALP_MULTI_TF,
]
