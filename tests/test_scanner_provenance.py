"""Scan-run provenance stays small (workstation incident 2026-10-03: per-file paths + checksums made
scan_runs rows ~711 kB and the table 21 GB). One digest per date partition, unchanged files never
re-hashed, partitions older than the scan's lookback ignored."""

import builtins
import os

from cryptonite_scanner_service.worker import ScannerWorker


def make_lake(tmp_path):
    inst = tmp_path / "binance_klines_enriched" / "binance" / "spot" / "1m" / "BTCUSDT"
    for date, files in {"2026-09-01": 2, "2026-10-02": 3, "2026-10-03": 2}.items():
        part = inst / f"date={date}"
        part.mkdir(parents=True)
        for i in range(files):
            (part / f"part-{i}.parquet").write_bytes(f"{date}-{i}".encode())
    return inst


def test_one_digest_per_partition_in_the_lookback(tmp_path):
    inst = make_lake(tmp_path)
    worker = ScannerWorker(analytics_lake_path=str(tmp_path))
    digests = worker._partition_digests(inst, "2026-10-01")
    assert sorted(p.rsplit("=", 1)[1] for p in digests) == ["2026-10-02", "2026-10-03"]
    assert all(len(d) == 64 for d in digests.values())


def test_a_changed_file_changes_its_partition_digest_only(tmp_path):
    inst = make_lake(tmp_path)
    worker = ScannerWorker(analytics_lake_path=str(tmp_path))
    before = worker._partition_digests(inst, "2026-10-01")
    target = inst / "date=2026-10-03" / "part-0.parquet"
    target.write_bytes(b"rewritten bar")
    os.utime(target, ns=(1, 1))  # force a different mtime even within the same clock tick
    after = worker._partition_digests(inst, "2026-10-01")
    changed = [p for p in before if before[p] != after[p]]
    assert len(changed) == 1 and changed[0].endswith("date=2026-10-03")


def test_unchanged_files_are_not_read_again(tmp_path, monkeypatch):
    inst = make_lake(tmp_path)
    worker = ScannerWorker(analytics_lake_path=str(tmp_path))
    worker._partition_digests(inst, "2026-10-01")
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
    worker._partition_digests(inst, "2026-10-01")
    assert not [f for f in opened if f.endswith(".parquet")]
