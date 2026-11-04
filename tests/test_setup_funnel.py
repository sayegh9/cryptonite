"""H13c: every setup's stopping point is visible, its what-if scored, and a funnel that silently stops raises an alarm."""

import datetime
import json
import re
import sys
import uuid
from pathlib import Path

import polars as pl
import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_schemas import funnel as stages
from cryptonite_schemas.funnel import ROWS_SQL, funnel_counts, funnel_row, furthest_stage, what_if
from migrated_db import migrated_database
from test_notifier import make_notifier

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "debate_worker"))
import funnel  # noqa: E402
import proposals  # noqa: E402
from funnel import FunnelSnapshot, funnel_findings  # noqa: E402

NOW = datetime.datetime(2026, 11, 4, 12, 0, tzinfo=datetime.UTC)
MIN = datetime.timedelta(minutes=1)
BTC, WLD = "BTCUSDT.BINANCE", "WLDUSDT.BINANCE"
URL = "/api/v1/funnel"


def raw(age_min=30, instrument=BTC, **over):
    row = {"candidate_id": uuid.uuid4(), "created_at": NOW - age_min * MIN, "instrument_id": instrument, "timeframe": "1h",
           "preset": "trend-pullback", "entry": 100, "stop": 95, "target": 110, "outbox_status": "PUBLISHED"}
    return {**row, **over}


def debate(status="NO_TRADE", rec="HOLD", **over):
    return {"debate_status": status, "recommendation": rec, "trigger_source": "scanner", "debate_at": NOW - 20 * MIN, **over}


def checks(snapshot, now=NOW):
    return [f["check"] for f in funnel_findings(snapshot, now)]


def quiet(**over):
    return FunnelSnapshot(**{"last_committed_scan_at": NOW - MIN, **over})


# ------------------------------------------------------------------------------------ furthest stage


@pytest.mark.parametrize(("row", "stage", "reason"), [
    (raw(outbox_status=None), stages.NOT_QUEUED, "BAR_NOT_FRESH"),
    (raw(outbox_status="EXPIRED", outbox_error="too late"), stages.NOT_QUEUED, "too late"),
    (raw(outbox_status="FAILED"), stages.NOT_QUEUED, "OUTBOX_FAILED"),
    (raw(**debate("GATE_REFUSED", None, failure_reason="COOLDOWN")), stages.GATE_REFUSED, "COOLDOWN"),
    (raw(**debate("FAILED", None, failure_reason="WORKER_LOST")), stages.DEBATE_FAILED, "WORKER_LOST"),
    (raw(**debate("NO_PRICE", None)), stages.DEBATE_FAILED, "NO_PRICE"),
    (raw(**debate(stop_stage="PROPOSAL_SKIPPED", stop_reason="NO_PROFILE")), stages.PROPOSAL_SKIPPED, "NO_PROFILE"),
    (raw(**debate("MANDATE_REFUSED", "BUY", policy_b_reason="ROUTE_X")), stages.VERDICT_NOT_ROUTED, "ROUTE_X"),
    (raw(**debate("PENDING_APPROVAL", "BUY")), stages.ROUTED, "PENDING_APPROVAL"),
    (raw(**debate("COMPLETED", "SELL")), stages.ROUTED, "COMPLETED"),
    (raw(**debate(), proposal_status="PENDING", proposal_expires_at=NOW + MIN), stages.PROPOSED, None),
    (raw(**debate(), proposal_status="PENDING", proposal_expires_at=NOW - MIN), stages.EXPIRED, None),
    (raw(**debate(), proposal_status="APPROVED"), stages.APPROVED, None),
    (raw(**debate(), proposal_status="REJECTED", proposal_refusal="SLIP"), stages.REJECTED, "SLIP"),
    (raw(**debate(), proposal_status="EXPIRED"), stages.EXPIRED, None),
    # A proposal outranks a stop written by an earlier delivery
    (raw(**debate(stop_stage="PROPOSAL_SKIPPED", stop_reason="X"), proposal_status="APPROVED"), stages.APPROVED, None),
    # Nothing final: unaccounted after 10 minutes, never hidden
    (raw(), stages.UNACCOUNTED, "QUEUED_NO_DEBATE"),
    (raw(outbox_status="PENDING"), stages.UNACCOUNTED, "QUEUED_NO_DEBATE"),
    (raw(**debate("RUNNING", None)), stages.UNACCOUNTED, "DEBATE_STILL_RUNNING"),
    (raw(**debate()), stages.UNACCOUNTED, "HOLD_WITHOUT_RECORD"),
    (raw(age_min=9), stages.IN_PROGRESS, "QUEUED_NO_DEBATE"),
    (raw(age_min=9, **debate("RUNNING", None)), stages.IN_PROGRESS, "DEBATE_STILL_RUNNING"),
])
def test_furthest_stage(row, stage, reason):
    assert furthest_stage(row, NOW) == (stage, reason)


