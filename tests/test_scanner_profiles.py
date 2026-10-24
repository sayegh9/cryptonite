"""ADR 0011 P1.2: profiles scan spot bars and preserve global scanner behavior."""

import asyncio
import importlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import polars as pl
import pytest
from cryptonite_scanner.models import SignalCandidateRead, compute_spec_hash
from cryptonite_scanner.presets import preset_by_slug
from cryptonite_scanner.resampler import ONE_MINUTE_NS
from cryptonite_scanner_service import worker as worker_mod
from cryptonite_scanner_service.worker import ScannerWorker
from cryptonite_schemas.profile import preset_ids_for

BTC = "BTCUSDT.BINANCE"
ETH = "ETHUSDT.BINANCE"
END = 1_791_201_600 * 10**9


@pytest.fixture
async def scanner(monkeypatch):
    """Real isolated Postgres, deterministic lake, matching engine, and a captured Redis queue."""
    url = os.environ["DATABASE_URL"]
    assert "cryptonite_ci_" in url
    pool = await asyncpg.create_pool(url, min_size=1, max_size=3)
    await pool.execute("TRUNCATE ai_mandates, instruments, scan_specs CASCADE")
    for inst in (BTC, ETH):
        await pool.execute(
            """INSERT INTO instruments (instrument_id, raw_symbol, base_currency, quote_currency,
                price_precision, size_precision, price_increment, size_increment)
                VALUES ($1, $2, $3, 'USDT', 2, 4, 0.01, 0.0001)""",
            inst, inst.split(".")[0], inst[:3],
        )
    w = ScannerWorker(database_url=url, analytics_lake_path="/nonexistent-p12-lake")
    w.pool = pool
    w.redis = AsyncMock()
    w.redis.xadd.return_value = "1-0"
    w._publish_brief = AsyncMock(return_value=None)
    calls = []
    df = pl.DataFrame({
        "bar_open_ns": list(range(END - 5 * 60 * ONE_MINUTE_NS, END, ONE_MINUTE_NS)),
        "ts_event_ns": list(range(END - 5 * 60 * ONE_MINUTE_NS + ONE_MINUTE_NS, END + 1, ONE_MINUTE_NS)),
        "open": [100.] * 300, "high": [101.] * 300, "low": [99.] * 300,
        "close": [100.] * 300, "volume": [1.] * 300,
    })
    w.query_engine = SimpleNamespace(query_bars_df=lambda **kwargs: df)
    monkeypatch.setattr(worker_mod, "time", SimpleNamespace(
        time_ns=lambda: END + 30 * 10**9, monotonic=time.monotonic,
        strftime=time.strftime, gmtime=time.gmtime,
    ))
    monkeypatch.setattr(worker_mod.ScannerEngine, "enrich_indicators", lambda frame: frame)

    def evaluate(**kw):
        calls.append(kw)
        short = kw["risk_config"].direction == "SHORT"
        return [SignalCandidateRead(
            id=uuid4(), scan_run_id=kw["scan_run_id"], spec_version_id=kw["spec_version_id"],
            instrument_id=kw["instrument_id"], timeframe=kw["timeframe"],
            bar_open_ns=r["bar_open_ns"], bar_close_inclusive_ns=r["interval_end_exclusive_ns"] - 1,
            interval_end_exclusive_ns=r["interval_end_exclusive_ns"], matched_rules=["test"],
            metrics={"direction": kw["risk_config"].direction}, suggested_entry=Decimal(100),
            suggested_stop=Decimal(102 if short else 98), suggested_target=Decimal(96 if short else 104),
            created_at=datetime.now(UTC),
        ) for r in kw["df"].to_dicts()]

    monkeypatch.setattr(worker_mod.ScannerEngine, "evaluate", evaluate)
    yield SimpleNamespace(worker=w, pool=pool, calls=calls)
    await pool.execute("TRUNCATE ai_mandates, instruments, scan_specs CASCADE")
    await pool.close()


