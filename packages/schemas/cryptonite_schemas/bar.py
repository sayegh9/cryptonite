"""Bar models, exact decimal PyArrow schemas, and Nautilus translation."""

import hashlib
import json
from decimal import Decimal
from typing import Any

import pyarrow as pa
from nautilus_trader.model import (
    Bar,
    BarType,
    Price,
    Quantity,
)
from pydantic import BaseModel, Field

# Canonical PyArrow schema for Enriched Analytics Lake (decimal128(38, 18))
ANALYTICS_PYARROW_SCHEMA = pa.schema([
    ("instrument_id", pa.string()),
    ("bar_type", pa.string()),
    ("schema_version", pa.int16()),
    ("source", pa.string()),
    ("payload_checksum", pa.string()),
    ("ingest_batch_id", pa.string()),
    ("ts_event_ns", pa.uint64()),
    ("ts_init_ns", pa.uint64()),
    ("received_at_ns", pa.uint64()),
    ("price_precision", pa.uint8()),
    ("size_precision", pa.uint8()),
    ("open", pa.decimal128(38, 18)),
    ("high", pa.decimal128(38, 18)),
    ("low", pa.decimal128(38, 18)),
    ("close", pa.decimal128(38, 18)),
    ("volume", pa.decimal128(38, 18)),
    ("quote_volume", pa.decimal128(38, 18)),
    ("taker_buy_base_volume", pa.decimal128(38, 18)),
    ("taker_buy_quote_volume", pa.decimal128(38, 18)),
    ("trades_count", pa.int64()),
    ("open_raw", pa.string()),
    ("high_raw", pa.string()),
    ("low_raw", pa.string()),
    ("close_raw", pa.string()),
    ("volume_raw", pa.string()),
])


class RawBinanceKline(BaseModel):
    """Raw structure from Binance WebSocket or REST kline."""
    open_time_ms: int
    open: str
    high: str
    low: str
    close: str
    volume: str
    close_time_ms: int
    quote_volume: str
    trades_count: int
    taker_buy_base_volume: str
    taker_buy_quote_volume: str
    is_closed: bool = True


class EnrichedBar(BaseModel):
    """Normalized, enriched bar record with exact decimal representations."""
    instrument_id: str
    bar_type: str
    schema_version: int = 1
    source: str = "BINANCE_SPOT_KLINE"
    payload_checksum: str = ""
    ingest_batch_id: str = ""
    ts_event_ns: int = Field(..., description="Kline close time in UTC nanoseconds")
    ts_init_ns: int = Field(..., description="Same as ts_event_ns for deterministic parity")
    received_at_ns: int = Field(..., description="Arrival wall-clock timestamp in nanoseconds")
    price_precision: int
    size_precision: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    taker_buy_base_volume: Decimal
    taker_buy_quote_volume: Decimal
    trades_count: int
    open_raw: str
    high_raw: str
    low_raw: str
    close_raw: str
    volume_raw: str
    is_closed: bool = True

    @classmethod
    def from_binance_raw(
        cls,
        raw: RawBinanceKline,
        instrument_id: str,
        bar_type: str,
        price_precision: int,
        size_precision: int,
        received_at_ns: int,
        ingest_batch_id: str = "",
        payload_checksum: str = "",
    ) -> "EnrichedBar":
        # ts_event_ns is Binance close_time_ms + 1 millisecond into nanoseconds for exact boundary
        # or close_time_ms * 1_000_000 (standard Binance close timestamp nanoseconds)
        # We use (close_time_ms + 1) * 1_000_000 for exact clean bar close matching (e.g. 00:00:59.999 -> 00:01:00.000)
        # or close_time_ms * 1_000_000. Let's use (close_time_ms + 1) * 1_000_000 for exact minute boundary!
        close_boundary_ms = raw.close_time_ms + 1 if (raw.close_time_ms + 1) % 1000 == 0 else raw.close_time_ms
        ts_event_ns = close_boundary_ms * 1_000_000

        return cls(
            instrument_id=instrument_id,
            bar_type=bar_type,
            schema_version=1,
            source="BINANCE_SPOT_KLINE",
            payload_checksum=payload_checksum,
            ingest_batch_id=ingest_batch_id,
            ts_event_ns=ts_event_ns,
            ts_init_ns=ts_event_ns,
            received_at_ns=received_at_ns,
            price_precision=price_precision,
            size_precision=size_precision,
            open=Decimal(raw.open),
            high=Decimal(raw.high),
            low=Decimal(raw.low),
            close=Decimal(raw.close),
            volume=Decimal(raw.volume),
            quote_volume=Decimal(raw.quote_volume),
            taker_buy_base_volume=Decimal(raw.taker_buy_base_volume),
            taker_buy_quote_volume=Decimal(raw.taker_buy_quote_volume),
            trades_count=raw.trades_count,
            open_raw=raw.open,
            high_raw=raw.high,
            low_raw=raw.low,
            close_raw=raw.close,
            volume_raw=raw.volume,
            is_closed=raw.is_closed,
        )