def test_what_if_is_final_then_provisional_then_pending():
    assert what_if({"net_r_live": 1.5, "gross_r": 9}) == (1.5, stages.FINAL, None)
    assert what_if({"gross_r": 0.5, "provisional_state": "OPEN"}) == (0.5, stages.PROVISIONAL, "OPEN")
    assert what_if({"provisional_state": "INVALID", "gross_r": None}) == (None, stages.PENDING, None)
    assert what_if({}) == (None, stages.PENDING, None)


# ------------------------------------------------------------------------------------ alarms, each firing and not


def test_a_quiet_healthy_funnel_finds_nothing():
    assert checks(quiet(rows=[raw(age_min=5), raw(outbox_status=None)], moves={"BTC": 0.4})) == []


def test_unaccounted():
    assert "UNACCOUNTED" in checks(quiet(rows=[raw(age_min=11)]))
    assert "UNACCOUNTED" not in checks(quiet(rows=[raw(age_min=9), raw(**debate("GATE_REFUSED", None))]))


def test_proposal_missing_is_the_hold_on_a_setup_policy_a_takes_with_no_record():
    missing = raw(**debate(policy_a="TAKE"))
    found = funnel_findings(quiet(rows=[missing]), NOW)
    assert [f["check"] for f in found] == ["PROPOSAL_MISSING"]  # reported once, not again as unaccounted
    for row in (raw(**debate(policy_a="SKIP", stop_stage="PROPOSAL_SKIPPED", stop_reason="POLICY_A_X")),
                raw(**debate(policy_a="TAKE", stop_stage="PROPOSAL_SKIPPED", stop_reason="NO_PROFILE")),
                raw(**debate(policy_a="TAKE"), proposal_status="PENDING", proposal_expires_at=NOW + MIN),
                raw(**{**debate(policy_a="TAKE"), "debate_at": NOW - 9 * MIN}),
                raw(**debate("COMPLETED", "BUY", policy_a="TAKE"))):
        assert "PROPOSAL_MISSING" not in checks(quiet(rows=[row]))


def test_scanner_silent():
    assert checks(FunnelSnapshot(last_committed_scan_at=NOW - 16 * MIN)) == ["SCANNER_SILENT"]
    assert checks(FunnelSnapshot(last_committed_scan_at=None)) == ["SCANNER_SILENT"]
    assert checks(FunnelSnapshot(last_committed_scan_at=NOW - 14 * MIN)) == []


def test_debates_failing():
    bad, ok = {"status": "FAILED", "failure_reason": None}, {"status": "NO_TRADE", "failure_reason": None}
    reason = {"status": "NO_TRADE", "failure_reason": "EVIDENCE_INCOMPLETE"}
    assert checks(quiet(recent_debates=[bad, {"status": "NO_PRICE", "failure_reason": "NO_PRICE"}, reason, ok, ok])) == [
        "DEBATES_FAILING"]
    assert checks(quiet(recent_debates=[bad, reason, ok, ok, ok])) == []
    assert checks(quiet(recent_debates=[ok, ok, ok, ok, ok, bad, bad, bad])) == []  # only the last five


def test_move_no_proposals_the_11_04_case_with_refused_candidates():
    rows = [raw(instrument=WLD, **debate("GATE_REFUSED", None, failure_reason="COOLDOWN")) for _ in range(17)]
    found = funnel_findings(quiet(rows=rows, moves={"WLD": 6.21, "BTC": 0.5}, evaluations={"WLD": {"NO_TRIGGER": 400}}), NOW)
    assert [f["check"] for f in found] == ["MOVE_NO_PROPOSALS"]
    assert found[0]["coins"] == [{"coin": "WLD", "move_pct": 6.21, "candidates": 17,
                                  "reasons": {"GATE_REFUSED: COOLDOWN": 17}}]
    assert re.search(r"WLD moved \+6\.2%.*0 proposals.*17 candidates.*COOLDOWN x17.*NO_TRIGGER 400", found[0]["message"])


