"""``backup.sh data`` archives the LIVE data folder: the deploy worktree's ``data/`` that the stack mounts, not the
repo's, and survives the stack writing while it reads (host-stability runbook §7). No database is touched."""

import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup.sh"


@pytest.fixture
def bed(tmp_path):
    root = tmp_path / "cryptonite"
    repo, deploy = root / "repo", root / "deploy-main"
    for base, marker in ((repo, "old"), (deploy, "live")):
        lake = base / "data" / "analytics_lake" / "x"
        lake.mkdir(parents=True)
        (lake / f"{marker}.parquet").write_text(marker)
    env = {**os.environ, "CRYPTONITE_REPO": str(repo), "CRYPTONITE_BACKUP_DIR": str(tmp_path / "backups")}
    for k in ("CRYPTONITE_DEPLOY", "CRYPTONITE_DATA_DIR"):
        env.pop(k, None)
    return {"tmp": tmp_path, "repo": repo, "deploy": deploy, "env": env, "dest": tmp_path / "backups"}


def run(env, path_prefix=None):
    if path_prefix:
        env = {**env, "PATH": f"{path_prefix}:{env['PATH']}"}
    return subprocess.run(["bash", str(SCRIPT), "data"], capture_output=True, text=True, env=env, timeout=120)


def members(dest: Path) -> list[str]:
    archive = next((dest / "data").glob("data-*.tar.zst"))
    out = subprocess.run(f"zstd -dc '{archive}' | tar -tf -", shell=True, capture_output=True, text=True, check=True)
    return out.stdout.split()


def test_the_deploy_worktree_data_is_archived(bed):
    r = run(bed["env"])
    assert r.returncode == 0, r.stderr
    names = members(bed["dest"])
    assert "data/analytics_lake/x/live.parquet" in names and "data/analytics_lake/x/old.parquet" not in names
    assert f"of {bed['deploy'] / 'data'}" in (bed["dest"] / "backup.log").read_text()


def test_repo_data_without_a_deploy_worktree(bed):
    import shutil

    shutil.rmtree(bed["deploy"])
    assert run(bed["env"]).returncode == 0
    assert "data/analytics_lake/x/old.parquet" in members(bed["dest"])


def test_an_explicit_data_dir_wins_and_is_archived_as_data(bed):
    other = bed["tmp"] / "elsewhere" / "live-data"
    (other / "analytics_lake").mkdir(parents=True)
    (other / "analytics_lake" / "z.parquet").write_text("z")
    assert run({**bed["env"], "CRYPTONITE_DATA_DIR": str(other)}).returncode == 0
    assert "data/analytics_lake/z.parquet" in members(bed["dest"])


def test_files_changing_during_the_read_do_not_fail_the_backup(bed):
    """GNU tar exits 1 when a file changes or vanishes while it reads (ingest writing): still a good archive."""
    shim = bed["tmp"] / "shim"
    shim.mkdir()
    real_tar = subprocess.run(["bash", "-c", "command -v tar"], capture_output=True, text=True).stdout.strip()
    (shim / "tar").write_text(f'#!/usr/bin/env bash\n"{real_tar}" "$@"\nexit 1\n')
    (shim / "tar").chmod(0o755)
    r = run(bed["env"], path_prefix=shim)
    assert r.returncode == 0, r.stderr
    assert "data/analytics_lake/x/live.parquet" in members(bed["dest"])


def test_a_real_tar_error_still_fails(bed):
    shim = bed["tmp"] / "shim2"
    shim.mkdir()
    (shim / "tar").write_text("#!/usr/bin/env bash\nexit 2\n")
    (shim / "tar").chmod(0o755)
    r = run(bed["env"], path_prefix=shim)
    assert r.returncode != 0
    assert list((bed["dest"] / "data").glob("data-*.tar.zst")) == []
    assert "ERROR: data backup attempt failed" in (bed["dest"] / "backup.log").read_text()


def test_a_stale_lake_is_flagged(bed):
    old = time.time() - 3 * 86400
    f = bed["deploy"] / "data" / "analytics_lake" / "x" / "live.parquet"
    os.utime(f, (old, old))
    assert run(bed["env"]).returncode == 0
    assert "WARNING: no lake file" in (bed["dest"] / "backup.log").read_text()
