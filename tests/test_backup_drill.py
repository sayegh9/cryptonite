"""Backup restore drill as a release gate (A3, F-H5): pg_restore failure cannot be masked,
restored row counts must equal the backup-time snapshot (not merely >0), and the outcome plus the
comparison are written durably to backup.log and a per-run drill evidence file. The scratch database
is always dropped, even on error; the source database is never dropped.

Runs backup.sh against throwaway databases on the compose postgres cluster and a throwaway DEST dir:
never the dev DB `cryptonite`, never the real backup dir, no live data touched."""

import os
import subprocess
import uuid
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup.sh"
PG_CONTAINER = os.getenv("CRYPTONITE_PG_CONTAINER", "infra-postgres-1")

TABLES = ["operator_users", "ai_mandates", "execution_commands", "paper_accounts",
          "portfolio_decisions", "alembic_version"]
SEED = {"operator_users": 3, "ai_mandates": 2, "execution_commands": 5, "paper_accounts": 1,
        "portfolio_decisions": 4, "alembic_version": 1}


def docker(*args, **kwargs):
    return subprocess.run(["docker", "exec", PG_CONTAINER, *args],
                          capture_output=True, text=True, **kwargs)


def psql(db, sql):
    return docker("psql", "-U", "cryptonite", "-d", db, "-tAc", sql)


@pytest.fixture
def bed(tmp_path):
    """Throwaway source/scratch databases plus a throwaway DEST dir and script env."""
    tag = uuid.uuid4().hex[:8]
    src, scratch = f"a3_src_{tag}", f"a3_scratch_{tag}"
    dest = tmp_path / "backups"
    assert docker("createdb", "-U", "cryptonite", src).returncode == 0
    try:
        for t in TABLES:
            cols = "version_num VARCHAR(32) PRIMARY KEY" if t == "alembic_version" else "id SERIAL PRIMARY KEY, note TEXT"
            assert psql(src, f"CREATE TABLE {t}({cols})").returncode == 0
        for t, n in SEED.items():
            if t == "alembic_version":
                assert psql(src, "INSERT INTO alembic_version VALUES ('039')").returncode == 0
            else:
                assert psql(src, f"INSERT INTO {t}(note) SELECT 's' FROM generate_series(1,{n})").returncode == 0
        env = {**os.environ,
               "CRYPTONITE_BACKUP_DIR": str(dest),
               "CRYPTONITE_PG_CONTAINER": PG_CONTAINER,
               "CRYPTONITE_BACKUP_DB": src,
               "CRYPTONITE_BACKUP_SCRATCH": scratch}
        yield {"env": env, "src": src, "scratch": scratch, "dest": dest}
    finally:
        docker("dropdb", "-U", "cryptonite", "--if-exists", src)
        docker("dropdb", "-U", "cryptonite", "--if-exists", scratch)


def run_backup(env, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)


def source_count(bed, table):
    return psql(bed["src"], f"SELECT count(*) FROM {table}").stdout.strip()


def scratch_exists(bed):
    out = docker("psql", "-U", "cryptonite", "-d", "postgres", "-tAc",
                 f"SELECT 1 FROM pg_database WHERE datname='{bed['scratch']}'")
    return out.stdout.strip() == "1"


def test_db_backup_writes_dump_manifest_and_log(bed):
    assert run_backup(bed["env"], "db").returncode == 0
    dumps = list((bed["dest"] / "db").glob("cryptonite-*.dump"))
    assert len(dumps) == 1 and dumps[0].stat().st_size > 0
    manifest = Path(str(dumps[0]) + ".counts")
    assert manifest.is_file()
    got = dict(line.split("=", 1) for line in manifest.read_text().splitlines() if line)
    assert set(got) == set(TABLES)
    for t in TABLES:
        assert got[t] == source_count(bed, t) == str(SEED[t])
    assert "db backup" in (bed["dest"] / "backup.log").read_text()


def test_drill_passes_with_matching_counts(bed):
    assert run_backup(bed["env"], "db").returncode == 0
    assert run_backup(bed["env"], "drill").returncode == 0
    evidence = list(bed["dest"].glob("drill-*.log"))
    assert len(evidence) == 1
    body = evidence[0].read_text()
    assert "pg_restore exit=0" in body
    for t in TABLES:
        assert f"MATCH {t} snapshot={SEED[t]} restored={SEED[t]}" in body
    assert "drill OK" in body and "drill FAILED" not in body
    assert "drill OK" in (bed["dest"] / "backup.log").read_text()
    assert not scratch_exists(bed)  # scratch always dropped, on success too


def test_drill_fails_on_corrupt_dump(bed):
    assert run_backup(bed["env"], "db").returncode == 0
    dump = next((bed["dest"] / "db").glob("cryptonite-*.dump"))
    with open(dump, "r+b") as f:
        f.truncate(100)  # not a valid pg_dump custom-format archive
    assert run_backup(bed["env"], "drill").returncode != 0
    body = next(bed["dest"].glob("drill-*.log")).read_text()
    assert "pg_restore exit=" in body and "pg_restore exit=0" not in body
    assert "drill FAILED" in body
    assert not scratch_exists(bed)  # scratch dropped even though the restore failed


def test_drill_fails_on_count_mismatch(bed):
    assert run_backup(bed["env"], "db").returncode == 0
    dump = next((bed["dest"] / "db").glob("cryptonite-*.dump"))
    manifest = Path(str(dump) + ".counts")
    lines = manifest.read_text().splitlines()
    manifest.write_text("\n".join(
        f"operator_users={SEED['operator_users'] + 10}" if l.startswith("operator_users=") else l
        for l in lines) + "\n")
    assert run_backup(bed["env"], "drill").returncode != 0
    body = next(bed["dest"].glob("drill-*.log")).read_text()
    assert "MISMATCH operator_users snapshot=13 restored=3" in body
    assert "drill FAILED" in body
    assert not scratch_exists(bed)


def test_drill_fails_without_manifest(bed):
    assert run_backup(bed["env"], "db").returncode == 0
    dump = next((bed["dest"] / "db").glob("cryptonite-*.dump"))
    Path(str(dump) + ".counts").unlink()  # dumps that predate the manifest cannot prove equality
    assert run_backup(bed["env"], "drill").returncode != 0
    body = next(bed["dest"].glob("drill-*.log")).read_text()
    assert "no counts manifest" in body and "drill FAILED" in body


def test_drill_refuses_scratch_equal_to_source(bed):
    env = {**bed["env"], "CRYPTONITE_BACKUP_SCRATCH": bed["src"]}
    assert run_backup(bed["env"], "db").returncode == 0
    assert run_backup(env, "drill").returncode != 0
    assert "refusing drill" in (bed["dest"] / "backup.log").read_text()
    for t in TABLES:  # the source database is untouched
        assert source_count(bed, t) == str(SEED[t])