def compute_payload_checksum(bars: list[EnrichedBar] | list[dict[str, Any]]) -> str:
    """Compute deterministic SHA256 checksum of logical bar contents."""
    hasher = hashlib.sha256()
    for item in bars:
        if isinstance(item, EnrichedBar):
            row_tuple = (
                item.instrument_id,
                item.bar_type,
                item.ts_event_ns,
                item.open_raw,
                item.high_raw,
                item.low_raw,
                item.close_raw,
                item.volume_raw,
                item.trades_count,
            )
        else:
            row_tuple = (
                item["instrument_id"],
                item["bar_type"],
                item["ts_event_ns"],
                item.get("open_raw", str(item.get("open"))),
                item.get("high_raw", str(item.get("high"))),
                item.get("low_raw", str(item.get("low"))),
                item.get("close_raw", str(item.get("close"))),
                item.get("volume_raw", str(item.get("volume"))),
                item.get("trades_count", 0),
            )
        hasher.update(json.dumps(row_tuple, default=str, sort_keys=True).encode("utf-8"))
    return hasher.hexdigest()


def bars_to_arrow_table(bars: list[EnrichedBar]) -> pa.Table:
    """Convert list of EnrichedBar objects to an exact PyArrow Table matching ANALYTICS_PYARROW_SCHEMA."""
    if not bars:
        return ANALYTICS_PYARROW_SCHEMA.empty_table()

    data = {
        "instrument_id": [b.instrument_id for b in bars],
        "bar_type": [b.bar_type for b in bars],
        "schema_version": [b.schema_version for b in bars],
        "source": [b.source for b in bars],
        "payload_checksum": [b.payload_checksum for b in bars],
        "ingest_batch_id": [b.ingest_batch_id for b in bars],
        "ts_event_ns": [b.ts_event_ns for b in bars],
        "ts_init_ns": [b.ts_init_ns for b in bars],
        "received_at_ns": [b.received_at_ns for b in bars],
        "price_precision": [b.price_precision for b in bars],
        "size_precision": [b.size_precision for b in bars],
        "open": [b.open for b in bars],
        "high": [b.high for b in bars],
        "low": [b.low for b in bars],
        "close": [b.close for b in bars],
        "volume": [b.volume for b in bars],
        "quote_volume": [b.quote_volume for b in bars],
        "taker_buy_base_volume": [b.taker_buy_base_volume for b in bars],
        "taker_buy_quote_volume": [b.taker_buy_quote_volume for b in bars],
        "trades_count": [b.trades_count for b in bars],
        "open_raw": [b.open_raw for b in bars],
        "high_raw": [b.high_raw for b in bars],
        "low_raw": [b.low_raw for b in bars],
        "close_raw": [b.close_raw for b in bars],
        "volume_raw": [b.volume_raw for b in bars],
    }
    return pa.Table.from_pydict(data, schema=ANALYTICS_PYARROW_SCHEMA)


def bar_to_nautilus(bar: EnrichedBar, bar_type: BarType) -> Bar:
    """Convert EnrichedBar to a native Nautilus Bar domain object."""
    p_prec = bar.price_precision
    s_prec = bar.size_precision
    return Bar(
        bar_type=bar_type,
        open=Price(bar.open, p_prec),
        high=Price(bar.high, p_prec),
        low=Price(bar.low, p_prec),
        close=Price(bar.close, p_prec),
        volume=Quantity(bar.volume, s_prec),
        ts_event=bar.ts_event_ns,
        ts_init=bar.ts_init_ns,
    )
