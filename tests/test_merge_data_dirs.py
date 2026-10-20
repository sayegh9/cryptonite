"""``scripts/merge_data_dirs.py`` (host-stability runbook §7) brings into the live data folder only what the old one
holds alone: never overwriting, never adding a catalog bar file whose interval overlaps one already there, skipping
dot-files, and changing nothing in a dry run."""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "merge_data_dirs.py"
spec = importlib.util.spec_from_file_location("merge_data_dirs", SCRIPT)
merge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = merge
spec.loader.exec_module(merge)

BARS = "nautilus_catalog/data/bars/BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL"


def iv(start: str, end: str) -> str:
    return f"2026-{start}-00-000000000Z_2026-{end}-00-000000000Z.parquet"


def put(root: Path, rel: str, data: bytes = b"x") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def setup(tmp_path: Path) -> tuple[Path, Path]:
    src, dst = tmp_path / "repo-data", tmp_path / "live-data"
    # history only the old folder has, up to 10-19 11:26; the live catalog starts 11:27
    put(src, f"{BARS}/{iv('03-18T20-44', '03-19T13-23')}")
    put(src, f"{BARS}/{iv('10-19T11-26', '10-19T11-26')}")
    put(dst, f"{BARS}/{iv('10-19T11-27', '10-19T11-28')}")
    put(src, "nautilus_catalog/data/instruments/BTCUSDT.BINANCE/i1.parquet")
    put(src, "analytics_lake/binance_klines_enriched/binance/spot/1m/BTCUSDT/date=2026-10-19/part-late.parquet")
    shared = "analytics_lake/binance_klines_enriched/binance/spot/1m/BTCUSDT/date=2026-10-18/part-a.parquet"
    put(src, shared, b"same")
    put(dst, shared, b"same")
    put(src, "research_reports/r1.json/report.json")
    put(src, "analytics_lake/.lake-maintenance.lock", b"")
    put(src, "analytics_lake/binance_klines_enriched/binance/spot/1m/BTCUSDT/date=2026-10-19/.tmp.ab.parquet")
    return src, dst


def test_plan_brings_only_what_the_live_folder_lacks(tmp_path):
    src, dst = setup(tmp_path)
    p = merge.plan(src, dst)
    assert sorted(map(str, p.link)) == sorted([
        f"{BARS}/{iv('03-18T20-44', '03-19T13-23')}", f"{BARS}/{iv('10-19T11-26', '10-19T11-26')}",
        "nautilus_catalog/data/instruments/BTCUSDT.BINANCE/i1.parquet",
        "analytics_lake/binance_klines_enriched/binance/spot/1m/BTCUSDT/date=2026-10-19/part-late.parquet",
        "research_reports/r1.json/report.json",
    ])
    assert p.present == 1 and p.conflicts == []


def test_dry_run_changes_nothing(tmp_path, capsys):
    src, dst = setup(tmp_path)
    before = (snapshot(src), snapshot(dst))
    assert merge.main(["--src", str(src), "--dst", str(dst)]) == 0
    assert (snapshot(src), snapshot(dst)) == before
    assert "dry run" in capsys.readouterr().out


def test_apply_links_the_same_files_and_is_idempotent(tmp_path):
    src, dst = setup(tmp_path)
    assert merge.main(["--src", str(src), "--dst", str(dst), "--apply"]) == 0
    moved = dst / BARS / iv("03-18T20-44", "03-19T13-23")
    assert moved.samefile(src / BARS / iv("03-18T20-44", "03-19T13-23"))  # a hard link: no extra space
    lock = dst / "analytics_lake" / ".lake-maintenance.lock"  # the run's own lock, not the old folder's file
    assert not lock.exists() or not lock.samefile(src / "analytics_lake" / ".lake-maintenance.lock")
    assert not list(dst.rglob(".tmp.*"))
    after = snapshot(dst)
    assert merge.main(["--src", str(src), "--dst", str(dst), "--apply"]) == 0
    assert snapshot(dst) == after
    assert merge.plan(src, dst).link == []


def test_overlapping_bars_and_different_files_are_refused(tmp_path):
    src, dst = setup(tmp_path)
    put(src, f"{BARS}/{iv('10-19T11-28', '10-19T11-30')}")  # overlaps the live 11:27-11:28 file
    shared = "analytics_lake/binance_klines_enriched/binance/spot/1m/BTCUSDT/date=2026-10-18/part-a.parquet"
    put(src, shared, b"different size")
    p = merge.plan(src, dst)
    assert len(p.conflicts) == 2
    assert merge.main(["--src", str(src), "--dst", str(dst), "--apply"]) == 1
    assert not (dst / BARS / iv("10-19T11-28", "10-19T11-30")).exists()
    assert (dst / shared).read_bytes() == b"same"  # never overwritten


def test_the_same_folder_is_refused(tmp_path):
    src, _ = setup(tmp_path)
    assert merge.main(["--src", str(src), "--dst", str(src)]) == 2
