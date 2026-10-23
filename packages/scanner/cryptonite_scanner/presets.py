"""Built-in production scanner presets."""

import json
from typing import Any
from uuid import uuid4

from cryptonite_scanner.models import (
    RiskConfig,
    RuleCondition,
    RuleGroup,
    RuleOperator,
    ScanSpecCreate,
    UniverseConfig,
    compute_spec_hash,
)

PRESET_RSI_OVERSOLD = ScanSpecCreate(
    slug="rsi-oversold-reversal",
    name="RSI Oversold Reversal",
    description="Identifies oversold reversals where price crosses above lower Bollinger Band with RSI <= 35 and volume.",
    universe=UniverseConfig(
        instrument_ids=["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
        timeframes=["15m", "1h"],
    ),
    rules=[
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.CROSS_ABOVE, value="bb_lower", lookback=1),
            ],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[
                RuleCondition(field="rsi", operator=RuleOperator.LTE, value=35.0, lookback=0),
                RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=1.0, lookback=0),
            ],
        ),
    ],
    risk=RiskConfig(stop_loss_atr_mult=2.0, take_profit_atr_mult=4.0, risk_reward_ratio=2.0),
)

PRESET_TREND_PULLBACK = ScanSpecCreate(
    slug="trend-pullback",
    name="Trend Pullback Bounce",
    description="Scans for uptrending assets (Close > EMA200, EMA20 > EMA50) where RSI crosses above 40 out of a pullback with a bullish candle.",
    universe=UniverseConfig(
        instrument_ids=["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
        timeframes=["15m", "1h"],
    ),
    rules=[
        RuleGroup(
            category="trend",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.GT, value="ema_200", lookback=0),
                RuleCondition(field="ema_20", operator=RuleOperator.GT, value="ema_50", lookback=0),
            ],
        ),
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(field="rsi", operator=RuleOperator.CROSS_ABOVE, value=40.0, lookback=1),
            ],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.GT, value="open", lookback=0),
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
        timeframes=["15m", "1h"],
    ),
    rules=[
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.CROSS_ABOVE, value="bb_upper", lookback=1),
            ],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[
                RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=1.5, lookback=0),
                RuleCondition(field="close", operator=RuleOperator.GT, value="open", lookback=0),
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
                RuleCondition(field="close", operator=RuleOperator.GT, value="ema_50", lookback=0),
            ],
        ),
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(field="macd_hist", operator=RuleOperator.CROSS_ABOVE, value=0.0, lookback=1),
            ],
        ),
    ],
    risk=RiskConfig(stop_loss_atr_mult=2.0, take_profit_atr_mult=4.0, risk_reward_ratio=2.0),
)

PRESET_SCALP_MULTI_TF = ScanSpecCreate(
    slug="scalp-multi-tf",
    name="Execution Strategist Multi-TF",
    description="Captures 15m, 1H and 4H bar close states for the Execution Strategist Engine.",
    universe=UniverseConfig(
        instrument_ids=["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
        timeframes=["15m", "1h", "4h"],  # no 1d (review-plan Q8, A1a M2: a 300-day lookback per event)
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

PRESET_SCALP_MULTI_TF_SHORT = ScanSpecCreate(
    slug="scalp-multi-tf-short",
    name="Execution Strategist Multi-TF Short",
    description="Captures 15m, 1H and 4H bar close states for the Execution Strategist Engine in a downtrend.",
    universe=UniverseConfig(
        instrument_ids=["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
        timeframes=["15m", "1h", "4h"],
    ),
    rules=[
        RuleGroup(
            category="trend",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.LT, value="ema_50", lookback=0)],
        ),
    ],
    risk=RiskConfig(direction="SHORT", stop_loss_atr_mult=0.5, take_profit_atr_mult=2.0, risk_reward_ratio=2.0),
)

PRESET_TREND_PULLBACK_SHORT = ScanSpecCreate(
    slug="trend-pullback-short",
    name="Trend Pullback Rejection",
    description="Scans for downtrending assets (Close < EMA200, EMA20 < EMA50) where RSI crosses below 60 on a bearish candle.",
    universe=UniverseConfig(
        instrument_ids=["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
        timeframes=["15m", "1h"],
    ),
    rules=[
        RuleGroup(
            category="trend",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.LT, value="ema_200", lookback=0),
                RuleCondition(field="ema_20", operator=RuleOperator.LT, value="ema_50", lookback=0),
            ],
        ),
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="rsi", operator=RuleOperator.CROSS_BELOW, value=60.0, lookback=1)],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.LT, value="open", lookback=0)],
        ),
    ],
    risk=RiskConfig(direction="SHORT", stop_loss_atr_mult=2.0, take_profit_atr_mult=4.0, risk_reward_ratio=2.0),
)

