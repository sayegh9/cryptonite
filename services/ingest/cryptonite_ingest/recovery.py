"""Subscribe-before-backfill gap detection and recovery manager."""

import asyncio
import logging
import time

import asyncpg
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline, bar_to_nautilus
from cryptonite_storage.coordinator import TwoStoreCoordinator
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from nautilus_trader.model import Bar, BarType

from cryptonite_ingest.client import BinanceRestClient
from cryptonite_ingest.stream import BinanceStreamClient

logger = logging.getLogger("cryptonite.recovery")


class GapRecoveryManager:
    """Manages gap detection and subscribe-before-backfill recovery."""

    def __init__(
        self,
        rest_client: BinanceRestClient,
        coordinator: TwoStoreCoordinator,
        nautilus_manager: NautilusCatalogManager,
        pg_pool: asyncpg.Pool | None = None,
    ) -> None:
        self.rest = rest_client
        self.coordinator = coordinator
        self.nautilus = nautilus_manager
        self.pool = pg_pool

    async def get_durable_checkpoint(
        self, symbol: str, timeframe: str, venue: str = "BINANCE", market_type: str = "SPOT"
    ) -> int | None:
        """Get the latest durable closed bar timestamp (ns) from Postgres."""
        if not self.pool:
            return None
        val = await self.pool.fetchval(
            """
            SELECT last_timestamp_ns FROM ingest_checkpoints
            WHERE venue = $1 AND market_type = $2 AND symbol = $3 AND timeframe = $4;
            """,
            venue,
            market_type,
            symbol,
            timeframe,
        )
        return int(val) if val is not None else None

    async def heal_empty_files(
        self,
        symbol: str,
        timeframe: str,
        bar_type: BarType,
        price_precision: int,
        size_precision: int,
    ) -> int:
        """Quarantine empty catalog files of this stream and refill their bars from REST.

        An empty file left by a killed writer claims its interval, so neither gap recovery (which
        starts at the checkpoint) nor a backfill sees the hole. The refill goes to the catalog only
        (the analytics lake was not the store that broke) and bypasses the checkpoint guard, which
        would skip bars older than the live checkpoint. Run before the live stream writes.
        """
        intervals = self.nautilus.quarantine_empty_bar_files(str(bar_type))
        if not intervals:
            return 0
        logger.warning(
            "Quarantined %d empty catalog file(s) for %s: %s", len(intervals), bar_type, intervals
        )
        healed = 0
        step_ms = (
            int(timeframe[:-1]) * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[timeframe[-1]]
        )
        for start_ns, end_ns in intervals:
            # a bar's ts_event is its close; Binance selects klines by open time, one step earlier
            raw = await self.rest.fetch_klines(
                symbol=symbol,
                interval=timeframe,
                start_time_ms=start_ns // 1_000_000 - step_ms,
                end_time_ms=end_ns // 1_000_000,
                limit=1000,
            )
            raw = [k for k in raw if k.is_closed]
            arrival_ns = time.time_ns()
            bars = sorted(
                (
                    EnrichedBar.from_binance_raw(
                        raw=k,
                        instrument_id=f"{symbol}.BINANCE",
                        bar_type=str(bar_type),
                        price_precision=price_precision,
                        size_precision=size_precision,
                        received_at_ns=arrival_ns,
                    )
                    for k in raw
                ),
                key=lambda b: b.ts_event_ns,
            )
            bars = [b for b in bars if start_ns <= b.ts_event_ns <= end_ns]
            if bars:
                self.nautilus.write_bars([bar_to_nautilus(b, bar_type) for b in bars])
                healed += len(bars)
        logger.warning("Refilled %d bar(s) for %s from REST", healed, bar_type)
        return healed

    async def recover_gaps_for_stream(
        self,
        symbol: str,
        timeframe: str,
        bar_type: BarType,
        price_precision: int,
        size_precision: int,
        stream_client: BinanceStreamClient,
        lookback_seconds: int = 86400,
    ) -> int:
        """Execute subscribe-before-backfill sequence to guarantee zero gaps."""
        now_ns = time.time_ns()
        checkpoint_ns = await self.get_durable_checkpoint(symbol, timeframe)
        start_ns = checkpoint_ns if checkpoint_ns else (now_ns - lookback_seconds * 1_000_000_000)

        bar_id = str(bar_type)
        missing_intervals = self.nautilus.get_missing_intervals_for_request(
            start=start_ns,
            end=now_ns,
            data_cls=Bar,
            identifier=bar_id,
        )

        if not missing_intervals:
            logger.info("No gaps detected in Nautilus catalog for %s", bar_id)
            return 0

        logger.info(
            "Found %d missing interval(s) for %s: %s",
            len(missing_intervals),
            bar_id,
            missing_intervals,
        )
        total_recovered = 0

        for gap_start_ns, gap_end_ns in missing_intervals:
            gap_start_ms = gap_start_ns // 1_000_000
            gap_end_ms = gap_end_ns // 1_000_000

            # REST backfill the missing range (paginated up to gap_end_ms)
            timeframe_multiplier = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}.get(
                timeframe[-1], 60_000
            )
            step_ms = int(timeframe[:-1]) * timeframe_multiplier

            raw_klines: list[RawBinanceKline] = []
            cur_start_ms = gap_start_ms
            max_pages = 20  # bounded: at most 20,000 bars per missing interval
            page = 0

            while cur_start_ms <= gap_end_ms and page < max_pages:
                page += 1
                logger.info(
                    "Fetching REST klines for %s (%s) from %d to %d (page %d)",
                    symbol,
                    timeframe,
                    cur_start_ms,
                    gap_end_ms,
                    page,
                )
                batch = await self.rest.fetch_klines(
                    symbol=symbol,
                    interval=timeframe,
                    start_time_ms=cur_start_ms,
                    end_time_ms=gap_end_ms,
                    limit=1000,
                )
                if not batch:
                    break
                raw_klines.extend(batch)
                if len(batch) < 1000:
                    break
                next_start = batch[-1].open_time_ms + step_ms
                if next_start <= cur_start_ms:
                    break
                cur_start_ms = next_start

            # Drain any buffered closed bars from the stream that arrived during the REST fetch
            # IMPORTANT: Re-queue bars belonging to other symbols or timeframes to avoid losing them
            buffered_bars = []
            unmatched = []
            while not stream_client.closed_bar_buffer.empty():
                try:
                    item = stream_client.closed_bar_buffer.get_nowait()
                    s, tf, k_buf, arr_ns = item
                    if s == symbol and tf == timeframe:
                        buffered_bars.append(
                            EnrichedBar.from_binance_raw(
                                raw=k_buf,
                                instrument_id=f"{symbol}.BINANCE",
                                bar_type=bar_id,
                                price_precision=price_precision,
                                size_precision=size_precision,
                                received_at_ns=arr_ns,
                            )
                        )
                    else:
                        unmatched.append(item)
                except asyncio.QueueEmpty:
                    break

            for item in unmatched:
                stream_client.closed_bar_buffer.put_nowait(item)

            # A gap that reaches "now" returns the still-open kline; committing it would move the
            # checkpoint past the live bar, and the stream's real closed bar would then be skipped
            raw_klines = [k for k in raw_klines if k.is_closed]

            arrival_ns = time.time_ns()
            enriched_bars = [
                EnrichedBar.from_binance_raw(
                    raw=k,
                    instrument_id=f"{symbol}.BINANCE",
                    bar_type=bar_id,
                    price_precision=price_precision,
                    size_precision=size_precision,
                    received_at_ns=arrival_ns,
                )
                for k in raw_klines
            ]

            if not enriched_bars and not buffered_bars:
                continue

            # Deduplicate REST bars and buffered live bars by ts_event_ns
            all_bars_map: dict[int, EnrichedBar] = {}
            for b in enriched_bars:
                all_bars_map[b.ts_event_ns] = b
            for b in buffered_bars:
                if b.ts_event_ns in all_bars_map:
                    # Check for conflicting duplicate
                    existing = all_bars_map[b.ts_event_ns]
                    if existing.close != b.close or existing.volume != b.volume:
                        await self.coordinator.quarantine_conflicting_bar(
                            bar=b,
                            existing_data=existing.model_dump(mode="json"),
                            reason="Conflicting duplicate between REST backfill and WebSocket stream",
                        )
                        continue
                all_bars_map[b.ts_event_ns] = b

            deduped_bars = sorted(all_bars_map.values(), key=lambda b: b.ts_event_ns)
            if deduped_bars:
                await self.coordinator.commit_batch(
                    bars=deduped_bars,
                    bar_type=bar_type,
                    venue="BINANCE",
                    market_type="SPOT",
                    symbol=symbol,
                    timeframe=timeframe,
                )
                total_recovered += len(deduped_bars)

        logger.info("Successfully recovered %d bars for %s", total_recovered, symbol)
        return total_recovered