def test_move_no_proposals_fires_when_the_scanner_found_nothing_and_on_a_fall():
    found = funnel_findings(quiet(moves={"ETH": -3.0}), NOW)
    assert [f["check"] for f in found] == ["MOVE_NO_PROPOSALS"]
    assert "the scanner found nothing" in found[0]["message"] and found[0]["coins"][0]["candidates"] == 0


def test_move_no_proposals_quiet_market_or_an_answer_does_not_fire():
    assert checks(quiet(moves={"BTC": 2.99, "ETH": -1.0})) == []
    for row in (raw(**debate(), proposal_status="EXPIRED"), raw(**debate("COMPLETED", "BUY")),
                raw(**debate("PENDING_APPROVAL", "BUY"))):
        assert "MOVE_NO_PROPOSALS" not in checks(quiet(rows=[row], moves={"BTC": 8.0}))
    # A card on another coin answers nothing for this one
    assert "MOVE_NO_PROPOSALS" in checks(quiet(rows=[raw(instrument=WLD, **debate(), proposal_status="EXPIRED")],
                                               moves={"BTC": 8.0}))


def test_all_expired():
    expired = [raw(**debate(), proposal_status="EXPIRED") for _ in range(5)]
    assert checks(quiet(rows=expired)) == ["ALL_EXPIRED"]
    assert checks(quiet(rows=expired[:4])) == []
    assert checks(quiet(rows=[*expired, raw(**debate(), proposal_status="REJECTED")])) == []
    assert checks(quiet(rows=[*expired, raw(**debate(), proposal_status="APPROVED")])) == []


# ------------------------------------------------------------------------------------ publishing


class FakeRedis:
    def __init__(self, subscribers=1):
        self.kv, self.published, self.subscribers = {}, [], subscribers

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = (value, ex)
        return True

    async def delete(self, *keys):
        for k in keys:
            self.kv.pop(k, None)

    async def publish(self, channel, data):
        self.published.append((channel, json.loads(data)))
        return self.subscribers


async def test_an_alarm_is_critical_keyed_by_its_check_and_deduped_for_six_hours():
    r = FakeRedis()
    findings = funnel_findings(FunnelSnapshot(last_committed_scan_at=None), NOW)
    assert await funnel.publish_findings(r, findings) == 1
    assert await funnel.publish_findings(r, findings) == 0
    assert len(r.published) == 1
    channel, alert = r.published[0]
    assert channel == "channel:system_alerts"
    assert alert["kind"] == "FUNNEL_ALARM" and alert["severity"] == "CRITICAL"
    assert alert["check"] == alert["action"] == "SCANNER_SILENT" and alert["message"]
    assert r.kv["funnel:alarm:SCANNER_SILENT"][1] == 6 * 3600
    # another check has its own window
    other = [{"check": "ALL_EXPIRED", "message": "m"}]
    assert await funnel.publish_findings(r, other) == 1


async def test_an_alarm_nobody_received_is_tried_again_next_pass():
    r = FakeRedis(subscribers=0)
    finding = [{"check": "UNACCOUNTED", "message": "m"}]
    assert await funnel.publish_findings(r, finding) == 0
    r.subscribers = 1
    assert await funnel.publish_findings(r, finding) == 1


async def test_the_daily_summary_waits_for_00_05_and_goes_once_a_day():
    r = FakeRedis()
    rows = [raw(**debate(), proposal_status="EXPIRED", net_r_live=2.0),
            raw(**debate(stop_stage="PROPOSAL_SKIPPED", stop_reason="X"), gross_r=-1.0, provisional_state="OPEN"),
            raw(**debate("GATE_REFUSED", None)), raw(**debate(), proposal_status="APPROVED", net_r_live=0.5)]
    snapshot = quiet(rows=rows)
    assert await funnel.publish_daily(r, snapshot, NOW.replace(hour=0, minute=4)) is False
    assert await funnel.publish_daily(r, snapshot, NOW.replace(hour=0, minute=5)) is True
    assert await funnel.publish_daily(r, snapshot, NOW.replace(hour=9)) is False
    assert len(r.published) == 1
    alert = r.published[0][1]
    assert alert["kind"] == "FUNNEL_DAILY" and alert["date"] == "2026-11-04"
    text = alert["message"]
    assert "4 setups, 3 passed the gate, 1 refused" in text and "HOLD 3" in text
    assert "not taken +0.50R (n=2), approved +0.50R (n=1)" in text
    assert "Best missed: BTC LONG 1h +2.00R. Worst missed: BTC LONG 1h -1.00R." in text


