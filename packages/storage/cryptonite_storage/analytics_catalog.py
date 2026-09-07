"""Immutable, date-partitioned Parquet writer for the Enriched Analytics Lake."""

import hashlib
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
from cryptonite_schemas.bar import EnrichedBar, bars_to_arrow_table


class AnalyticsLakeWriter:
    """Writes immutable date-partitioned batch files to the Analytics Lake with fsync durability."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def write_batch(
        self,
        bars: list[EnrichedBar],
        venue: str = "binance",
        market_type: str = "spot",
        timeframe: str = "1m",
        symbol: str = "BTCUSDT",
        batch_id: str | None = None,
    ) -> tuple[str, str]:
        """Write a batch of bars to an immutable partition file.
        
        Path pattern:
            {base_dir}/binance_klines_enriched/{venue}/{market_type}/{timeframe}/{symbol}/date={YYYY-MM-DD}/part-{batch_id}.parquet
            
        Returns:
            (target_file_path, file_sha256_checksum)
        """
        if not bars:
            return "", ""

        active_batch_id = batch_id or str(uuid.uuid4())
        
        # Determine partition date based on the first bar's close timestamp (UTC)
        first_bar_ts = bars[0].ts_event_ns
        dt = datetime.fromtimestamp(first_bar_ts / 1_000_000_000, tz=UTC)
        date_str = dt.strftime("%Y-%m-%d")

        partition_dir = (
            self.base_dir
            / "binance_klines_enriched"
            / venue.lower()
            / market_type.lower()
            / timeframe
            / symbol.upper()
            / f"date={date_str}"
        )
        partition_dir.mkdir(parents=True, exist_ok=True)

        target_file = partition_dir / f"part-{active_batch_id}.parquet"
        tmp_file = partition_dir / f".tmp.{uuid.uuid4().hex}.parquet"

        table = bars_to_arrow_table(bars)

        # 1. Write to temporary file
        with open(tmp_file, "wb") as fh:
            pq.write_table(table, fh, compression="snappy")
            # 2. Flush and fsync the file handle for durability
            fh.flush()
            os.fsync(fh.fileno())

        # 3. Atomic rename to target file
        os.replace(tmp_file, target_file)

        # 4. Fsync the parent directory for power-loss durability
        dir_fd = os.open(str(partition_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        # 5. Compute file checksum
        hasher = hashlib.sha256()
        with open(target_file, "rb") as fh:
            while chunk := fh.read(65536):
                hasher.update(chunk)
        file_checksum = hasher.hexdigest()

        return str(target_file), file_checksum
