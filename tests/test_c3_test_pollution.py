"""C3 (F-M9, F-H2): the cleanup script, the no-active-test-spec guard (038) and the dev-DB test guard.

All against a throwaway database at Alembic head; the dev DB `cryptonite` is only ever opened by the
fail-fast check, which reads current_database() and closes.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from conftest import dev_db_env_vars
from connections import DB_URL, DevDatabaseError, assert_not_dev_db, database_name
from migrated_db import migrated_database, seed_paper_account

REPO = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cleanup = _load("cleanup_test_pollution")
backlog = _load("skip_scanner_backlog")


# --- dev-DB guard for tests ---------------------------------------------------------------------------

def test_database_name_and_dev_db_refusal():
    assert database_name("postgresql://h:5432/cryptonite_ci_c3") == "cryptonite_ci_c3"
    assert database_name("postgresql://cryptonite@h:5432") == "cryptonite"  # libpq: db defaults to the user
    with pytest.raises(DevDatabaseError):
        assert_not_dev_db("postgresql://localhost:5432/cryptonite")
    assert_not_dev_db("postgresql://localhost:5432/cryptonite_ci")
    env = {"DATABASE_URL": "postgresql://h/cryptonite_ci", "ASYNC_DATABASE_URL": "postgresql+asyncpg://h/cryptonite",
           "AGENT_TOOLS_DATABASE_URL": "postgresql://h/cryptonite_x", "REDIS_URL": "redis://h/0"}
    assert dev_db_env_vars(env) == ["ASYNC_DATABASE_URL"]


async def test_an_asyncpg_connection_to_the_dev_db_fails_fast(monkeypatch):
    # Without opening the real dev DB (absent in CI): treat this run's own DB as the dev DB. The guard
    # checks current_database() after connecting, so it catches the DB however the URL was built.
    import conftest
    monkeypatch.setattr(conftest, "DEV_DATABASES", frozenset({database_name(DB_URL)}))
    with pytest.raises(DevDatabaseError):
        await asyncpg.connect(DB_URL)
    with pytest.raises(DevDatabaseError):
        pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=1)
        await pool.close()
    monkeypatch.undo()
    con = await asyncpg.connect(DB_URL)  # and the run's DB itself is fine
    await con.close()


def test_pytest_refuses_to_start_against_the_dev_db():
    dev = re.sub(r"/[^/]*$", "/cryptonite", DB_URL)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_c3_test_pollution.py::test_database_name_and_dev_db_refusal"],
        cwd=REPO, env={**os.environ, "DATABASE_URL": dev}, capture_output=True, text=True, timeout=120,
    )
    assert res.returncode == 4, res.stdout + res.stderr  # pytest UsageError: nothing collected, nothing run
    assert "dev database" in res.stdout + res.stderr


# --- 038: no active test specs in a protected database --------------------------------------------------

@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_c3test") as url:
        yield url


async def _insert_spec(conn, slug: str, archived: bool = False) -> uuid.UUID:
    sid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO scan_specs (id, slug, name, active_version_id, is_archived) VALUES ($1, $2, $2, NULL, $3)",
        sid, slug, archived)
    return sid


async def test_guard_refuses_active_test_specs_only_in_a_protected_db(db_url):
    conn = await asyncpg.connect(db_url)
    try:
        # unprotected (a throwaway DB): tests may create active test specs
        assert await conn.fetchval("SELECT cryptonite_is_protected_db()") is False
        await _insert_spec(conn, "test-spec-free")

        await conn.execute("SET cryptonite.protected_db = 'on'")
        assert await conn.fetchval("SELECT cryptonite_is_protected_db()") is True
        for slug in ("test-spec-abc", "TEST_x", "test-"):
            with pytest.raises(asyncpg.CheckViolationError, match="test spec"):
                await _insert_spec(conn, slug)
        archived = await _insert_spec(conn, "test-spec-archived", archived=True)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE scan_specs SET is_archived = false WHERE id = $1", archived)
        real = await _insert_spec(conn, "rsi-test-variant")  # not a test- prefix
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE scan_specs SET slug = 'test-renamed' WHERE id = $1", real)
        # archiving and unrelated updates of a (legacy) active test spec stay allowed
        await conn.execute("SET cryptonite.protected_db = 'off'")
        legacy = await _insert_spec(conn, "test-spec-legacy")
        await conn.execute("SET cryptonite.protected_db = 'on'")
        await conn.execute("UPDATE scan_specs SET name = 'renamed', updated_at = now() WHERE id = $1", legacy)
        await conn.execute("UPDATE scan_specs SET is_archived = true WHERE id = $1", legacy)
    finally:
        await conn.execute("RESET cryptonite.protected_db")
        await conn.execute("DELETE FROM scan_specs WHERE slug ~* '^test[-_]' OR slug = 'rsi-test-variant'")
        await conn.close()


# --- cleanup_test_pollution.py ---------------------------------------------------------------------------

XRP_ID = uuid.UUID("589b83cd-ef96-42db-b431-199fd90184b6")
SCALP_UNIVERSE = {"instrument_ids": ["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"], "timeframes": ["5m", "15m", "1h", "4h", "1d"]}
SCALP_RULES = [{"category": "universe", "logic": "AND", "conditions": []}]
TEST_ACCOUNTS = {  # account -> latched
    "ACC-RACE-050d3f96": True, "ACC-CRASH-10cab310": True, "ACC-051dd738": False, "ACC-DEMO-13677FF0": False,
    "ACC-RESET-13BC3768": False, "ACC-ABORT-0a1b2c3d": True, "ACC-E2E-0A1B2C3D": False, "TEST-ACC": True,
}
REAL_ROWS = [("DEMO", "BINANCE-SPOT-DEMO", True, 10), ("DEMO", "DEMO-ACCOUNT-001", False, 0),
             ("PAPER", "PAPER-ACCOUNT-001", False, 1)]


async def _seed(conn) -> dict:
    ids = {}
    await seed_paper_account(conn)
    # scanner specs: two active test specs, one archived test spec, one real spec, scalp-multi-tf v1 with 1d
    ids["test_specs"] = [await _insert_spec(conn, f"test-spec-{uuid.uuid4().hex[:8]}") for _ in range(2)]
    ids["archived_test_spec"] = await _insert_spec(conn, "test-spec-old", archived=True)
    ids["real_spec"] = await _insert_spec(conn, "rsi-oversold-reversal")
    scalp, v1, run = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO scan_specs (id, slug, name, description) VALUES ($1, 'scalp-multi-tf', 'Execution Strategist Multi-TF', $2)",
        scalp, cleanup.SCALP_OLD_DESCRIPTION)
    h = cleanup.compute_spec_hash(SCALP_UNIVERSE, {"rules": SCALP_RULES})
    await conn.execute(
        "INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules) VALUES ($1, $2, 1, $3, $4::jsonb, $5::jsonb)",
        v1, scalp, h, json.dumps(SCALP_UNIVERSE), json.dumps(SCALP_RULES))
    await conn.execute("UPDATE scan_specs SET active_version_id = $1 WHERE id = $2", v1, scalp)
    await conn.execute(
        """INSERT INTO scan_runs (id, spec_version_id, spec_hash, scanner_version, indicator_version, input_watermark_ns,
               source_partitions, source_partition_checksums, trigger_reason, status)
           VALUES ($1, $2, $3, 'v', 'v', 1, '[]', '{}', 'COMMITTED_BAR_EVENT', 'COMPLETED')""", run, v1, h)
    for inst in SCALP_UNIVERSE["instrument_ids"]:
        await conn.execute(
            "INSERT INTO scanner_checkpoints (spec_version_id, instrument_id, last_processed_watermark_ns, last_run_id) "
            "VALUES ($1, $2, 1791240660000000000, $3)", v1, inst, run)
    ids.update(scalp=scalp, scalp_v1=v1)
    # mandates: the XRP one (with a pending ADD_RISK approval) and a BTC/ETH one that must not change
    await conn.execute(
        "INSERT INTO ai_mandates (id, name, capital_budget_usd, instruments, max_position_pct) VALUES ($1, 'XRPUSDT.BINANCE', 1000, $2, 10)",
        XRP_ID, ["XRPUSDT.BINANCE"])
    ids["btc_mandate"] = await conn.fetchval(
        "INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct) "
        "VALUES ('BTC-ETH spot L1', 5000, $1, 10) RETURNING id", ["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"])
    await conn.execute(
        """INSERT INTO ai_approvals (mandate_id, action_class, command_type, payload, source, source_ref, level_at_creation,
               timeout_policy, expires_at) VALUES ($1, 'ADD_RISK', 'SUBMIT_ORDER', '{}', 't', 'r1', 1, 'DENY', now() + interval '1 hour')""",
        XRP_ID)
    # strategy instances
    strat = uuid.UUID(int=0xA15)
    for iid, (inst, tf) in {**cleanup.INSTANCES_TO_STOP, "INST-KEEP0001": ("ETHUSDT.BINANCE", "1h")}.items():
        await conn.execute(
            """INSERT INTO paper_strategy_instances (instance_id, account_id, strategy_version_id, strategy_source_hash,
                   risk_policy_hash, strategy_name, instrument_id, timeframe, resolved_parameters, allocation_usd, status)
               VALUES ($1, 'PAPER-ACCOUNT-001', $2, 'h', 'h', 'ema-cross', $3, $4, '{}', 100, 'RUNNING')""",
            iid, strat, inst, tf)
    # kill-switch rows: test accounts, real accounts (one latched), one unclassified
    for acc, latched in TEST_ACCOUNTS.items():
        await conn.execute(
            "INSERT INTO execution_kill_switch (environment, account_id, is_latched, latch_generation) VALUES ('DEMO', $1, $2, 1)",
            acc, latched)
    for env, acc, latched, gen in REAL_ROWS:
        await conn.execute(
            "INSERT INTO execution_kill_switch (environment, account_id, is_latched, latch_generation, latched_by, reason) "
            "VALUES ($1, $2, $3, $4, 'OPERATOR:admin', 'Manual operator emergency halt')", env, acc, latched, gen)
    await conn.execute(
        "INSERT INTO execution_kill_switch (environment, account_id, is_latched, latch_generation) VALUES ('DEMO', 'SOME-OTHER-ACC', true, 3)")
    return ids


async def _run(url: str, *args: str, capsys) -> tuple[int, str]:
    code = await cleanup.main(["--database-url", url, "--wait-seconds", "0", *args])
    return code, capsys.readouterr().out


async def _snapshot(conn) -> str:
    parts = []
    for t in ("scan_specs", "scan_spec_versions", "scanner_checkpoints", "ai_mandates", "ai_autonomy_events",
              "ai_approvals", "paper_strategy_instances", "execution_commands", "execution_kill_switch"):
        parts.append(await conn.fetchval(f"SELECT coalesce(json_agg(t ORDER BY t::text), '[]')::text FROM {t} t"))
    return "\n".join(parts)


@pytest.fixture
async def seeded(db_url):
    conn = await asyncpg.connect(db_url)
    for t in ("execution_commands", "execution_kill_switch", "ai_approvals", "ai_autonomy_events", "ai_mandates",
              "scanner_checkpoints", "scan_runs", "scan_spec_versions", "scan_specs"):
        await conn.execute(f"DELETE FROM {t}")
    await conn.execute("DELETE FROM paper_positions WHERE instance_id LIKE 'INST-%'")
    await conn.execute("DELETE FROM paper_strategy_instances WHERE instance_id LIKE 'INST-%'")
    ids = await _seed(conn)
    yield conn, ids
    await conn.close()


async def test_dry_run_changes_nothing_and_apply_needs_the_plan_id(db_url, seeded, capsys):
    conn, _ = seeded
    before = await _snapshot(conn)
    code, out = await _run(db_url, capsys=capsys)
    assert code == 0, out
    assert "mode: dry-run (READ ONLY)" in out
    assert "1. archive 2 active test spec(s)" in out
    assert "v1 ['5m', '15m', '1h', '4h', '1d'] -> v2 ['5m', '15m', '1h', '4h']" in out
    assert "5. delete 8 test-account execution_kill_switch row(s) (4 latched)" in out
    assert "SOME-OTHER-ACC" in out.split("unclassified rows (left alone):")[1]
    assert await _snapshot(conn) == before
    plan_id = re.search(r"plan id: ([0-9a-f]{12})", out).group(1)

    code, out = await _run(db_url, "--apply", "--confirm", "000000000000", capsys=capsys)
    assert code == 2 and "REFUSED: plan id" in out and "ROLLED BACK" in out
    assert await _snapshot(conn) == before

    # the plan changed since the dry-run: refused
    await _insert_spec(conn, "test-spec-late")
    code, out = await _run(db_url, "--apply", "--confirm", plan_id, capsys=capsys)
    assert code == 2 and "REFUSED: plan id" in out


async def test_apply_cleans_test_pollution_and_leaves_real_rows_alone(db_url, seeded, capsys):
    conn, ids = seeded
    real_before = await conn.fetch(
        "SELECT row_to_json(k)::text FROM execution_kill_switch k WHERE account_id = ANY($1::text[]) ORDER BY id",
        [r[1] for r in REAL_ROWS] + ["SOME-OTHER-ACC"])
    btc_before = await conn.fetchrow("SELECT row_to_json(m)::text AS j FROM ai_mandates m WHERE id = $1", ids["btc_mandate"])
    code, out = await _run(db_url, capsys=capsys)
    plan_id = re.search(r"plan id: ([0-9a-f]{12})", out).group(1)

    code, out = await _run(db_url, "--apply", "--confirm", plan_id, capsys=capsys)
    assert code == 0, out
    assert "COMMITTED" in out
    assert "active_test_specs: 2" in out.split("== Before ==")[1] and "active_test_specs: 0" in out.split("== After")[1]

    # 1. test specs archived, never deleted; the real spec stays active
    rows = await conn.fetch("SELECT slug, is_archived FROM scan_specs WHERE slug LIKE 'test-spec-%'")
    assert len(rows) == 3 and all(r["is_archived"] for r in rows)
    assert await conn.fetchval("SELECT is_archived FROM scan_specs WHERE id = $1", ids["real_spec"]) is False
    # 2. scalp-multi-tf v2 without 1d, hash as the API computes it, checkpoints carried over, v1 kept
    v2 = await conn.fetchrow(
        "SELECT v.id, v.version_num, v.universe, v.rules, v.spec_hash, s.description FROM scan_specs s "
        "JOIN scan_spec_versions v ON v.id = s.active_version_id WHERE s.id = $1", ids["scalp"])
    universe = json.loads(v2["universe"])
    assert v2["version_num"] == 2 and universe["timeframes"] == ["5m", "15m", "1h", "4h"]
    assert v2["spec_hash"] == cleanup.compute_spec_hash(universe, {"rules": json.loads(v2["rules"])})
    assert "1D" not in v2["description"]
    assert await conn.fetchval("SELECT count(*) FROM scan_spec_versions WHERE spec_id = $1", ids["scalp"]) == 2
    assert await conn.fetchval("SELECT count(*) FROM scanner_checkpoints WHERE spec_version_id = $1", v2["id"]) == 2
    # 3. XRP mandate archived through PAUSE (approval cancelled), BTC mandate untouched
    assert await conn.fetchval("SELECT status FROM ai_mandates WHERE id = $1", XRP_ID) == "ARCHIVED"
    events = await conn.fetch("SELECT kind, actor, detail FROM ai_autonomy_events WHERE mandate_id = $1 ORDER BY id", XRP_ID)
    assert [e["kind"] for e in events] == ["PAUSED", "EDITED"]
    assert json.loads(events[1]["detail"])["changes"] == {"status": ["PAUSED", "ARCHIVED"]}
    assert await conn.fetchval("SELECT status FROM ai_approvals WHERE mandate_id = $1", XRP_ID) == "CANCELLED"
    assert (await conn.fetchrow("SELECT row_to_json(m)::text AS j FROM ai_mandates m WHERE id = $1", ids["btc_mandate"])) == btc_before
    # 4. one pending STOP per instance, the orchestrator applies it; the other instance gets none
    cmds = await conn.fetch("SELECT target_instance_id, environment, account_id, status, requester_id FROM execution_commands "
                            "WHERE command_type = 'STOP' ORDER BY 1")
    assert [(c["target_instance_id"], c["environment"], c["status"]) for c in cmds] == [
        ("INST-513B9013", "PAPER", "PENDING"), ("INST-7DF91538", "PAPER", "PENDING")]
    # 5. test kill-switch rows gone; real and unclassified rows byte-identical
    left = {r["account_id"] for r in await conn.fetch("SELECT account_id FROM execution_kill_switch")}
    assert left == {"BINANCE-SPOT-DEMO", "DEMO-ACCOUNT-001", "PAPER-ACCOUNT-001", "SOME-OTHER-ACC"}
    assert await conn.fetch(
        "SELECT row_to_json(k)::text FROM execution_kill_switch k WHERE account_id = ANY($1::text[]) ORDER BY id",
        [r[1] for r in REAL_ROWS] + ["SOME-OTHER-ACC"]) == real_before

    # idempotent: a second dry-run finds nothing left to do
    code, out = await _run(db_url, capsys=capsys)
    assert code == 0
    assert "1. archive 0 active test spec(s)" in out and "2. scalp-multi-tf: no change" in out
    assert "3. archive 0 XRP-only mandate(s)" in out and "none (STOP already pending)" in out
    assert "5. delete 0 test-account" in out


async def test_refusals_roll_back_everything(db_url, seeded, capsys):
    conn, _ = seeded
    await conn.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price, quantity,
               current_price, status) VALUES ('p1', 'PAPER-ACCOUNT-001', 'INST-513B9013', 'SOLUSDT.BINANCE', 'LONG', 1, 1, 1, 'OPEN')""")
    await conn.execute("UPDATE paper_strategy_instances SET timeframe = '5m' WHERE instance_id = 'INST-7DF91538'")
    before = await _snapshot(conn)
    code, out = await _run(db_url, capsys=capsys)
    assert code == 1
    assert "REFUSED: instance INST-513B9013 holds 1 open position(s)" in out
    assert "REFUSED: instance INST-7DF91538 is BTCUSDT.BINANCE 5m, expected BTCUSDT.BINANCE 15m" in out
    plan_id = re.search(r"plan id: ([0-9a-f]{12})", out).group(1)
    code, out = await _run(db_url, "--apply", "--confirm", plan_id, capsys=capsys)
    assert code == 2 and "ROLLED BACK" in out
    assert await _snapshot(conn) == before