async def test_the_notifier_forwards_the_daily_once_at_action_tier_and_the_alarm_as_critical():
    n, tg, _, ntfy = make_notifier()
    daily = {"kind": "FUNNEL_DAILY", "severity": "ACTION", "date": "2026-11-04", "message": "Last 24h: 4 setups"}
    assert await n.on_system_alert(json.dumps(daily)) is True
    assert await n.on_system_alert(json.dumps(daily)) is False
    assert len(ntfy.posts) == 1 and ntfy.posts[-1]["priority"] == 3
    sent = tg.sent()[-1]["text"]
    assert "ACTION" in sent and "CRITICAL" not in sent and "4 setups" in sent

    alarm = {"kind": "FUNNEL_ALARM", "severity": "CRITICAL", "check": "PROPOSAL_MISSING", "action": "PROPOSAL_MISSING",
             "message": "2 scanner debates said HOLD"}
    assert await n.on_system_alert(json.dumps(alarm)) is True
    assert await n.on_system_alert(json.dumps(alarm)) is False  # the notifier's key
    assert await n.on_system_alert(json.dumps({**alarm, "action": "ALL_EXPIRED"})) is True  # keyed by the check
    assert ntfy.posts[-1]["priority"] == 5
    assert "CRITICAL" in tg.sent()[-1]["text"] and "2 scanner debates said HOLD" in tg.sent()[-1]["text"]


# ------------------------------------------------------------------------------------ database