async def profile(h, *, coins=(BTC,), style="SWING", direction="LONG", dry_run=False, status="ACTIVE"):
    """Seed an existing running profile; lifecycle start guards are covered elsewhere."""
    return await h.pool.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct,
            status, style, direction, dry_run, markets, allow_perps)
            VALUES ($1, 1000, $2, 10, $3, $4, $5, $6, $7, $8) RETURNING id""",
        str(uuid4()), list(coins), status, style, direction, dry_run,
        ["PERP"] if any("-PERP" in c for c in coins) else (["SPOT", "PERP"] if direction != "LONG" else ["SPOT"]),
        any("-PERP" in c for c in coins) or direction != "LONG",
    )


async def bar(h, *, inst=BTC, tf="1h", event="profile-bar"):
    """Deliver a single closed-bar event through the real worker handler."""
    await h.worker._handle_stream_message("closed", "1-0", {"data": json.dumps({
        "event_id": event, "instrument_id": inst, "timeframe": tf, "bar_close_inclusive_ns": END - 1,
    })})


@pytest.mark.parametrize("style,direction,tf", [("SWING", "LONG", "1h"), ("SWING", "SHORT", "4h"),
                                                ("INTRADAY", "BOTH", "15m")])
async def test_scanner_profiles_filter_coins_style_direction(scanner, style, direction, tf):
    await profile(scanner, style=style, direction=direction)
    await profile(scanner, coins=(ETH,), style=style, direction=direction, status="PAUSED")
    await bar(scanner, inst=ETH, tf=tf, event="other-coin")
    await bar(scanner, tf="5m", event="wrong-style")
    assert scanner.calls == []
    await bar(scanner, tf=tf)
    expected = [preset_by_slug(s) for s in preset_ids_for(style, direction) if tf in preset_by_slug(s).universe.timeframes]
    assert len(scanner.calls) == len(expected) > 0
    assert {c["instrument_id"] for c in scanner.calls} == {BTC}
    assert {c["timeframe"] for c in scanner.calls} == {tf}
    assert {c["risk_config"].direction for c in scanner.calls} == {p.risk.direction for p in expected}


async def test_scanner_perps_dry_run_payload_uses_underlying_spot(scanner):
    mid = await profile(scanner, coins=("BTCUSDT-PERP.BINANCE",), direction="SHORT", dry_run=True)
    await bar(scanner)
    assert scanner.calls and all(c["instrument_id"] == BTC for c in scanner.calls)
    queued = scanner.worker.redis.xadd.call_args_list
    assert len(queued) == 3
    payload = json.loads(queued[0].args[1]["candidate_payload"])
    assert payload["mandate_id"] == str(mid)
    assert payload["instrument_id"] == BTC
    assert payload["metrics"]["direction"] == "SHORT"


async def test_scanner_profile_aggregate_is_once_per_bar_including_redelivery(scanner):
    mid = await profile(scanner, direction="BOTH")
    await bar(scanner)
    await bar(scanner, event="same-bar-another-event")
    rows = await scanner.pool.fetch("SELECT detail FROM ai_profile_decisions WHERE mandate_id=$1 AND stage='SCAN'", mid)
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert detail["specs_run"] > 1
    assert detail["candidates_found"] == 6
    assert detail["enqueued"] == 6
    assert await scanner.pool.fetchval("SELECT count(*) FROM signal_candidates") == 6


@pytest.mark.parametrize("column,value", [("is_active", False), ("status", "BREAK"), ("venue", "OTHER"),
                                           ("market_type", "PERP")])
async def test_scanner_profile_skips_coins_outside_ingest_universe(scanner, column, value):
    mid = await profile(scanner)
    await scanner.pool.execute(f"UPDATE instruments SET {column}=$1 WHERE instrument_id=$2", value, BTC)
    await bar(scanner)
    assert scanner.calls == []
    detail = await scanner.pool.fetchval("SELECT detail FROM ai_profile_decisions WHERE mandate_id=$1", mid)
    assert detail is not None
    assert json.loads(detail)["skipped"]["COIN_NOT_IN_FEED"] == 1


async def seed_global(h, slug="swing-trend-pullback-1h"):
    """Activate the matching built-in global preset without changing its definition."""
    p = preset_by_slug(slug)
    universe = p.universe.model_dump(mode="json") | {"risk": p.risk.model_dump(mode="json")}
    rules = [r.model_dump(mode="json") for r in p.rules]
    sid, vid = uuid4(), uuid4()
    await h.pool.execute("INSERT INTO scan_specs (id,slug,name) VALUES ($1,$2,$2)", sid, p.slug)
    await h.pool.execute("""INSERT INTO scan_spec_versions (id,spec_id,version_num,spec_hash,universe,rules)
        VALUES ($1,$2,1,$3,$4::jsonb,$5::jsonb)""", vid, sid, compute_spec_hash(universe, rules),
        json.dumps(universe), json.dumps(rules))
    await h.pool.execute("UPDATE scan_specs SET active_version_id=$1 WHERE id=$2", vid, sid)
    return vid


async def test_scanner_profile_dedupes_global_candidate_with_mandate(scanner):
    vid = await seed_global(scanner)
    mid = await profile(scanner)
    await bar(scanner)
    payloads = [json.loads(c.args[1]["candidate_payload"]) for c in scanner.worker.redis.xadd.call_args_list]
    btc = [p for p in payloads if p["instrument_id"] == BTC]
    assert len(btc) == 3 and all(p.get("mandate_id") == str(mid) for p in btc)
    assert len({p["preset_slug"] for p in btc}) == 3
    assert await scanner.pool.fetchval("SELECT count(*) FROM signal_candidates WHERE instrument_id=$1 AND bar_close_inclusive_ns=$2", BTC, END - 1) == 3
    assert await scanner.pool.fetchval("SELECT is_archived FROM scan_specs WHERE active_version_id=$1", vid) is False
    assert any(c["instrument_id"] == ETH for c in scanner.calls)  # Global universe is unchanged.


async def test_scanner_profiles_cache_active_rows_for_30_seconds(scanner, monkeypatch):
    clock = [0.]
    monkeypatch.setattr(worker_mod.time, "monotonic", lambda: clock[0])
    mid = await profile(scanner, status="PAUSED")
    await bar(scanner)
    await scanner.pool.execute("UPDATE ai_mandates SET status='ACTIVE' WHERE id=$1", mid)
    clock[0] = 29.
    await bar(scanner, event="cached")
    assert scanner.calls == []
    clock[0] = 30.
    await bar(scanner, event="refreshed")
    assert scanner.calls


async def test_scanner_profile_adopts_an_existing_global_candidate(scanner):
    """A restart or global priming scan cannot leave the profile's matching bar untagged."""
    vid = await seed_global(scanner)
    await bar(scanner, event="global-first")
    mid = await profile(scanner)
    scanner.worker._profiles_cached_at = None
    await bar(scanner, event="profile-after-global")
    rows = await scanner.pool.fetch("""SELECT payload FROM outbox_events
        WHERE event_type='signal_candidate' AND payload->>'instrument_id'=$1
        AND payload->>'bar_close_inclusive_ns'=$2 AND payload->>'spec_version_id'=$3""", BTC, str(END - 1), str(vid))
    assert len(rows) == 1
    assert "mandate_id" not in json.loads(rows[0]["payload"])
    stored = await scanner.pool.fetchval("""SELECT fields FROM scanner_debate_outbox
        WHERE fields->'candidate_payload'->>'instrument_id'=$1
          AND fields->'candidate_payload'->>'spec_version_id'=$2""", BTC, str(vid))
    assert "mandate_id" not in json.loads(stored)["candidate_payload"]
    already_sent = [json.loads(c.args[1]["candidate_payload"])
                    for c in scanner.worker.redis.xadd.call_args_list
                    if json.loads(c.args[1]["candidate_payload"])["spec_version_id"] == str(vid)
                    and json.loads(c.args[1]["candidate_payload"])["instrument_id"] == BTC]
    assert await scanner.pool.fetchval("SELECT count(*) FROM ai_profile_decisions WHERE mandate_id=$1 AND stage='SCAN'", mid) == 1
    assert len(already_sent) == 1
    assert "mandate_id" not in already_sent[0]
    await scanner.pool.execute("INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('BTC',$1)", mid)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/debate_worker"))
    routing = importlib.import_module("mandate_routing")
    async with scanner.pool.acquire() as conn:
        resolved = await routing.resolve_profile(conn, already_sent[0]["instrument_id"], already_sent[0].get("mandate_id"))
        assert resolved.id == mid
        reason, instrument = await routing.entry_profile_route(conn, resolved, BTC, "BUY")
        assert reason is None and instrument == BTC
    assert await scanner.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0
    assert await scanner.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_scanner_profile_does_not_suppress_a_different_global_preset(scanner):
    """A profile match only replaces the global match for that same preset slug."""
    vid = await seed_global(scanner, "rsi-oversold-reversal")
    await profile(scanner)
    await bar(scanner)
    current = [json.loads(c.args[1]["candidate_payload"])
               for c in scanner.worker.redis.xadd.call_args_list
               if json.loads(c.args[1]["candidate_payload"])["instrument_id"] == BTC]
    assert len(current) == 4
    assert sum(p["spec_version_id"] == str(vid) for p in current) == 1
    assert sum("mandate_id" in p for p in current) == 3