async def test_a_failed_apply_check_rolls_back_without_asserts(db_url, seeded, capsys, monkeypatch):
    conn, _ = seeded
    code, out = await _run(db_url, capsys=capsys)
    plan_id = re.search(r"plan id: ([0-9a-f]{12})", out).group(1)
    before = await _snapshot(conn)
    real_apply = cleanup.apply

    async def apply_then_fail(c, plan):
        await real_apply(c, plan)  # every write done, then an invariant fails
        cleanup._expect(False, "injected")

    monkeypatch.setattr(cleanup, "apply", apply_then_fail)
    code, out = await _run(db_url, "--apply", "--confirm", plan_id, capsys=capsys)
    assert code == 3 and "CHECK FAILED: injected" in out and "ROLLED BACK" in out and "COMMITTED" not in out
    assert await _snapshot(conn) == before
    with pytest.raises(cleanup.CheckFailed):
        cleanup._expect(False, "x")
    assert "assert " not in Path(cleanup.__file__).read_text().split("async def apply(")[1].split("def check_after")[0]


def test_a_real_account_matching_a_test_pattern_is_refused():
    assert cleanup.test_account_label("ACC-RACE-050d3f96")
    assert cleanup.test_account_label("ACC-RACE-050D3F96") is None  # m5a uses lowercase hex
    for real in cleanup.REAL_ACCOUNTS:
        assert cleanup.test_account_label(real) is None


