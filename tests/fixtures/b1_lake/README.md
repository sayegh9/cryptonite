# B1 / F-C1 lake fixture

A miniature Analytics Lake (same layout and parquet schema as `AnalyticsLakeWriter`) holding one real UTC
day, 2026-10-04, for BTCUSDT and ETHUSDT:

- `1m/`: 1500 rows, `ts_event_ns` (the bar close) in (2026-10-03 23:00, 2026-10-05 00:00].
- `1h/`: the 24 native Binance 1h bars ingest recorded for that day (also stamped at the close).

`payload_checksum` and `ingest_batch_id` are blanked; everything else is copied unchanged.

Used by `tests/test_bar_alignment.py` (resampled lake 1m vs native 1h: 0 OHLC mismatches) and
`tests/test_indicators_golden.py::test_scanner_indicators_through_lake_resampler_match_native_1h`.
Both fail, never skip, when these files are missing.

Run:

    uv run pytest tests/test_bar_alignment.py tests/test_indicators_golden.py -q

Regenerate or add a day (read-only against the local lake; edit `DAY` / `SYMBOLS` in the script, then the
expected row counts in the tests). Pick a day with no open klines in the native 1h store: on 2026-10-03 the
11:00 1h bar was recorded at 11:19, before it closed, and is no oracle.

    uv run python tests/fixtures/b1_lake/export_b1_fixture.py --lake data/analytics_lake