PRESET_BOLLINGER_BREAKDOWN_SHORT = ScanSpecCreate(
    slug="bollinger-breakdown-short",
    name="Bollinger Band Volatility Breakdown",
    description="Detects downside momentum closing below the lower Bollinger Band with >= 1.5x volume expansion.",
    universe=UniverseConfig(
        instrument_ids=["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
        timeframes=["15m", "1h"],
    ),
    rules=[
        RuleGroup(
            category="trend",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.LT, value="ema_50", lookback=0)],
        ),
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.CROSS_BELOW, value="bb_lower", lookback=1)],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[
                RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=1.5, lookback=0),
                RuleCondition(field="close", operator=RuleOperator.LT, value="open", lookback=0),
            ],
        ),
    ],
    risk=RiskConfig(direction="SHORT", stop_loss_atr_mult=1.5, take_profit_atr_mult=3.0, risk_reward_ratio=2.0),
)

BUILTIN_PRESETS = [
    PRESET_RSI_OVERSOLD,
    PRESET_TREND_PULLBACK,
    PRESET_BOLLINGER_BREAKOUT,
    PRESET_MACD_MOMENTUM,
    PRESET_SCALP_MULTI_TF,
]

SWING_LIQUID_12: list[str] = [
    "BTCUSDT.BINANCE",
    "ETHUSDT.BINANCE",
    "SOLUSDT.BINANCE",
    "BNBUSDT.BINANCE",
    "XRPUSDT.BINANCE",
    "DOGEUSDT.BINANCE",
    "ADAUSDT.BINANCE",
    "AVAXUSDT.BINANCE",
    "LINKUSDT.BINANCE",
    "SUIUSDT.BINANCE",
    "NEARUSDT.BINANCE",
    "WLDUSDT.BINANCE",
]

PRESET_SWING_TREND_PULLBACK_1H = ScanSpecCreate(
    slug="swing-trend-pullback-1h",
    name="Swing Trend Pullback (1H/4H)",
    description="1H swing trend pullback setup requiring HTF trend confluence (4H Close > EMA50) with RSI pullback bounce and volume.",
    universe=UniverseConfig(
        instrument_ids=SWING_LIQUID_12,
        timeframes=["15m", "1h", "4h"],
    ),
    rules=[
        RuleGroup(
            category="trend",
            logic="AND",
            conditions=[
                RuleCondition(field="htf_close", operator=RuleOperator.GT, value="htf_ema_50", lookback=0),
                RuleCondition(field="close", operator=RuleOperator.GT, value="ema_50", lookback=0),
            ],
        ),
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(field="rsi", operator=RuleOperator.CROSS_ABOVE, value=40.0, lookback=1),
            ],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.GT, value="open", lookback=0),
                RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=0.9, lookback=0),
            ],
        ),
    ],
    risk=RiskConfig(min_risk_reward=1.8),
)

PRESET_SWING_TREND_PULLBACK_SHORT_1H = ScanSpecCreate(
    slug="swing-trend-pullback-short-1h",
    name="Swing Trend Pullback Short (1H/4H)",
    description="1H swing downtrend pullback setup requiring HTF trend confluence (4H Close < EMA50) with RSI rejection and volume.",
    universe=UniverseConfig(instrument_ids=SWING_LIQUID_12, timeframes=["15m", "1h", "4h"]),
    rules=[
        RuleGroup(
            category="trend",
            logic="AND",
            conditions=[
                RuleCondition(field="htf_close", operator=RuleOperator.LT, value="htf_ema_50", lookback=0),
                RuleCondition(field="close", operator=RuleOperator.LT, value="ema_50", lookback=0),
            ],
        ),
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="rsi", operator=RuleOperator.CROSS_BELOW, value=60.0, lookback=1)],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.LT, value="open", lookback=0),
                RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=0.9, lookback=0),
            ],
        ),
    ],
    risk=RiskConfig(direction="SHORT", min_risk_reward=1.8),
)