# --- skip_scanner_backlog.py ----------------------------------------------------------------------------

class FakeRedis:
    def __init__(self, pending: int = 0):
        self.calls: list[tuple] = []
        self.pending = [{"message_id": f"{i}-0".encode()} for i in range(pending)]

    def xinfo_stream(self, stream):
        return {"length": 900, "last-generated-id": b"1791300000000-0"}

    def xinfo_groups(self, stream):
        moved = any(c[0] == "setid" for c in self.calls)
        return [{"name": b"other", "lag": 0},
                {"name": b"scanner-worker", "lag": 0 if moved else 881, "pending": len(self.pending),
                 "last-delivered-id": b"1791300000000-0" if moved else b"1791200000000-0", "entries-read": 19, "consumers": 1}]

    def xgroup_setid(self, stream, group, id):
        self.calls.append(("setid", stream, group, id))

    def xpending_range(self, stream, group, min, max, count):
        return self.pending[:count]

    def xack(self, stream, group, *ids):
        self.calls.append(("xack", len(ids)))
        self.pending = self.pending[len(ids):]
        return len(ids)


def test_skip_backlog_dry_run_reads_only(monkeypatch, capsys):
    fake = FakeRedis(pending=3)
    monkeypatch.setattr(backlog.redis.Redis, "from_url", staticmethod(lambda url: fake))
    assert backlog.main(["--redis-url", "redis://x"]) == 0
    out = capsys.readouterr().out
    assert "lag=881 pending=3" in out and "XGROUP SETID cryptonite:stream:closed_bar_committed scanner-worker $" in out
    assert f"rollback would be: {ROLLBACK}" in out
    assert fake.calls == []


