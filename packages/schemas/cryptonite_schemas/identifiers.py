"""Symbology, identifier translation, and Nautilus BarType utilities."""

from typing import NamedTuple

from nautilus_trader.model import (
    BarAggregation,
    BarSpecification,
    BarType,
    InstrumentId,
    PriceType,
    Symbol,
    Venue,
)

DEFAULT_VENUE = "BINANCE"


class SpotInstrumentId(NamedTuple):
    symbol: str
    venue: str = DEFAULT_VENUE

    def __str__(self) -> str:
        return f"{self.symbol}.{self.venue}"


class PerpInstrumentId(NamedTuple):
    symbol: str
    venue: str = DEFAULT_VENUE

    def __str__(self) -> str:
        return f"{self.symbol}-PERP.{self.venue}"


def to_spot_instrument_id(raw_symbol: str, venue: str = DEFAULT_VENUE) -> str:
    """Format a spot instrument ID, e.g. BTCUSDT -> BTCUSDT.BINANCE."""
    clean = raw_symbol.upper().replace(".BINANCE", "").replace("-PERP", "")
    return f"{clean}.{venue.upper()}"


def to_perp_instrument_id(raw_symbol: str, venue: str = DEFAULT_VENUE) -> str:
    """Format a USD-M perp instrument ID, e.g. BTCUSDT -> BTCUSDT-PERP.BINANCE."""
    clean = raw_symbol.upper().replace(".BINANCE", "").replace("-PERP", "")
    return f"{clean}-PERP.{venue.upper()}"


def parse_instrument_id(instrument_id_str: str) -> tuple[str, str, bool]:
    """Parse instrument string into (symbol, venue, is_perp).
    
    Examples:
        'BTCUSDT.BINANCE' -> ('BTCUSDT', 'BINANCE', False)
        'BTCUSDT-PERP.BINANCE' -> ('BTCUSDT', 'BINANCE', True)
    """
    if "." not in instrument_id_str:
        return instrument_id_str, DEFAULT_VENUE, False
    symbol_part, venue_part = instrument_id_str.split(".", 1)
    if symbol_part.endswith("-PERP"):
        return symbol_part[:-5], venue_part, True
    return symbol_part, venue_part, False


def to_nautilus_instrument_id(instrument_id_str: str) -> InstrumentId:
    """Convert string instrument ID to Nautilus InstrumentId object."""
    if "." in instrument_id_str:
        symbol_part, venue_part = instrument_id_str.split(".", 1)
        return InstrumentId(Symbol(symbol_part), Venue(venue_part))
    return InstrumentId(Symbol(instrument_id_str), Venue(DEFAULT_VENUE))


def parse_timeframe_to_nautilus_spec(timeframe: str) -> BarSpecification:
    """Parse timeframe string like '1m', '5m', '15m', '1h', '1d' into BarSpecification."""
    tf = timeframe.strip().lower()
    if tf.endswith("m"):
        step = int(tf[:-1])
        return BarSpecification(step, BarAggregation.MINUTE, PriceType.LAST)
    elif tf.endswith("h"):
        step = int(tf[:-1])
        return BarSpecification(step, BarAggregation.HOUR, PriceType.LAST)
    elif tf.endswith("d"):
        step = int(tf[:-1])
        return BarSpecification(step, BarAggregation.DAY, PriceType.LAST)
    elif tf.endswith("s"):
        step = int(tf[:-1])
        return BarSpecification(step, BarAggregation.SECOND, PriceType.LAST)
    else:
        raise ValueError(f"Unsupported timeframe: {timeframe}")


def to_nautilus_bar_type(instrument_id: str | InstrumentId, timeframe: str) -> BarType:
    """Create a Nautilus BarType from an instrument ID and timeframe."""
    if isinstance(instrument_id, str):
        inst_obj = to_nautilus_instrument_id(instrument_id)
    else:
        inst_obj = instrument_id
    bar_spec = parse_timeframe_to_nautilus_spec(timeframe)
    return BarType(inst_obj, bar_spec)
