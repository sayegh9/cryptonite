"""Backup restore drill as a release gate (A3, F-H5): pg_restore failure cannot be masked,
restored row counts must equal the backup-time snapshot (not merely >0), and the outcome plus the
comparison are written durably to backup.log and a per-run drill evidence file. The scratch database
is always dropped, even on error; the source database is never dropped.

diffs vs earlier A3 heads:
  - db mode exports ONE PG snapshot and feeds both pg_dump --snapshot and every per-table count, so
    the manifest always matches the dump under concurrent writes; counts are fail-checked and must be
    numeric before the dump/manifest are published.
  - the manifest carries alembic_version_hash (sha256 of the ordered alembic_version rows) and the
    drill validates the manifest strictly (exact table set, numeric counts, no extras/duplicates/
    blanks, allowlisted identifiers, hash present) before comparing the restored database to it.

Runs backup.sh against throwaway databases on the compose postgres cluster and a throwaway DEST dir:
never the dev DB `cryptonite`, never the real backup dir, no live data touched."""

import hashlib
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup.sh"
PG_CONTAINER = os.getenv("CRYPTONITE_PG_CONTAINER", "infra-postgres-1")

# Portable PG access, mirroring backup.sh: run the client binaries directly when pointed at a
# reachable cluster (CRYPTONITE_PG_DIRECT=1, or PGHOST/PGPORT/PGDATABASE set — as in CI), else go
# through the compose container. Either way only throwaway databases are touched, never `cryptonite`.
PG_DIRECT = bool(os.getenv("CRYPTONITE_PG_DIRECT") or os.getenv("PGHOST") or os.getenv("PGPORT") or os.getenv("PGDATABASE"))

TABLES = ["operator_users", "ai_mandates", "execution_commands", "paper_accounts",
          "portfolio_decisions", "alembic_version"]
SEED = {"operator_users": 3, "ai_mandates": 2, "execution_commands": 5, "paper_accounts": 1,
        "portfolio_decisions": 4, "alembic_version": 1}
ALEMBIC_REV = "039"


def run_pg(*args, **kwargs):
    """Run a postgres client on the reachable cluster (direct, or via the compose container)."""
    if PG_DIRECT:
        return subprocess.run(list(args), capture_output=True, text=True, **kwargs)
    return subprocess.run(["docker", "exec", PG_CONTAINER, *args], capture_output=True, text=True, **kwargs)


def psql(db, sql):
    return run_pg("psql", "-U", "cryptonite", "-d", db, "-tAc", sql)


@pytest.fixture
def bed(tmp_path):
    """Throwaway source/scratch databases plus a throwaway DEST dir and script env."""
    tag = uuid.uuid4().hex[:8]
    src, scratch = f"a3_src_{tag}", f"a3_scratch_{tag}"
    dest = tmp_path / "backups"
    assert run_pg("createdb", "-U", "cryptonite", src).returncode == 0
    try:
        for t in TABLES:
            cols = "version_num VARCHAR(32) PRIMARY KEY" if t == "alembic_version" else "id SERIAL PRIMARY KEY, note TEXT"
            assert psql(src, f"CREATE TABLE {t}({cols})").returncode == 0
        for t, n in SEED.items():
            if t == "alembic_version":
                assert psql(src, f"INSERT INTO alembic_version VALUES ('{ALEMBIC_REV}')").returncode == 0
            else:
                assert psql(src, f"INSERT INTO {t}(note) SELECT 's' FROM generate_series(1,{n})").returncode == 0
        env = {**os.environ,
               "CRYPTONITE_BACKUP_DIR": str(dest),
               "CRYPTONITE_PG_CONTAINER": PG_CONTAINER,
               "CRYPTONITE_BACKUP_DB": src,
               "CRYPTONITE_BACKUP_SCRATCH": scratch}
        yield {"env": env, "src": src, "scratch": scratch, "dest": dest}
    finally:
        run_pg("dropdb", "-U", "cryptonite", "--if-exists", src)
        run_pg("dropdb", "-U", "cryptonite", "--if-exists", scratch)


def run_backup(env, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)


def source_count(bed, table):
    return psql(bed["src"], f"SELECT count(*) FROM {table}").stdout.strip()


def manifest_of(bed):
    dump = next((bed["dest"] / "db").glob("cryptonite-*.dump"))
    return Path(str(dump) + ".counts")