async def test_scanner_profile_enqueued_counts_durable_scheduling_after_relay_recovery(scanner):
    """Durably enqueued counts remain accurate when a failed publication later recovers."""
    mid = await profile(scanner)
    scanner.worker.redis.xadd.side_effect = RuntimeError("queue unavailable")
    await bar(scanner)
    detail = json.loads(await scanner.pool.fetchval(
        "SELECT detail FROM ai_profile_decisions WHERE mandate_id=$1", mid))
    assert detail["candidates_found"] == 3
    assert detail["enqueued"] == 3
    assert detail["published_initial"] == 0
    assert await scanner.pool.fetchval("SELECT count(*) FROM scanner_debate_outbox WHERE status='PENDING'") == 3
    scanner.worker.redis.xadd.side_effect = None
    await scanner.pool.execute("UPDATE scanner_debate_outbox SET next_attempt_at=now()")
    assert await scanner.worker.relay_debate_outbox_once() == 3
    after = json.loads(await scanner.pool.fetchval("SELECT detail FROM ai_profile_decisions WHERE mandate_id=$1", mid))
    assert after == detail
    assert await scanner.pool.fetchval("SELECT count(*) FROM scanner_debate_outbox WHERE status='PUBLISHED'") == 3


async def test_scanner_profile_concurrent_duplicate_bars_write_one_aggregate(scanner):
    """Competing handlers serialize each logical profile/bar decision and candidate."""
    mid = await profile(scanner)
    await asyncio.gather(bar(scanner), bar(scanner))
    assert await scanner.pool.fetchval("SELECT count(*) FROM ai_profile_decisions WHERE mandate_id=$1 AND stage='SCAN'", mid) == 1
    assert await scanner.pool.fetchval("SELECT count(*) FROM signal_candidates") == 3
    assert scanner.worker.redis.xadd.call_count == 3