ROLLBACK = "XGROUP SETID cryptonite:stream:closed_bar_committed scanner-worker 1791200000000-0 ENTRIESREAD 19"


def test_skip_backlog_apply_moves_the_group_to_the_stream_end(monkeypatch, capsys):
    fake = FakeRedis(pending=3)
    seen_before_setid = []
    real_setid = fake.xgroup_setid
    fake.xgroup_setid = lambda *a: (seen_before_setid.append(capsys.readouterr().out), real_setid(*a))
    monkeypatch.setattr(backlog.redis.Redis, "from_url", staticmethod(lambda url: fake))
    assert backlog.main(["--redis-url", "redis://x", "--apply"]) == 0
    assert fake.calls == [("setid", backlog.STREAM, backlog.GROUP, "$")]
    # the exact rollback is printed before the group moves
    assert f"ROLLBACK (restores the group exactly): {ROLLBACK}" in seen_before_setid[0]
    assert "lag=0 pending=3" in capsys.readouterr().out.split("== After ==")[1]  # PEL untouched without --ack-pending

    fake = FakeRedis(pending=3)
    monkeypatch.setattr(backlog.redis.Redis, "from_url", staticmethod(lambda url: fake))
    assert backlog.main(["--redis-url", "redis://x", "--apply", "--ack-pending"]) == 0
    assert fake.calls == [("setid", backlog.STREAM, backlog.GROUP, "$"), ("xack", 3)]
    out = capsys.readouterr().out
    assert "acknowledging (irreversible): 0-0 1-0 2-0" in out and "acknowledged 3 pending entries" in out


def test_skip_backlog_rollback_without_entries_read():
    g = {"last-delivered-id": "5-0", "entries-read": None}
    assert backlog.rollback_command("s", "g", g) == "XGROUP SETID s g 5-0"


def test_skip_backlog_refuses_a_missing_group(monkeypatch, capsys):
    fake = FakeRedis()
    monkeypatch.setattr(backlog.redis.Redis, "from_url", staticmethod(lambda url: fake))
    assert backlog.main(["--redis-url", "redis://x", "--group", "nope", "--apply"]) == 2
    assert fake.calls == []
