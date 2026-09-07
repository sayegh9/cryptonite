"""Wrapper around NautilusTrader ParquetDataCatalog with exact API signatures."""

import hashlib
import os
from pathlib import Path
from typing import Any

from nautilus_trader.model import Bar, CurrencyPair
from nautilus_trader.persistence import ParquetDataCatalog


class NautilusCatalogManager:
    """Manages reading and writing to the canonical Nautilus ParquetDataCatalog."""

    def __init__(self, catalog_path: str | Path) -> None:
        self.catalog_path = Path(catalog_path).resolve()
        self.catalog_path.mkdir(parents=True, exist_ok=True)
        self._catalog = ParquetDataCatalog(str(self.catalog_path))

    @property
    def raw_catalog(self) -> ParquetDataCatalog:
        return self._catalog

    def write_instruments(self, instruments: list[Any]) -> list[str]:
        """Write instruments to catalog and return paths of created/updated files."""
        pre_files = self._snapshot_files()
        self._catalog.write_instruments(instruments)
        post_files = self._snapshot_files()
        new_or_modified = list(post_files - pre_files) or list(post_files)
        return sorted(new_or_modified)

    def write_bars(self, bars: list[Bar]) -> tuple[list[str], str]:
        """Write bars to catalog. Returns (file_paths, sha256_checksum_of_written_files)."""
        if not bars:
            return [], ""
        pre_files = self._snapshot_files()
        self._catalog.write_bars(bars, skip_disjoint_check=True)
        post_files = self._snapshot_files()
        affected = sorted(list(post_files - pre_files) or list(post_files))

        hasher = hashlib.sha256()
        for f in affected:
            try:
                with open(f, "rb") as fh:
                    while chunk := fh.read(65536):
                        hasher.update(chunk)
            except OSError:
                pass
        return affected, hasher.hexdigest()

    def query_bars(
        self, identifiers: list[str], start: int | None = None, end: int | None = None
    ) -> list[Bar]:
        """Query bars from the catalog by full BarType string."""
        kwargs: dict[str, Any] = {"identifiers": identifiers}
        if start is not None:
            kwargs["start"] = start
        if end is not None:
            kwargs["end"] = end
        return self._catalog.query_bars(**kwargs)

    def instruments(self) -> list[Any]:
        """List all instruments registered in the catalog."""
        return self._catalog.instruments()

    def get_intervals(
        self,
        data_cls: type | str = Bar,
        identifier: str | None = None,
        instrument_id: str | None = None,
    ) -> list[tuple[int, int]]:
        """Get time intervals covered in catalog.
        
        Conforms to official API: accepts data_cls=Bar and identifier=bar_identifier.
        Internally translates to Nautilus pyo3 backend requirements.
        """
        data_cls_str = self._resolve_data_cls_str(data_cls)
        target_id = identifier or instrument_id
        return self._catalog.get_intervals(data_cls_str, target_id)

    def get_missing_intervals_for_request(
        self,
        start: int,
        end: int,
        data_cls: type | str = Bar,
        identifier: str | None = None,
        instrument_id: str | None = None,
    ) -> list[tuple[int, int]]:
        """Get missing intervals for a requested range."""
        data_cls_str = self._resolve_data_cls_str(data_cls)
        target_id = identifier or instrument_id
        return self._catalog.get_missing_intervals_for_request(
            start, end, data_cls_str, target_id
        )

    def _resolve_data_cls_str(self, data_cls: type | str) -> str:
        if isinstance(data_cls, str):
            return data_cls
        if data_cls is Bar:
            return "bars"
        return getattr(data_cls, "__name__", str(data_cls)).lower()

    def _snapshot_files(self) -> set[str]:
        files: set[str] = set()
        for root, _, filenames in os.walk(self.catalog_path):
            for fn in filenames:
                if fn.endswith(".parquet"):
                    files.add(os.path.join(root, fn))
        return files