async def test_scanner_active_cache_after_pause_is_refused_by_fresh_routing(scanner, monkeypatch):
    """The 30-second scanner cache can emit after pause; entry routing rereads and refuses it."""
    mid = await profile(scanner)
    await bar(scanner)
    scanner.calls.clear()
    await scanner.pool.execute("UPDATE ai_mandates SET status='PAUSED' WHERE id=$1", mid)
    await scanner.pool.execute("INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('BTC',$1)", mid)
    await bar(scanner, event="cached-after-pause")
    assert scanner.calls
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/debate_worker"))
    routing = importlib.import_module("mandate_routing")
    async with scanner.pool.acquire() as conn:
        current = await routing.resolve_profile(conn, BTC, str(mid))
        reason, _ = await routing.entry_profile_route(conn, current, BTC, "BUY")
        assert reason == "PROFILE_NOT_RUNNING"
    assert await scanner.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0
    assert await scanner.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_scanner_profile_tags_pending_global_before_relay(scanner):
    """An unpublished global duplicate is tagged before its first successful Redis publication."""
    vid = await seed_global(scanner)
    scanner.worker.redis.xadd.side_effect = RuntimeError("temporary outage")
    await bar(scanner, event="global-pending")
    mid = await profile(scanner)
    scanner.worker._profiles_cached_at = None
    scanner.worker.redis.xadd.side_effect = None
    scanner.worker.redis.xadd.reset_mock()
    await bar(scanner, event="profile-adopts-pending")
    await scanner.pool.execute("UPDATE scanner_debate_outbox SET next_attempt_at=now()")
    await scanner.worker.relay_debate_outbox_once()
    published = [json.loads(c.args[1]["candidate_payload"]) for c in scanner.worker.redis.xadd.call_args_list
                 if json.loads(c.args[1]["candidate_payload"])["instrument_id"] == BTC
                 and json.loads(c.args[1]["candidate_payload"])["spec_version_id"] == str(vid)]
    assert len(published) == 1 and published[0]["mandate_id"] == str(mid)


async def test_scanner_relay_reloads_fields_claimed_before_profile_adoption(scanner):
    """A relay's older claim snapshot must not publish stale routing context after adoption."""
    vid = await seed_global(scanner)
    scanner.worker.redis.xadd.side_effect = RuntimeError("temporary outage")
    await bar(scanner, event="global-before-relay-claim")
    old = await scanner.pool.fetchrow("""SELECT candidate_id, fields FROM scanner_debate_outbox
        WHERE fields->'candidate_payload'->>'instrument_id'=$1
          AND fields->'candidate_payload'->>'spec_version_id'=$2""", BTC, str(vid))
    claimed_fields = json.loads(old["fields"])
    mid = await profile(scanner)
    scanner.worker._profiles_cached_at = None
    scanner.worker.redis.xadd.side_effect = None
    await bar(scanner, event="profile-between-claim-and-publish")
    scanner.worker.redis.xadd.reset_mock()
    await scanner.worker._publish_outbox_row(old["candidate_id"], claimed_fields, "", attempt_count=2)
    published = json.loads(scanner.worker.redis.xadd.call_args.args[1]["candidate_payload"])
    assert published["mandate_id"] == str(mid)