PRESET_SWING_SNIPER_PULLBACK_15M = ScanSpecCreate(
    slug="swing-sniper-pullback-15m",
    name="Swing Sniper Pullback (15m/1H/4H)",
    description="15m sniper entry into 1H/4H swing uptrend (HTF Close > EMA50) with lower Bollinger Band oversold bounce and volume.",
    universe=UniverseConfig(
        instrument_ids=SWING_LIQUID_12,
        timeframes=["15m", "1h", "4h"],
    ),
    rules=[
        RuleGroup(
            category="trend",
            logic="AND",
            conditions=[
                RuleCondition(field="htf_close", operator=RuleOperator.GT, value="htf_ema_50", lookback=0),
            ],
        ),
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.CROSS_ABOVE, value="bb_lower", lookback=1),
            ],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.GT, value="open", lookback=0),
                RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=0.9, lookback=0),
            ],
        ),
    ],
    risk=RiskConfig(min_risk_reward=1.8),
)

PRESET_SWING_SNIPER_PULLBACK_SHORT_15M = ScanSpecCreate(
    slug="swing-sniper-pullback-short-15m",
    name="Swing Sniper Pullback Short (15m/1H/4H)",
    description="15m sniper entry into 1H/4H swing downtrend (HTF Close < EMA50) with upper Bollinger Band overbought rejection and volume.",
    universe=UniverseConfig(instrument_ids=SWING_LIQUID_12, timeframes=["15m", "1h", "4h"]),
    rules=[
        RuleGroup(
            category="trend",
            logic="AND",
            conditions=[RuleCondition(field="htf_close", operator=RuleOperator.LT, value="htf_ema_50", lookback=0)],
        ),
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.CROSS_BELOW, value="bb_upper", lookback=1)],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.LT, value="open", lookback=0),
                RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=0.9, lookback=0),
            ],
        ),
    ],
    risk=RiskConfig(direction="SHORT", min_risk_reward=1.8),
)

PRESET_SWING_SQUEEZE_BREAKOUT_1H = ScanSpecCreate(
    slug="swing-squeeze-breakout-1h",
    name="Swing Volatility Breakout (1H/4H)",
    description="1H volatility breakout closing above upper Bollinger Band with HTF trend alignment (4H Close > EMA50) and volume expansion.",
    universe=UniverseConfig(
        instrument_ids=SWING_LIQUID_12,
        timeframes=["15m", "1h", "4h"],
    ),
    rules=[
        RuleGroup(
            category="trend",
            logic="AND",
            conditions=[
                RuleCondition(field="htf_close", operator=RuleOperator.GT, value="htf_ema_50", lookback=0),
            ],
        ),
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.CROSS_ABOVE, value="bb_upper", lookback=1),
            ],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.GT, value="open", lookback=0),
                RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=0.9, lookback=0),
            ],
        ),
    ],
    risk=RiskConfig(min_risk_reward=1.8),
)

PRESET_SWING_SQUEEZE_BREAKDOWN_SHORT_1H = ScanSpecCreate(
    slug="swing-squeeze-breakdown-short-1h",
    name="Swing Volatility Breakdown Short (1H/4H)",
    description="1H volatility breakdown closing below the lower Bollinger Band with HTF downtrend alignment (4H Close < EMA50) and volume.",
    universe=UniverseConfig(instrument_ids=SWING_LIQUID_12, timeframes=["15m", "1h", "4h"]),
    rules=[
        RuleGroup(
            category="trend",
            logic="AND",
            conditions=[RuleCondition(field="htf_close", operator=RuleOperator.LT, value="htf_ema_50", lookback=0)],
        ),
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.CROSS_BELOW, value="bb_lower", lookback=1)],
        ),
        RuleGroup(
            category="confirmation",
            logic="AND",
            conditions=[
                RuleCondition(field="close", operator=RuleOperator.LT, value="open", lookback=0),
                RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=0.9, lookback=0),
            ],
        ),
    ],
    risk=RiskConfig(direction="SHORT", min_risk_reward=1.8),
)