class FakeLake:
    """1m and 1h bars from a function of the timestamp; raises for the symbols in ``broken``."""

    def __init__(self, close_of=lambda ts: 100.0, broken=()):
        self.close_of, self.broken, self.calls = close_of, set(broken), []

    def query_bars_df(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
        self.calls.append((symbol, timeframe))
        if symbol in self.broken or "*" in self.broken:
            raise OSError("parquet is corrupt")
        step = (60 if timeframe == "1m" else 3600) * 10**9
        first = start_ns - start_ns % step + step
        stamps = list(range(first, end_ns + 1, step))
        closes = [self.close_of(ts) for ts in stamps]
        return pl.DataFrame({"ts_event_ns": stamps, "open": closes, "high": closes, "low": closes, "close": closes},
                            schema={"ts_event_ns": pl.Int64, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
                                    "close": pl.Float64})


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_h13c") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("TRUNCATE scan_specs, scan_eval_counts, debate_runs, ai_approvals, execution_commands CASCADE")
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    try:
        yield harness
    finally:
        await close_harness(harness)


async def scan_context(h, trigger="COMMITTED_BAR_EVENT"):
    spec = await h.pool.fetchval(
        "INSERT INTO scan_specs (id, slug, name) VALUES ($1, $2, 'n') RETURNING id", uuid.uuid4(), f"spec-{uuid.uuid4().hex[:6]}")
    version = await h.pool.fetchval(
        """INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
           VALUES ($1, $2, 1, 'h', '[]'::jsonb, '{}'::jsonb) RETURNING id""", uuid.uuid4(), spec)
    run = await h.pool.fetchval(
        """INSERT INTO scan_runs (id, spec_version_id, spec_hash, scanner_version, indicator_version, input_watermark_ns,
               source_partitions, source_partition_checksums, trigger_reason, status, started_at)
           VALUES ($1, $2, 'h', 'v', 'v', 0, '[]'::jsonb, '[]'::jsonb, $3, 'COMPLETED', now()) RETURNING id""",
        uuid.uuid4(), version, trigger)
    return run, version


async def candidate(h, run, version, instrument=BTC, *, age=datetime.timedelta(minutes=30), entry=100, stop=95, target=110,
                    timeframe="5m"):
    cid = uuid.uuid4()
    created = datetime.datetime.now(datetime.UTC) - age
    end_ns = int(created.timestamp() * 1e9)
    await h.pool.execute(
        """INSERT INTO signal_candidates (id, scan_run_id, spec_version_id, instrument_id, timeframe, bar_open_ns,
               bar_close_inclusive_ns, interval_end_exclusive_ns, matched_rules, metrics, suggested_entry, suggested_stop,
               suggested_target, created_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, '[]'::jsonb, '{}'::jsonb, $9, $10, $11, $12)""",
        cid, run, version, instrument, timeframe, end_ns - 10**9, end_ns - 1, end_ns, entry, stop, target, created)
    return cid


async def fetch_funnel(h, **params):
    async with h.client() as c:
        return await c.get(URL, params=params)


async def test_a_skip_reason_is_recorded_inside_the_savepoint_and_a_failed_write_cannot_reach_the_debate(h):
    run, version = await scan_context(h)
    cid = await candidate(h, run, version)
    hold = proposals.HoldDebate(uuid.uuid4(), cid, BTC, "scanner", "HOLD", "NO_TRADE", 0.5, "r", None, None, 95.0, 110.0,
                                {"suggested_entry": 100}, [])
    async with h.pool.acquire() as conn:
        assert await proposals.create_for_hold(conn, hold, None, h.redis) is None
    row = await h.pool.fetchrow("SELECT stage, reason, detail FROM candidate_funnel_stops WHERE candidate_id = $1", cid)
    assert (row["stage"], row["reason"]) == ("PROPOSAL_SKIPPED", "NO_PROFILE")
    assert json.loads(row["detail"]) == {"run_id": str(hold.run_id)}

    gone = proposals.HoldDebate(uuid.uuid4(), uuid.uuid4(), BTC, "scanner", "HOLD", "NO_TRADE", 0.5, "r", None, None, 95.0,
                                110.0, None, [])
    async with h.pool.acquire() as conn, conn.transaction():
        assert await proposals.create_for_hold(conn, gone, None, h.redis) is None  # the FK fails, in its savepoint
        assert await conn.fetchval("SELECT 1") == 1  # the debate's transaction is still alive


async def test_the_api_reads_every_stage_excludes_priming_and_runs_on_the_api_role(h):
    run, version = await scan_context(h)
    priming, primed_version = await scan_context(h, "HISTORICAL_PRIMING")
    await candidate(h, priming, primed_version)
    not_fresh = await candidate(h, run, version)
    refused = await candidate(h, run, version, "ETHUSDT.BINANCE")
    await h.pool.execute(
        """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, failure_reason, candidate_id, stream_message_id)
           VALUES ($1, 'ETHUSDT.BINANCE', 'GATE_REFUSED', 'scanner', 'COOLDOWN', $2, $3)""",
        uuid.uuid4(), refused, f"m-{uuid.uuid4()}")
    await h.pool.execute("INSERT INTO policy_decisions (candidate_id, policy, decision, reason) VALUES ($1, 'A', 'TAKE', 'GRADE_B')", refused)
    labelled = await candidate(h, run, version, WLD)
    await h.pool.execute(
        """INSERT INTO candidate_outcomes (candidate_id, direction, entry, stop, target, outcome, max_hold_hours, net_r_live)
           VALUES ($1, 'LONG', 100, 95, 110, 'TARGET', 12, 1.25)""", labelled)
    queued = await candidate(h, run, version)
    await h.pool.execute("INSERT INTO scanner_debate_outbox (candidate_id, fields, status) VALUES ($1, '{}'::jsonb, 'PUBLISHED')", queued)
    provisional = await candidate(h, run, version, "SOLUSDT.BINANCE", entry=100, stop=105, target=90)
    await h.pool.execute("INSERT INTO candidate_provisional_outcomes (candidate_id, gross_r, state) VALUES ($1, -0.5, 'OPEN')", provisional)
    await h.pool.execute(
        """INSERT INTO scan_eval_counts (bucket_start, instrument_id, timeframe, preset_slug, side, outcome, n)
           VALUES (now() - interval '1 hour', $1, '1h', 'p', 'LONG', 'NO_TRIGGER', 40),
                  (now() - interval '1 hour', $1, '1h', 'p', 'SHORT', 'REJECTED_RR', 2),
                  (now() - interval '30 hours', $1, '1h', 'p', 'LONG', 'NO_TRIGGER', 999)""", BTC)

    resp = await fetch_funnel(h)
    assert resp.status_code == 200
    body = resp.json()
    by_id = {r["candidate_id"]: r for r in body["rows"]}
    assert len(by_id) == 5  # the priming candidate is nowhere
    assert body["counts"]["candidates"] == 5
    assert body["counts"]["evaluations"] == {"NO_TRIGGER": 40, "REJECTED_RR": 2, "STALE": 0, "MATCHED_FRESH": 0}
    assert body["counts"]["gate_refused"] == 1 and body["counts"]["gate_passed"] == 0 and body["counts"]["unaccounted"] == 1
    assert by_id[str(not_fresh)]["stage"] == "NOT_QUEUED" and by_id[str(not_fresh)]["reason"] == "BAR_NOT_FRESH"
    assert by_id[str(queued)]["stage"] == "UNACCOUNTED"
    gate_row = by_id[str(refused)]
    assert gate_row["stage"] == "GATE_REFUSED" and gate_row["reason"] == "COOLDOWN" and gate_row["policy_a"] == "TAKE"
    assert gate_row["coin"] == "ETH" and gate_row["preset"].startswith("spec-") and gate_row["direction"] == "LONG"
    assert (by_id[str(labelled)]["what_if_r"], by_id[str(labelled)]["what_if_state"]) == (1.25, "FINAL")
    row = by_id[str(provisional)]
    assert (row["what_if_r"], row["what_if_state"], row["what_if_exit"], row["direction"]) == (-0.5, "PROVISIONAL", "OPEN", "SHORT")
    assert by_id[str(not_fresh)]["what_if_state"] == "PENDING" and by_id[str(not_fresh)]["what_if_r"] is None
    text = " ".join(body["caveats"])
    for word in ("model figures", "today", "before later costs", "overlap"):
        assert word in text

    assert (await fetch_funnel(h, hours=0)).status_code == 422
    assert (await fetch_funnel(h, hours=169)).status_code == 422
    assert (await fetch_funnel(h, hours=168)).json()["counts"]["evaluations"]["NO_TRIGGER"] == 1039
    async with h.client() as c:
        c.cookies.clear()
        assert (await c.get(URL)).status_code in (401, 403)

    # The read model and the learner's writes run on the role debate_worker and the API connect as
    async with h.pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        await conn.execute("SET LOCAL ROLE cryptonite_api")
        assert len(await conn.fetch(ROWS_SQL, datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=24))) == 5
        await conn.execute("INSERT INTO candidate_funnel_stops (candidate_id, stage, reason) VALUES ($1, 'S', 'r')", queued)
        await conn.execute("""INSERT INTO candidate_provisional_outcomes (candidate_id, gross_r, state) VALUES ($1, 1, 'OPEN')
                              ON CONFLICT (candidate_id) DO UPDATE SET gross_r = 1""", provisional)
        await conn.execute("SELECT status, instruments FROM ai_mandates")
        await tx.rollback()


