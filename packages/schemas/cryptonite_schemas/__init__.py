"""Cryptonite shared domain schemas and symbology."""

try:
    from cryptonite_schemas.bar import (
        ANALYTICS_PYARROW_SCHEMA,
        EnrichedBar,
        RawBinanceKline,
        bar_to_nautilus,
        bars_to_arrow_table,
        compute_payload_checksum,
    )
except ImportError:
    pass
try:
    from cryptonite_schemas.identifiers import (
        PerpInstrumentId,
        SpotInstrumentId,
        parse_instrument_id,
        to_nautilus_bar_type,
        to_nautilus_instrument_id,
        to_perp_instrument_id,
        to_spot_instrument_id,
    )
except ImportError:
    pass
from cryptonite_schemas.sources import (
    BarSource,
    InstrumentSource,
    OrderBookDeltaSource,
    QuoteSource,
    TradeSource,
)

__all__ = [
    "ANALYTICS_PYARROW_SCHEMA",
    "BarSource",
    "EnrichedBar",
    "InstrumentSource",
    "OrderBookDeltaSource",
    "PerpInstrumentId",
    "QuoteSource",
    "RawBinanceKline",
    "SpotInstrumentId",
    "TradeSource",
    "bar_to_nautilus",
    "bars_to_arrow_table",
    "compute_payload_checksum",
    "parse_instrument_id",
    "to_nautilus_bar_type",
    "to_nautilus_instrument_id",
    "to_perp_instrument_id",
    "to_spot_instrument_id",
]
from cryptonite_schemas.strategist import (
    MultiTimeframeSnapshot,
    StrategistConfig,
    StrategistSignal,
    TimeframeState,
    WebhookAlert,
)

__all__.extend([
    "MultiTimeframeSnapshot",
    "StrategistConfig",
    "StrategistSignal",
    "TimeframeState",
    "WebhookAlert",
])
