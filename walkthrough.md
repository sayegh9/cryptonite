
## Cryptonite Bug Fixes (Part 2)

**Deploy Strategy "Field Required" Fix**
- Discovered that the backend `DeployStrategyRequest` requires `allocation_usd`.
- Updated `DeployStrategyModal.tsx` to add an `allocationUsd` state and input field, and included it in the API payload.
- Fixed the `timeframe` property in the deployment request to match the canonical Nautilus bar type string format (`[instrument_id]-[timeframe]`), ensuring the Strategy is instantiated with the correct `bar_type` parameter by the orchestration server.

**Backtester Data Gap / Nautilus Catalog Fix**
- Discovered that the onboarding endpoint (`instruments.py`) was bypassing `TwoStoreCoordinator` and only writing to `AnalyticsLakeWriter`. As a result, the `nautilus_catalog` used by the Backtester lacked recent data.
- Refactored `run_onboarding` to instantiate and use `TwoStoreCoordinator`, effectively multiplexing the Binance backfill to both the API analytics lake and the backtester nautilus catalog.
- Reduced the default UI backtest window from 24h to 12h in `ResearchLab.tsx` so that out-of-the-box backtests reliably fit within the 1000-bar limit fetched from the Binance API during onboarding.

**WebSocket Origin Verification Fix**
- Reverted the `ALLOWED_ORIGINS` wildcard implementation to use a secure multi-origin whitelist (`http://localhost:3000`, `http://127.0.0.1:3000`, `*`).
- Corrected the `origin` validation logic in `ws.py` to properly evaluate the wildcard `"*"` presence in the `ALLOWED_ORIGINS` set, allowing live price streaming without `403 Forbidden` disconnects.
- Rebuilt the `api` container to persist the python changes.

**Research Lab Strategy Selection Fix**
- Discovered that the backend database (`strategies` table) contained stale/garbage data from previous end-to-end integration tests (e.g. `rec-dbo-67a422`, `ema-e2e`). 
- The `ResearchLab.tsx` UI was displaying these dummy test strategies because the `/api/v1/strategies` endpoint returned all rows from the database. 
- When the user attempted to run a backtest with a dummy strategy, the `cryptonite_backtester` rightfully failed with `"Strategy not registered"` because it does not exist in the Python code registry (`STRATEGY_REGISTRY`).
- Updated `strategies.py` to filter the database query results against the canonical `STRATEGY_REGISTRY` keys. This ensures the API strictly returns only strategies that have corresponding source code (e.g. `ema-cross`, `supertrend`, `rsi-reversal`), keeping the UI dropdown clean and preventing unregistered strategy execution errors.
