"""Setups inbox (Phase 2 Track C1): API shape, joins, filters and authorization."""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_scanner.costs import cost_in_r, default_market, round_trip_cost_fraction
from migrated_db import migrated_database

BASE = "/api/v1/setups"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_setups_api") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("DELETE FROM ai_profile_coin_locks")
    await harness.pool.execute("DELETE FROM signal_candidates")
    try:
        yield harness
    finally:
        await close_harness(harness)


async def candidate(h, *, slug="swing-trend-pullback-1h", instrument="BTCUSDT.BINANCE", timeframe="1h",
                    entry=100.0, stop=95.0, target=110.0, age_h=1.0) -> uuid.UUID:
    version_id, scan_id, cid = (uuid.uuid4() for _ in range(3))
    spec_id = await h.pool.fetchval("SELECT id FROM scan_specs WHERE slug = $1", slug)
    if spec_id is None:
        spec_id = uuid.uuid4()
        await h.pool.execute("INSERT INTO scan_specs (id, slug, name) VALUES ($1, $2, 'setups test')", spec_id, slug)
    await h.pool.execute(
        """INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
           SELECT $1, $2, COALESCE(MAX(version_num), 0) + 1, 'setups-test', '{}'::jsonb, '[]'::jsonb
           FROM scan_spec_versions WHERE spec_id = $2""", version_id, spec_id)
    await h.pool.execute(
        """INSERT INTO scan_runs (id, spec_version_id, spec_hash, scanner_version, indicator_version,
               input_watermark_ns, source_partitions, source_partition_checksums, trigger_reason)
           VALUES ($1, $2, 'setups-test', 'test', 'test', 1, '{}'::jsonb, '{}'::jsonb, 'MANUAL')""", scan_id, version_id)
    created = dt.datetime.now(dt.UTC) - dt.timedelta(hours=age_h)
    await h.pool.execute(
        """INSERT INTO signal_candidates (id, scan_run_id, spec_version_id, instrument_id, timeframe,
               bar_open_ns, bar_close_inclusive_ns, interval_end_exclusive_ns, matched_rules, metrics,
               suggested_entry, suggested_stop, suggested_target, created_at)
           VALUES ($1, $2, $3, $4, $5, 1, 2, 3, '["rsi<30"]'::jsonb, $6::jsonb, $7, $8, $9, $10)""",
        cid, scan_id, version_id, instrument, timeframe, json.dumps({"preset_slug": slug}), entry, stop, target,
        created)
    return cid


async def get(h, params=None, *, authed=True):
    async with h.client() as client:
        if not authed:
            client.cookies.clear()
        return await client.get(BASE, params=params or {})


async def test_requires_an_operator_session(h):
    assert (await get(h, authed=False)).status_code == 401


async def test_shape_cost_owner_funnel_and_policies(h):
    owner = await h.mandate(instruments=("BTCUSDT.BINANCE",))
    await h.pool.execute("INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('BTC', $1)", owner.id)
    cid = await candidate(h, slug="swing-trend-pullback-1h")
    for stage, outcome, reason in (("SCAN", "CANDIDATE", None), ("GATE", "PASSED", None),
                                   ("DEBATE", "SKIPPED", "LOW_CONVICTION")):
        await h.pool.execute(
            "INSERT INTO ai_profile_decisions (mandate_id, stage, outcome, reason, candidate_id) "
            "VALUES ($1, $2, $3, $4, $5)", owner.id, stage, outcome, reason, cid)
    await h.pool.execute(
        "INSERT INTO policy_decisions (candidate_id, policy, decision, side, size_factor, reason, model, cost_usd) "
        "VALUES ($1, 'A', 'TAKE', 'LONG', 1, 'rule', NULL, 0), ($1, 'B', 'SKIP', NULL, NULL, 'weak', 'gemini', 0.01)",
        cid)

    response = await get(h)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1 and body["cost_model_version"] == "v1"
    s = body["setups"][0]
    assert s["id"] == str(cid) and s["instrument_id"] == "BTCUSDT.BINANCE"
    assert s["provenance"]["preset"] == "swing-trend-pullback-1h"
    assert s["provenance"]["timeframe"] == "1h" and s["provenance"]["scan_run_id"]
    assert s["direction"] == "LONG"
    assert float(s["entry"]) == 100 and float(s["stop"]) == 95 and float(s["target"]) == 110
    assert 0 <= s["age_seconds"] < 2 * 3600 + 60 and s["age_seconds"] >= 3600 - 60
    expected = cost_in_r(100.0, 95.0, round_trip_cost_fraction(default_market("LONG"), s["cost"]["hold_hours"]))
    assert s["cost"]["cost_r"] == pytest.approx(expected, abs=1e-4) and s["cost"]["cost_model"] == "spot-v1"
    assert s["profile"]["id"] == str(owner.id) and s["eligibility"] == {"eligible": True, "reason": None}
    assert s["funnel"]["furthest_stage"] == "DEBATE" and s["funnel"]["reason"] == "LOW_CONVICTION"
    assert [t["stage"] for t in s["funnel"]["trail"]] == ["SCAN", "GATE", "DEBATE"]
    assert [(p["policy"], p["decision"]) for p in s["policies"]] == [("A", "TAKE"), ("B", "SKIP")]
    assert s["brief"]["matched_rules"] == ["rsi<30"] and s["brief"]["metrics"]["preset_slug"]


async def test_unowned_and_ineligible_candidates_and_null_funnel(h):
    paused = await h.mandate(instruments=("ETHUSDT.BINANCE",), status="PAUSED")
    await h.pool.execute("INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('ETH', $1)", paused.id)
    await candidate(h, instrument="SOLUSDT.BINANCE")
    await candidate(h, instrument="ETHUSDT.BINANCE")
    by_inst = {s["instrument_id"]: s for s in (await get(h)).json()["setups"]}
    sol, eth = by_inst["SOLUSDT.BINANCE"], by_inst["ETHUSDT.BINANCE"]
    assert sol["profile"] is None and sol["eligibility"] == {"eligible": False, "reason": "NO_OWNER"}
    assert sol["funnel"] == {"furthest_stage": None, "outcome": None, "reason": None, "trail": []}
    assert sol["policies"] == []
    assert eth["eligibility"] == {"eligible": False, "reason": "PROFILE_PAUSED"}


async def test_since_default_window_and_profile_filter(h):
    owner = await h.mandate(instruments=("BTCUSDT.BINANCE",))
    await h.pool.execute("INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('BTC', $1)", owner.id)
    fresh = await candidate(h, instrument="BTCUSDT.BINANCE", age_h=1)
    stale = await candidate(h, instrument="BTCUSDT.BINANCE", age_h=10)
    await candidate(h, instrument="SOLUSDT.BINANCE", age_h=1)

    ids = {s["id"] for s in (await get(h)).json()["setups"]}
    assert str(fresh) in ids and str(stale) not in ids and len(ids) == 2

    since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=12)).isoformat()
    assert (await get(h, {"since": since})).json()["count"] == 3

    only = (await get(h, {"profile": str(owner.id), "since": since})).json()["setups"]
    assert {s["id"] for s in only} == {str(fresh), str(stale)}
    assert (await get(h, {"profile": str(uuid.uuid4())})).status_code == 404