async def test_provisional_scores_an_open_setup_and_never_a_labelled_one(h):
    run, version = await scan_context(h)
    now = datetime.datetime.now(datetime.UTC)
    open_setup = await candidate(h, run, version)
    stopped = await candidate(h, run, version, WLD, age=datetime.timedelta(hours=2))
    labelled = await candidate(h, run, version)
    await h.pool.execute(
        """INSERT INTO candidate_outcomes (candidate_id, direction, entry, stop, target, outcome, max_hold_hours, net_r_live)
           VALUES ($1, 'LONG', 100, 95, 110, 'TARGET', 12, 1.25)""", labelled)
    primed_run, primed_version = await scan_context(h, "HISTORICAL_PRIMING")
    primed = await candidate(h, primed_run, primed_version)
    stop_after = int((now - datetime.timedelta(hours=1)).timestamp() * 1e9)

    class Lake(FakeLake):
        def query_bars_df(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
            self.close_of = (lambda ts: 90.0 if ts > stop_after else 100.0) if symbol == "WLDUSDT" else (lambda ts: 102.0)
            return super().query_bars_df(symbol, timeframe, start_ns, end_ns)

    assert await funnel.update_provisional(h.pool, Lake(), now=now) == 2
    rows = {r["candidate_id"]: r for r in await h.pool.fetch("SELECT * FROM candidate_provisional_outcomes")}
    assert labelled not in rows and primed not in rows
    assert rows[open_setup]["state"] == "OPEN" and rows[open_setup]["gross_r"] == 0
    assert rows[stopped]["state"] != "OPEN" and rows[stopped]["gross_r"] < 0
    # A figure that reached its outcome is not read again; the open one is
    lake = Lake()
    assert await funnel.update_provisional(h.pool, lake, now=now) == 1
    assert [c[0] for c in lake.calls] == ["BTCUSDT"]


async def test_the_batch_is_capped(h):
    run, version = await scan_context(h)
    for _ in range(3):
        await candidate(h, run, version)
    assert await funnel.update_provisional(h.pool, FakeLake(), limit=2) == 2


async def test_a_lake_failure_leaves_the_learner_pass_and_other_checks_standing(h):
    run, version = await scan_context(h)
    await candidate(h, run, version)
    await h.mandate(instruments=(BTC,))
    r = FakeRedis()
    out = await funnel.run_pass(h.pool, FakeLake(broken=["*"]), r)
    assert out["provisional"] == 0
    assert "UNACCOUNTED" not in [a["check"] for _, a in r.published if a["kind"] == "FUNNEL_ALARM"]  # no outbox: not fresh
    assert await h.pool.fetchval("SELECT count(*) FROM candidate_provisional_outcomes") == 0

    await h.pool.execute("UPDATE scan_runs SET started_at = now() - interval '1 hour'")
    r = FakeRedis()
    await funnel.run_pass(h.pool, FakeLake(broken=["*"]), r)
    assert "SCANNER_SILENT" in [a["check"] for _, a in r.published if a["kind"] == "FUNNEL_ALARM"]


async def test_a_database_failure_does_not_raise_either():
    class NoPool:
        def acquire(self):
            raise OSError("no database")

    assert await funnel.run_pass(NoPool(), FakeLake(), FakeRedis()) == {"provisional": 0, "alarms": 0}


async def test_the_pass_measures_a_move_from_1h_bars_of_a_running_profiles_coin_when_the_scanner_found_nothing(h):
    await scan_context(h)
    await h.mandate(instruments=(BTC,))
    now = datetime.datetime.now(datetime.UTC)
    day_ago = int((now - datetime.timedelta(hours=24)).timestamp() * 1e9)
    lake = FakeLake(close_of=lambda ts: 100.0 if ts <= day_ago else 106.0)
    r = FakeRedis()
    await funnel.run_pass(h.pool, lake, r, now=now)
    moved = [a for _, a in r.published if a.get("check") == "MOVE_NO_PROPOSALS"]
    assert len(moved) == 1 and "BTC moved +6.0%" in moved[0]["message"] and "the scanner found nothing" in moved[0]["message"]
    assert ("BTCUSDT", "1h") in lake.calls


def test_a_stale_or_gapped_series_is_no_measured_move():
    assert funnel.coin_move_pct(FakeLake(), "BTCUSDT", NOW) == 0.0
    empty = FakeLake()
    empty.query_bars_df = lambda *a, **k: pl.DataFrame()
    assert funnel.coin_move_pct(empty, "BTCUSDT", NOW) is None
    now_ns = int(NOW.timestamp() * 1e9)
    stale = FakeLake()
    real = stale.query_bars_df
    stale.query_bars_df = lambda s, tf, a, b: real(s, tf, a, now_ns - 3 * 3600 * 10**9)
    assert funnel.coin_move_pct(stale, "BTCUSDT", NOW) is None
    short = FakeLake()
    real_short = short.query_bars_df
    short.query_bars_df = lambda s, tf, a, b: real_short(s, tf, now_ns - 10 * 3600 * 10**9, b)
    assert funnel.coin_move_pct(short, "BTCUSDT", NOW) is None


def test_counts_are_over_the_views():
    views = [funnel_row(r, NOW) for r in (raw(**debate("GATE_REFUSED", None)), raw(**debate(), proposal_status="EXPIRED"))]
    c = funnel_counts(views)
    assert c["gate_refused"] == 1 and c["gate_passed"] == 1 and c["proposals"]["EXPIRED"] == 1 and c["verdicts"] == {"HOLD": 1}


async def test_this_step_reaches_no_order_path(h):
    """A whole pass with findings of every kind writes neither a command nor an approval, and its code names neither."""
    run, version = await scan_context(h)
    await candidate(h, run, version)
    await h.mandate(instruments=(BTC,))
    await funnel.run_pass(h.pool, FakeLake(), FakeRedis())
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0
    for path in ("services/debate_worker/funnel.py", "services/api/cryptonite_api/routes/funnel.py",
                 "packages/schemas/cryptonite_schemas/funnel.py"):
        source = (ROOT / path).read_text()
        assert "execution_commands" not in source and "ai_approvals" not in source