def rewrite_manifest(bed, edit):
    manifest = manifest_of(bed)
    lines = manifest.read_text().splitlines()
    manifest.write_text("\n".join(edit(lines)) + "\n")
    return manifest


def drill_evidence(bed):
    return next(bed["dest"].glob("drill-*.log")).read_text()


def scratch_exists(bed):
    out = run_pg("psql", "-U", "cryptonite", "-d", "postgres", "-tAc",
                 f"SELECT 1 FROM pg_database WHERE datname='{bed['scratch']}'")
    return out.stdout.strip() == "1"


# --------------------------------------------------------------------------- db mode


def test_db_backup_writes_dump_manifest_and_log(bed):
    assert run_backup(bed["env"], "db").returncode == 0
    dumps = list((bed["dest"] / "db").glob("cryptonite-*.dump"))
    assert len(dumps) == 1 and dumps[0].stat().st_size > 0
    manifest = Path(str(dumps[0]) + ".counts")
    assert manifest.is_file()
    got = dict(line.split("=", 1) for line in manifest.read_text().splitlines() if line)
    assert set(got) == set(TABLES) | {"alembic_version_hash"}
    for t in TABLES:
        assert got[t] == source_count(bed, t) == str(SEED[t])
    assert re.fullmatch(r"[0-9a-f]{64}", got["alembic_version_hash"])
    assert "db backup" in (bed["dest"] / "backup.log").read_text()