SWING_PRESETS: list[ScanSpecCreate] = [
    PRESET_SWING_TREND_PULLBACK_1H,
    PRESET_SWING_SNIPER_PULLBACK_15M,
    PRESET_SWING_SQUEEZE_BREAKOUT_1H,
]

_PROFILE_PRESETS: dict[str, ScanSpecCreate] = {
    preset.slug: preset for preset in [
        *BUILTIN_PRESETS,
        *SWING_PRESETS,
        PRESET_SCALP_MULTI_TF_SHORT,
        PRESET_TREND_PULLBACK_SHORT,
        PRESET_BOLLINGER_BREAKDOWN_SHORT,
        PRESET_SWING_TREND_PULLBACK_SHORT_1H,
        PRESET_SWING_SNIPER_PULLBACK_SHORT_15M,
        PRESET_SWING_SQUEEZE_BREAKDOWN_SHORT_1H,
    ]
}


def preset_by_slug(slug: str) -> ScanSpecCreate:
    """Resolve one of the twelve ADR 0011 profile presets by its stable slug."""
    try:
        return _PROFILE_PRESETS[slug]
    except KeyError:
        raise KeyError(slug) from None


async def ensure_swing_presets(conn: Any) -> list[str]:
    """Idempotently insert or update the 3 swing trading presets in scan_specs and scan_spec_versions."""
    seeded_slugs: list[str] = []
    for preset in SWING_PRESETS:
        univ_dict = preset.universe.model_dump()
        if preset.risk:
            univ_dict["risk"] = preset.risk.model_dump()
        rules_dict = [rg.model_dump() for rg in preset.rules]
        target_hash = compute_spec_hash(univ_dict, {"rules": rules_dict})

        spec_row = await conn.fetchrow(
            """
            SELECT id, active_version_id, is_archived
            FROM scan_specs
            WHERE slug = $1;
            """,
            preset.slug,
        )

        if not spec_row:
            spec_id = uuid4()
            ver_id = uuid4()
            await conn.execute(
                """
                INSERT INTO scan_specs (id, slug, name, description, active_version_id, is_archived)
                VALUES ($1, $2, $3, $4, NULL, false);
                """,
                spec_id,
                preset.slug,
                preset.name,
                preset.description,
            )
            await conn.execute(
                """
                INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
                VALUES ($1, $2, 1, $3, $4::jsonb, $5::jsonb);
                """,
                ver_id,
                spec_id,
                target_hash,
                json.dumps(univ_dict),
                json.dumps(rules_dict),
            )
            await conn.execute(
                """
                UPDATE scan_specs
                SET active_version_id = $1, is_archived = false, updated_at = now()
                WHERE id = $2;
                """,
                ver_id,
                spec_id,
            )
        else:
            spec_id = spec_row["id"]
            existing_ver = await conn.fetchrow(
                """
                SELECT id FROM scan_spec_versions
                WHERE spec_id = $1 AND spec_hash = $2;
                """,
                spec_id,
                target_hash,
            )
            if existing_ver:
                ver_id = existing_ver["id"]
            else:
                max_ver = await conn.fetchval(
                    "SELECT coalesce(max(version_num), 0) FROM scan_spec_versions WHERE spec_id = $1;",
                    spec_id,
                )
                ver_id = uuid4()
                await conn.execute(
                    """
                    INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb);
                    """,
                    ver_id,
                    spec_id,
                    max_ver + 1,
                    target_hash,
                    json.dumps(univ_dict),
                    json.dumps(rules_dict),
                )
            await conn.execute(
                """
                UPDATE scan_specs
                SET name = $1, description = $2, active_version_id = $3, is_archived = false, updated_at = now()
                WHERE id = $4;
                """,
                preset.name,
                preset.description,
                ver_id,
                spec_id,
            )
        seeded_slugs.append(preset.slug)

    return seeded_slugs
