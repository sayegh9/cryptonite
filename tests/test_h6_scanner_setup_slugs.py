"""H6 item 3: the Brain's ``_recent_scanner_setups`` keeps global-scanner setups, whose metrics carry no slug.

Runs against the test database at Alembic head (DATABASE_URL) as cryptonite_agent_ro, the role the tool uses in
production (migration 059 grants it the spec slug). Each test commits its own rows and removes them in ``finally``.
"""

import json
import time
import uuid

import asyncpg
import pytest
from connections import DB_URL, role_db_url
from cryptonite_agents import tools

AGENT_URL = role_db_url("cryptonite_agent_ro", "agent_ro_dev_pass")


@pytest.fixture(autouse=True)
def agent_tools(monkeypatch):
    """The tool reads as the agent role, through a fresh engine."""
    monkeypatch.setattr(tools, "DB_URL", AGENT_URL)
    monkeypatch.setattr(tools, "_engine", None)
    yield
    if tools._engine is not None:
        tools._engine.dispose()


async def _seed(conn, instrument: str, slug: str, metrics: dict) -> uuid.UUID:
    """One spec, its version, a run and a SHORT candidate on ``instrument``; returns the spec id."""
    spec, version, run = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await conn.execute("INSERT INTO scan_specs (id, slug, name) VALUES ($1, $2, $2)", spec, slug)
    await conn.execute(
        "INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules) "
        "VALUES ($1, $2, 1, 'h6', '[]', '[]')", version, spec)
    await conn.execute(
        "INSERT INTO scan_runs (id, spec_version_id, spec_hash, scanner_version, indicator_version, input_watermark_ns, "
        "source_partitions, source_partition_checksums, trigger_reason) "
        "VALUES ($1, $2, 'h6', 't', 't', 0, '[]', '{}', 'test')", run, version)
    close_ns = time.time_ns()
    await conn.execute(
        "INSERT INTO signal_candidates (id, scan_run_id, spec_version_id, instrument_id, timeframe, bar_open_ns, "
        "bar_close_inclusive_ns, interval_end_exclusive_ns, matched_rules, metrics, suggested_entry, suggested_stop, "
        "suggested_target) VALUES ($1, $2, $3, $4, '1h', $5, $6, $7, '[]', $8, 100, 102, 95)",
        uuid.uuid4(), run, version, instrument, close_ns - 3_600_000_000_000, close_ns, close_ns + 1,
        json.dumps(metrics))
    return spec


async def test_a_global_spec_candidate_gets_its_slug_from_the_spec():
    instrument = f"H6{uuid.uuid4().hex[:6].upper()}USDT.BINANCE"
    slug = f"h6-global-{uuid.uuid4().hex[:6]}"
    conn = await asyncpg.connect(DB_URL)
    try:
        spec = await _seed(conn, instrument, slug, {"price": 100})  # a global spec: no slug in metrics
        setups = tools._recent_scanner_setups(instrument, {})
        assert [(s["preset_slug"], s["direction"], s["timeframe"]) for s in setups] == [(slug, "SHORT", "1h")]
    finally:
        await conn.execute("DELETE FROM scan_specs WHERE id = $1", spec)
        await conn.close()


async def test_a_metrics_slug_still_wins_over_the_spec_slug():
    instrument = f"H6{uuid.uuid4().hex[:6].upper()}USDT.BINANCE"
    conn = await asyncpg.connect(DB_URL)
    try:
        spec = await _seed(conn, instrument, f"h6-spec-{uuid.uuid4().hex[:6]}", {"preset_slug": "trend-pullback"})
        assert [s["preset_slug"] for s in tools._recent_scanner_setups(instrument, {})] == ["trend-pullback"]
    finally:
        await conn.execute("DELETE FROM scan_specs WHERE id = $1", spec)
        await conn.close()