def test_db_manifest_and_dump_share_one_snapshot_under_concurrent_write(bed):
    """A row committed while `db` runs must not make the manifest disagree with the dump.

    The insert is performed only after the dump's `.part` file has bytes, i.e. after pg_dump has
    connected and taken its snapshot, so it is guaranteed invisible to that snapshot. With the
    shared-snapshot fix the manifest counts are read from the same snapshot and the drill passes;
    the pre-fix code (counts from a later live snapshot) would publish manifest=504 while the dump
    holds 503 rows and fail this drill, deterministically."""
    extra = 500
    assert psql(bed["src"],
                f"INSERT INTO operator_users(note) SELECT 'x' FROM generate_series(1,{extra})").returncode == 0
    proc = subprocess.Popen(["bash", str(SCRIPT), "db"], env=bed["env"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    part = None
    for _ in range(400):  # up to ~2s: a 500-row dump lingers well past a snapshot boundary
        candidates = list((bed["dest"] / "db").glob("cryptonite-*.dump.part"))
        if candidates and candidates[0].stat().st_size > 0:
            part = candidates[0]
            break
        time.sleep(0.005)
    assert part is not None, "db never reached the pg_dump stage"
    # Committed after the dump snapshot was taken: must be invisible to dump AND manifest counts.
    assert psql(bed["src"], "INSERT INTO operator_users(note) VALUES ('mid-backup')").returncode == 0
    _, err = proc.communicate(timeout=180)
    assert proc.returncode == 0, err[-2000:]
    expected = SEED["operator_users"] + extra
    got = dict(line.split("=", 1) for line in manifest_of(bed).read_text().splitlines() if line)
    assert got["operator_users"] == str(expected), "manifest must match the dump snapshot, not the live tail"
    assert run_backup(bed["env"], "drill").returncode == 0
    body = drill_evidence(bed)
    assert f"MATCH operator_users snapshot={expected} restored={expected}" in body
    assert "drill OK" in body


def test_db_backup_rejects_count_query_failure_and_publishes_nothing(bed):
    """A failing count query must abort the backup before any dump/manifest is published."""
    assert psql(bed["src"], "DROP TABLE execution_commands").returncode == 0
    r = run_backup(bed["env"], "db")
    assert r.returncode != 0
    log = (bed["dest"] / "backup.log").read_text()
    assert "count query failed for execution_commands" in log
    assert list((bed["dest"] / "db").glob("cryptonite-*.dump")) == []
    assert list((bed["dest"] / "db").glob("cryptonite-*.dump.counts")) == []
    assert list((bed["dest"] / "db").glob("*.part*")) == []  # no partial files left behind


# --------------------------------------------------------------------------- drill: happy path + restore failures


def test_drill_passes_with_matching_counts(bed):
    assert run_backup(bed["env"], "db").returncode == 0
    assert run_backup(bed["env"], "drill").returncode == 0
    body = drill_evidence(bed)
    assert "pg_restore exit=0" in body
    for t in TABLES:
        assert f"MATCH {t} snapshot={SEED[t]} restored={SEED[t]}" in body
    expected_hash = hashlib.sha256(ALEMBIC_REV.encode()).hexdigest()
    assert f"MATCH alembic_version hash={expected_hash}" in body
    assert "drill OK" in body and "drill FAILED" not in body
    assert "drill OK" in (bed["dest"] / "backup.log").read_text()
    assert not scratch_exists(bed)  # scratch always dropped, on success too


def test_drill_fails_on_corrupt_dump(bed):
    assert run_backup(bed["env"], "db").returncode == 0
    dump = next((bed["dest"] / "db").glob("cryptonite-*.dump"))
    with open(dump, "r+b") as f:
        f.truncate(100)  # not a valid pg_dump custom-format archive
    assert run_backup(bed["env"], "drill").returncode != 0
    body = drill_evidence(bed)
    assert "pg_restore exit=" in body and "pg_restore exit=0" not in body
    assert "drill FAILED" in body
    assert not scratch_exists(bed)  # scratch dropped even though the restore failed


def test_drill_fails_without_manifest(bed):
    assert run_backup(bed["env"], "db").returncode == 0
    manifest_of(bed).unlink()  # dumps that predate the manifest cannot prove equality
    assert run_backup(bed["env"], "drill").returncode != 0
    body = drill_evidence(bed)
    assert "no counts manifest" in body and "drill FAILED" in body


# --------------------------------------------------------------------------- drill: strict manifest validation


@pytest.mark.parametrize("mutate, expected", [
    # a valid db backup then a manifest edit must fail the drill with a precise reason
    (lambda lines: [ln for ln in lines if not ln.startswith("ai_mandates=")],
     "manifest missing ai_mandates"),
    (lambda lines: [ln for ln in lines if not ln.startswith("alembic_version_hash=")],
     "manifest missing alembic_version_hash"),
    (lambda lines: lines + ["operator_users=3"],
     "duplicate manifest line for operator_users"),
    (lambda lines: lines + ["foo=1"],
     "unexpected manifest key 'foo'"),
    (lambda lines: ["Ai-mandates=2" if ln.startswith("ai_mandates=") else ln for ln in lines],
     "unexpected manifest key 'Ai-mandates'"),
    (lambda lines: ["portfolio_decisions=abc" if ln.startswith("portfolio_decisions=") else ln for ln in lines],
     "non-numeric count for portfolio_decisions"),
    (lambda lines: ["alembic_version_hash=" + "0" * 64 if ln.startswith("alembic_version_hash=") else ln for ln in lines],
     "MISMATCH alembic_version hash"),
])
def test_drill_manifest_validation(bed, mutate, expected):
    assert run_backup(bed["env"], "db").returncode == 0
    rewrite_manifest(bed, mutate)
    assert run_backup(bed["env"], "drill").returncode != 0
    body = drill_evidence(bed)
    assert expected in body
    assert "drill FAILED" in body
    assert not scratch_exists(bed)


def test_drill_fails_on_empty_manifest(bed):
    assert run_backup(bed["env"], "db").returncode == 0
    manifest_of(bed).write_text("")  # truly empty: no table line at all
    assert run_backup(bed["env"], "drill").returncode != 0
    body = drill_evidence(bed)
    assert "manifest invalid" in body and "manifest missing operator_users" in body
    assert "drill FAILED" in body
    assert not scratch_exists(bed)


def test_drill_fails_on_count_mismatch(bed):
    assert run_backup(bed["env"], "db").returncode == 0
    rewrite_manifest(bed, lambda lines: [
        f"operator_users={SEED['operator_users'] + 10}" if ln.startswith("operator_users=") else ln
        for ln in lines])
    assert run_backup(bed["env"], "drill").returncode != 0
    body = drill_evidence(bed)
    assert f"MISMATCH operator_users snapshot={SEED['operator_users'] + 10} restored={SEED['operator_users']}" in body
    assert "drill FAILED" in body
    assert not scratch_exists(bed)


# --------------------------------------------------------------------------- safety


def test_drill_refuses_scratch_equal_to_source(bed):
    env = {**bed["env"], "CRYPTONITE_BACKUP_SCRATCH": bed["src"]}
    assert run_backup(bed["env"], "db").returncode == 0
    assert run_backup(env, "drill").returncode != 0
    assert "refusing drill" in (bed["dest"] / "backup.log").read_text()
    for t in TABLES:  # the source database is untouched
        assert source_count(bed, t) == str(SEED[t])