# Cryptonite Data Contract Specification

## 1. Scope and Principles
This document formalizes the canonical data model, storage formats, timestamp contracts, and durability guarantees across the dual-tier storage architecture of Cryptonite.

---

## 2. Storage Tier Separation

### Canonical Execution Store: `data/nautilus_catalog/`
- **Owner:** NautilusTrader `ParquetDataCatalog`
- **Purpose:** Deterministic backtesting, paper trading, and live execution.
- **Data Types:**
  - `CurrencyPair` / `CryptoPerpetual` instruments.
  - `Bar` models with fixed-point `Price` and `Quantity`.
- **Directory Layout:** Managed natively by NautilusTrader.

### Enriched Analytical Lake: `data/analytics_lake/`
- **Owner:** Cryptonite Analytical Pipeline
- **Purpose:** Fast local scanning, pattern recognition, and UI terminal querying via DuckDB and Polars.
- **Partition Layout:**
  `data/analytics_lake/binance_klines_enriched/binance/spot/{timeframe}/{symbol}/date={YYYY-MM-DD}/part-{batch_id}.parquet`
- **Immutability:** Every part file is written to `.tmp.{uuid}.parquet`, flushed, fsynced, and renamed into place, followed by an fsync on the parent directory. No updates or in-place appends are permitted.

---

## 3. Timestamp Contracts
All timestamps in Cryptonite conform to the following nanosecond standards:

| Field | Source | Meaning |
| :--- | :--- | :--- |
| `ts_event_ns` | Binance Kline Close Time | Unix epoch nanoseconds representing the formal close of the candle. |
| `ts_init_ns` | Kline Close Time | Set identical to `ts_event_ns` for deterministic parity between historical backtest datasets and live WebSocket streams. |
| `received_at_ns` | Local System Clock | Nanoseconds when the frame was read from the socket or REST payload. Used exclusively for latency audit and gap diagnosis. |

---

## 4. Precision and Exactness Contracts
- **Nautilus Catalog:** Preserves exchange tick and step increments via `Price` and `Quantity` objects. Precision shifts (e.g., Binance modifying price filters) require a new `CurrencyPair` instrument revision with an updated `ts_event`.
- **Analytics Lake:** Financial values are strictly encoded as Arrow `decimal128(38, 18)` alongside `price_precision` (`uint8`) and `size_precision` (`uint8`).
- **Forensic Raw Values:** Raw wire strings (`open_raw`, `high_raw`, `low_raw`, `close_raw`, `volume_raw`) are retained in the analytics schema for exact reconciliation.

---

## 5. Crash-Recoverable Idempotent Commit Protocol
1. Calculate logical `payload_checksum` (SHA256 of canonical bars).
2. Insert row in `ingest_batches` with `status='pending'` and durable `canonical_payload_json`.
3. Write to Nautilus Catalog via `catalog.write_bars()`.
4. Write to Analytics Lake via atomic fsynced `.tmp` rename to `part-{batch_id}.parquet`.
5. In one PostgreSQL transaction:
   - Mark `ingest_batches.status = 'committed'`, recording file paths and checksums.
   - Advance `ingest_checkpoints` through the latest contiguous validated close timestamp.
6. **Process-Exclusive Single Writer:** The ingest service must hold a PostgreSQL advisory lock (`pg_try_advisory_lock`) to prevent split-brain concurrency across containers.
