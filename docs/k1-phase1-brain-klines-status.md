# K1 Phase 1: Any-Symbol Brain Klines Tool Status

## Overview
Implementation of the isolated, read-only `get_klines(symbol, interval, limit)` tool registered strictly for the **Brain agent**, enabling any Binance trading pair symbol inquiry without mutating system state or affecting live feeds/databases.

## Summary of Changes
- **`packages/agents/cryptonite_agents/tools.py`**:
  - Implemented `_get_valid_symbols()` with 24-hour TTL caching over Binance `exchangeInfo` to validate active Binance trading pairs.
  - Implemented `get_klines(symbol, interval='1h', limit=200)`:
    - Symbol regex validation (`^[A-Z0-9]{3,20}$`).
    - Interval validation (`1m`, `5m`, `15m`, `1h`, `4h`, `1d`).
    - Limit validation (`1 <= limit <= 500`).
    - Strictly closed candle filtering (`close_time <= now_ms`).
    - 60-second in-memory result caching for identical `(symbol, interval, limit)` tuples.
    - Used-weight rate limit protection (`X-MBX-USED-WEIGHT-1M > 3000` fails closed).
    - Rate limit (HTTP 429/418) handling with `Retry-After` header extraction.
    - Standard metadata return schema including `in_feed` status (`"fed"` for `BTCUSDT`/`ETHUSDT`, `"unknown"` otherwise).
  - Registered `GET_KLINES` with `RiskClass.READ`.
- **`packages/agents/cryptonite_agents/brain/toolset.py`**:
  - Added `GET_KLINES` to `BRAIN_TOOLS`.
- **`packages/agents/cryptonite_agents/brain/agent.py`**:
  - Updated `BRAIN_INSTRUCTIONS` to direct the Brain agent to use `get_klines` for spot price history inquiries across any Binance symbol, while reserving `show_price_chart` for fed symbols (`BTCUSDT`, `ETHUSDT`).
- **`tests/test_get_klines.py`**:
  - Added comprehensive test suite covering regex validation, invalid intervals/limits, exchangeInfo symbol validation, closed candle filtering, result caching, weight rate-limiting, and 429/418 handling.

## Verification & Testing
- Unit tests run: `uv run pytest tests/test_get_klines.py tests/test_brain.py tests/test_brain_api.py`
- Result: 38 passed.
- Linting: `uv run ruff check packages/agents/ tests/test_get_klines.py`
- Git Diff Check: `git diff --check`
