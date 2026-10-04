"""Atomic Parquet and JSON artifact persistence for backtest runs."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq


def _atomic_write_bytes(target_path: Path, data: bytes) -> str:
    """Atomically write bytes to disk with fsync and return the SHA-256 checksum."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.parent / f".tmp.{uuid4().hex}.bin"

    hasher = hashlib.sha256(data)
    checksum = hasher.hexdigest()

    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, target_path)

    # Sync parent directory to persist directory entry metadata
    dir_fd = os.open(str(target_path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

    return checksum


def _atomic_write_arrow_table(target_path: Path, table: pa.Table) -> tuple[str, int]:
    """Atomically write an Arrow table as a Parquet file with fsync.
    
    Returns (sha256_checksum, byte_size).
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.parent / f".tmp.{uuid4().hex}.parquet"

    with open(tmp_path, "wb") as f:
        pq.write_table(table, f, compression="zstd")
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, target_path)

    dir_fd = os.open(str(target_path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

    byte_size = target_path.stat().st_size
    hasher = hashlib.sha256()
    with open(target_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)

    return hasher.hexdigest(), byte_size


class BacktestArtifactStore:
    """Manages immutable artifact directories for backtest runs."""

    def __init__(self, base_dir: str | Path = "data/backtest_artifacts") -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_run_dir(self, run_id: str) -> Path:
        """Get absolute path to a run's artifact directory."""
        return self.base_dir / str(run_id)

    def get_staging_dir(self, run_id: str, attempt_id: str) -> Path:
        """Get absolute path to an attempt-specific staging artifact directory."""
        return self.base_dir / f".tmp_{run_id}_{attempt_id}"

    def persist_staged_artifacts(
        self,
        run_id: str,
        attempt_id: str,
        equity_table: pa.Table,
        trades_table: pa.Table,
        orders_table: pa.Table,
        manifest_meta: dict[str, Any],
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically persist artifacts into an attempt-specific staging directory.

        ``metrics`` (``metrics_version``, benchmark, fees, turnover) is stored under the ``metrics`` key of
        the artifact manifest, which is what the database keeps, so a run recovered from disk keeps it.
        """
        staging_dir = self.get_staging_dir(run_id, attempt_id)
        staging_dir.mkdir(parents=True, exist_ok=True)

        eq_path = staging_dir / "equity_curve.parquet"
        eq_hash, eq_size = _atomic_write_arrow_table(eq_path, equity_table)

        tr_path = staging_dir / "trades.parquet"
        tr_hash, tr_size = _atomic_write_arrow_table(tr_path, trades_table)

        ord_path = staging_dir / "orders.parquet"
        ord_hash, ord_size = _atomic_write_arrow_table(ord_path, orders_table)

        artifacts_meta = {
            "equity_curve.parquet": {"sha256": eq_hash, "size_bytes": eq_size},
            "trades.parquet": {"sha256": tr_hash, "size_bytes": tr_size},
            "orders.parquet": {"sha256": ord_hash, "size_bytes": ord_size},
        }
        if metrics is not None:
            artifacts_meta["metrics"] = metrics

        full_manifest = {
            **manifest_meta,
            "run_id": str(run_id),
            "attempt_id": str(attempt_id),
            "artifacts": artifacts_meta,
        }

        manifest_path = staging_dir / "manifest.json"
        manifest_bytes = json.dumps(full_manifest, indent=2, sort_keys=True).encode("utf-8")
        manifest_hash = _atomic_write_bytes(manifest_path, manifest_bytes)
        artifacts_meta["manifest.json"] = {
            "sha256": manifest_hash,
            "size_bytes": len(manifest_bytes),
        }

        return artifacts_meta

    def promote_staged_artifacts(self, run_id: str, attempt_id: str) -> Path:
        """Atomically promote staged attempt artifacts directory to the final run directory and fsync parent."""
        staging_dir = self.get_staging_dir(run_id, attempt_id)
        run_dir = self.get_run_dir(run_id)

        if not staging_dir.exists():
            raise FileNotFoundError(f"Staging directory {staging_dir} does not exist for promotion")

        # Atomic directory replacement
        os.replace(staging_dir, run_dir)

        # Fsync base directory to guarantee directory entry persistence
        dir_fd = os.open(str(self.base_dir), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        return run_dir

    def persist_artifacts(
        self,
        run_id: str,
        equity_table: pa.Table,
        trades_table: pa.Table,
        orders_table: pa.Table,
        manifest_meta: dict[str, Any],
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically persist all three Parquet artifact tables and manifest.json directly into run_dir.
        
        Returns the artifact manifest dict mapping relative paths to checksums and byte sizes.
        """
        run_dir = self.get_run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        eq_path = run_dir / "equity_curve.parquet"
        eq_hash, eq_size = _atomic_write_arrow_table(eq_path, equity_table)

        tr_path = run_dir / "trades.parquet"
        tr_hash, tr_size = _atomic_write_arrow_table(tr_path, trades_table)

        ord_path = run_dir / "orders.parquet"
        ord_hash, ord_size = _atomic_write_arrow_table(ord_path, orders_table)

        artifacts_meta = {
            "equity_curve.parquet": {"sha256": eq_hash, "size_bytes": eq_size},
            "trades.parquet": {"sha256": tr_hash, "size_bytes": tr_size},
            "orders.parquet": {"sha256": ord_hash, "size_bytes": ord_size},
        }
        if metrics is not None:
            artifacts_meta["metrics"] = metrics

        full_manifest = {
            **manifest_meta,
            "run_id": str(run_id),
            "artifacts": artifacts_meta,
        }

        manifest_path = run_dir / "manifest.json"
        manifest_bytes = json.dumps(full_manifest, indent=2, sort_keys=True).encode("utf-8")
        manifest_hash = _atomic_write_bytes(manifest_path, manifest_bytes)
        artifacts_meta["manifest.json"] = {
            "sha256": manifest_hash,
            "size_bytes": len(manifest_bytes),
        }

        return artifacts_meta

    def load_manifest(self, run_id: str) -> dict[str, Any] | None:
        """Load manifest.json for a run if present."""
        run_dir = self.get_run_dir(run_id)
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            with open(manifest_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def verify_artifacts(self, run_id: str) -> bool:
        """Verify that all required artifact files exist on disk."""
        run_dir = self.get_run_dir(run_id)
        required_files = [
            "equity_curve.parquet",
            "trades.parquet",
            "orders.parquet",
            "manifest.json",
        ]
        return all((run_dir / f).is_file() for f in required_files)
