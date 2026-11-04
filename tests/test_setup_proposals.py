"""H13b: a scanner HOLD on a setup policy A would take reaches the operator as a proposal; only their click executes it.

Runs on a throwaway migrated database and the Redis at REDIS_URL, as test_brain_card_preflight does. The ``test_e2e_*``
cases drive the real worker (models stubbed) against that database.
"""

import asyncio
import json
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_agents.brain.widgets import OrderCardWidget
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import EMBED, FLASH, FLASH_LITE, PRO
from cryptonite_api.brain_outcomes import _fill_tp1
from cryptonite_api.routes import proposals as proposal_routes
from cryptonite_execution import profile_lifecycle
from cryptonite_execution.autonomy_metrics import gate_metrics
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_execution.risk import OrderIntent
from cryptonite_orchestrator.mandate_gate import ensure_mandate_instance, gate_ai_order
from cryptonite_orchestrator.server import SimpleQuoteCache
from cryptonite_schemas.marks import quote_tick_key
from migrated_db import migrated_database
from test_debate_gate import FakeEmbedder, Models, load
from test_debate_perp_gate import CapRedis
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_fresh_dispatcher

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "debate_worker"))
import proposals  # noqa: E402
import reflection  # noqa: E402

pytestmark = pytest.mark.usefixtures("complete_lake_bars")

SPOT = "BTCUSDT.BINANCE"
PERP = "BTCUSDT-PERP.BINANCE"
MARK_KEY = "perp:mark:BTCUSDT"
ENTRY, STOP, TARGET = 60000.0, 58000.0, 65000.0
URL = "/api/v1/proposals"
HOLD_LITE = {"bull_case": "b", "bear_case": "r", "recommendation": "HOLD", "conviction_score": 0.6,
             "allocation_pct": 0.0, "reasoning": "mixed picture"}


@pytest.fixture(autouse=True)
def ai_triage_on(monkeypatch):
    monkeypatch.setenv("DEBATE_AI_TRIAGE", "1")


@pytest.fixture(scope="module")
def main():
    return load("debate_worker_main_h13b", "main.py")


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_h13b") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("TRUNCATE setup_proposals, ai_profile_coin_locks, ai_profile_decisions, ai_approvals, "
                               "notifications_outbox, execution_commands, paper_orders, paper_positions, debate_runs CASCADE")
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("UPDATE paper_accounts SET total_equity = 100000, cash_balance = 100000")
    profile_lifecycle.reset_switch_cache()
    await harness.redis.set(quote_tick_key(SPOT), json.dumps({
        "instrument_id": SPOT, "bid_price": "59999", "ask_price": "60001", "arrival_ns": time.time_ns()}), ex=60)
    await set_prices(harness, ENTRY)
    try:
        yield harness
    finally:
        await harness.redis.delete(MARK_KEY, "market:24h:BTCUSDT", quote_tick_key(SPOT))
        await close_harness(harness)


async def set_prices(h, price: float) -> None:
    await h.redis.set("market:24h:BTCUSDT", json.dumps({"last_price": price}), ex=60)
    await h.redis.set(MARK_KEY, json.dumps({"type": "perp.mark", "mark_price": str(price), "funding_rate": "0.0001",
                                            "ts_event_ns": 1}), ex=60)


async def profile(h, *, level=1, markets=("SPOT",), direction="LONG", max_leverage=1, status="ACTIVE", lock=True):
    perps = "PERP" in markets
    mid = await h.pool.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, status, autonomy_level,
               markets, max_leverage, direction, style, allow_perps, exit_tp_r, exit_tp1_scale_pct)
           VALUES ($1, 10000, $2, 25, $3, $4, $5, $6, $7, 'SWING', $8, 1.5, 50) RETURNING id""",
        f"h13b-{uuid.uuid4().hex[:6]}", [SPOT, PERP] if perps else [SPOT], status, level, list(markets), max_leverage,
        direction, perps)
    if lock:
        await h.pool.execute("INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('BTC', $1)", mid)
    async with h.pool.acquire() as conn:
        m = await load_mandate(conn, mid)
        await ensure_mandate_instance(conn, m, "PAPER-ACCOUNT-001")
    return m


async def candidate(h, *, reason="GRADE_B", decision="TAKE", policy_a=True) -> uuid.UUID:
    spec_id, version_id, scan_id, cid = (uuid.uuid4() for _ in range(4))
    await h.pool.execute("INSERT INTO scan_specs (id, slug, name) VALUES ($1, $2, 'h13b')", spec_id, f"h13b-{spec_id.hex}")
    await h.pool.execute(
        """INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
           VALUES ($1, $2, 1, 'h13b', '{}'::jsonb, '[]'::jsonb)""", version_id, spec_id)
    await h.pool.execute(
        """INSERT INTO scan_runs (id, spec_version_id, spec_hash, scanner_version, indicator_version,
               input_watermark_ns, source_partitions, source_partition_checksums, trigger_reason)
           VALUES ($1, $2, 'h13b', 'test', 'test', 1, '{}'::jsonb, '{}'::jsonb, 'MANUAL')""", scan_id, version_id)
    await h.pool.execute(
        """INSERT INTO signal_candidates (id, scan_run_id, spec_version_id, instrument_id, timeframe, bar_open_ns,
               bar_close_inclusive_ns, interval_end_exclusive_ns, matched_rules, metrics, suggested_entry,
               suggested_stop, suggested_target)
           VALUES ($1, $2, $3, $4, '1h', 1, 2, 3, '[]'::jsonb, '{}'::jsonb, $5, $6, $7)""",
        cid, scan_id, version_id, SPOT, ENTRY, STOP, TARGET)
    if policy_a:
        await h.pool.execute("INSERT INTO policy_decisions (candidate_id, policy, decision, side, reason) "
                             "VALUES ($1, 'A', $2, 'LONG', $3)", cid, decision, reason)
    return cid


def hold(cid, **over) -> proposals.HoldDebate:
    fields = {
        "run_id": uuid.uuid4(), "candidate_id": cid, "instrument_id": SPOT, "trigger_source": "scanner",
        "recommendation": "HOLD", "final_status": "NO_TRADE", "conviction": 0.6, "reasoning": "r" * 900,
        "trace_id": str(uuid.uuid4()), "non_actionable": None, "stop_loss": STOP, "take_profit": TARGET,
        "candidate": {"suggested_entry": ENTRY, "suggested_stop": STOP, "suggested_target": TARGET, "timeframe": "1h",
                      "preset_slug": "h13b-preset"},
        "grade_reasons": ["trend up"]}
    return proposals.HoldDebate(**{**fields, **over})


async def create(h, debate, mandate):
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            return await proposals.create_for_hold(conn, debate, mandate)


async def seeded(h, mandate, **over) -> str:
    """A pending proposal on ``mandate``; its id."""
    cid = await candidate(h)
    assert await create(h, hold(cid, **over), mandate) is not None
    return str(await h.pool.fetchval("SELECT id FROM setup_proposals WHERE candidate_id = $1", cid))


async def post(h, path: str, body: dict | None = None):
    async with h.client() as c:
        return await c.post(f"{URL}/{path}", json=body or {}, headers=h.headers)


async def counts(h) -> tuple[int, int]:
    return (await h.pool.fetchval("SELECT count(*) FROM ai_approvals"),
            await h.pool.fetchval("SELECT count(*) FROM execution_commands"))


async def status_of(h, pid: str) -> str:
    return await h.pool.fetchval("SELECT status FROM setup_proposals WHERE id = $1", uuid.UUID(pid))


ACK = {"acknowledge_against_debate": True}


# --- creation (invariant 4) ------------------------------------------------------------------------------

async def test_created_on_hold_with_policy_a_take_and_shaped_as_an_order_card(h):
    m = await profile(h)
    cid = await candidate(h)
    debate = hold(cid)
    alert = await create(h, debate, m)
    row = await h.pool.fetchrow("SELECT * FROM setup_proposals WHERE candidate_id = $1", cid)
    card = json.loads(row["card"])
    assert (row["status"], row["instrument_id"], row["side"], row["mandate_id"]) == ("PENDING", SPOT, "BUY", m.id)
    assert (row["expires_at"] - row["created_at"]).total_seconds() == pytest.approx(900, abs=5)
    assert OrderCardWidget.model_validate({**card, "quantity": 0.01}).status == "PENDING_APPROVAL"
    assert card["setup_grade"] == "B" and card["grade_inputs"]["entry"] == ENTRY and card["against_debate_verdict"] == "HOLD"
    assert card["debate_link"] == {"run_id": str(debate.run_id), "recommendation": "HOLD", "conviction_score": 0.6,
                                   "candidate_id": str(cid), "against": True, "status": "NO_TRADE"}
    assert len(card["debate_reasoning"]) == 600 and card["preset_slug"] == "h13b-preset" and card["timeframe"] == "1h"
    assert alert["severity"] == "INFO" and "the debate said HOLD" in alert["message"] and "grade B" in alert["message"]


@pytest.mark.parametrize("over", [
    {"trigger_source": "brain_on_demand"}, {"trigger_source": "user_on_demand"}, {"recommendation": "BUY"},
    {"final_status": "PENDING_APPROVAL"}, {"non_actionable": "OPPOSITE_POSITION_OPEN: BUY is opposite"},
    {"non_actionable": "EVIDENCE_INCOMPLETE: bars"}, {"stop_loss": None}, {"take_profit": None},
    {"candidate": {"suggested_stop": STOP, "suggested_target": TARGET}},
    {"take_profit": 60500.0},  # R:R under the 1.5 floor at the entry
], ids=str)
async def test_not_created(h, over):
    m = await profile(h)
    assert await create(h, hold(await candidate(h), **over), m) is None
    assert await h.pool.fetchval("SELECT count(*) FROM setup_proposals") == 0


@pytest.mark.parametrize("kw", [{"reason": "GRADE_NO_TRADE", "decision": "SKIP"}, {"reason": "NO_GRADE", "decision": "SKIP"},
                                {"reason": "HIGH_FUNDING", "decision": "SKIP"}, {"policy_a": False}], ids=str)
async def test_not_created_without_policy_a_take(h, kw):
    m = await profile(h)
    assert await create(h, hold(await candidate(h, **kw)), m) is None
    assert await h.pool.fetchval("SELECT count(*) FROM setup_proposals") == 0


async def test_not_created_without_a_candidate_or_a_profile(h):
    m = await profile(h)
    assert await create(h, hold(None), m) is None
    assert await create(h, hold(await candidate(h)), None) is None
    assert await h.pool.fetchval("SELECT count(*) FROM setup_proposals") == 0


async def test_a_hold_with_an_opposite_position_open_makes_no_proposal(h):
    """A HOLD carries no block_reason (opposite_block_reason reads the verdict's side), so the candidate's side is read."""
    m = await profile(h, markets=("PERP",), max_leverage=3)
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               current_price, quantity, status, leverage, margin_mode)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'SHORT', 60000, 60000, 0.01, 'OPEN', 2, 'ISOLATED')""",
        uuid.uuid4().hex, m.instance_id, PERP)
    assert await create(h, hold(await candidate(h)), m) is None
    assert await h.pool.fetchval("SELECT count(*) FROM setup_proposals") == 0


async def test_btc_spot_routes_to_the_perp_of_a_perp_profile(h):
    m = await profile(h, markets=("PERP",), max_leverage=3)
    pid = await seeded(h, m)
    row = await h.pool.fetchrow("SELECT instrument_id, card FROM setup_proposals WHERE id = $1", uuid.UUID(pid))
    assert row["instrument_id"] == PERP and json.loads(row["card"])["instrument_id"] == PERP


@pytest.mark.parametrize("kw", [{"direction": "SHORT", "markets": ("PERP",), "max_leverage": 3},
                                {"status": "PAUSED"}, {"markets": ("SPOT_MARGIN",), "max_leverage": 3}], ids=str)
async def test_a_refused_route_writes_nothing(h, kw):
    m = await profile(h, **kw)
    assert await create(h, hold(await candidate(h)), m) is None
    assert await h.pool.fetchval("SELECT count(*) FROM setup_proposals") == 0


async def test_a_locked_coin_writes_nothing(h):
    owner = await profile(h)
    other = await profile(h, lock=False)
    assert owner.id != other.id
    assert await create(h, hold(await candidate(h)), other) is None


async def test_a_redelivered_debate_makes_one_proposal_and_one_alert(h):
    m = await profile(h)
    cid = await candidate(h)
    assert await create(h, hold(cid), m) is not None
    assert await create(h, hold(cid), m) is None
    assert await h.pool.fetchval("SELECT count(*) FROM setup_proposals") == 1


async def test_a_helper_failure_never_breaks_the_debate_transaction(h, monkeypatch):
    m = await profile(h)
    cid = await candidate(h)
    run = uuid.uuid4()
    real = proposals._skip_reason

    async def broken(conn, debate, mandate):
        await conn.execute("SELECT * FROM a_table_that_does_not_exist")
        return await real(conn, debate, mandate)

    monkeypatch.setattr(proposals, "_skip_reason", broken)
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("INSERT INTO debate_runs (id, instrument_id, status, trigger_source) "
                               "VALUES ($1, $2, 'NO_TRADE', 'scanner')", run, SPOT)
            assert await proposals.create_for_hold(conn, hold(cid), m) is None
            await conn.execute("UPDATE debate_runs SET failure_reason = 'still-usable' WHERE id = $1", run)
    assert await h.pool.fetchval("SELECT failure_reason FROM debate_runs WHERE id = $1", run) == "still-usable"
    assert await h.pool.fetchval("SELECT count(*) FROM setup_proposals") == 0


async def test_a_failed_publish_is_only_logged():
    class Down:
        async def publish(self, *a):
            raise ConnectionError("down")

    await proposals.publish_alert(Down(), {"proposal_id": "x"})
    await proposals.publish_alert(Down(), None)


def test_the_alert_reads_well_in_the_notifier():
    from cryptonite_notifier import render

    title, body = render.alert_text({"kind": "SCANNER_PROPOSAL", "severity": "INFO",
                                     "message": "BTCUSDT-PERP.BINANCE long, grade B: the debate said HOLD."})
    assert title == "Scanner Proposal" and body.endswith("the debate said HOLD.")


# --- the real worker: invariants 1, 2 and 5 ----------------------------------------------------------------

def scanner_payload(cid) -> dict:
    close_ns = int((time.time() - 60) * 1e9)
    return {"instrument_id": SPOT, "trigger_source": "scanner", "brief_key": "brief:k",
            "candidate_payload": json.dumps({"candidate_id": str(cid), "timeframe": "1h", "bar_close_inclusive_ns": close_ns,
                                             "suggested_entry": 65010, "suggested_stop": 64000, "suggested_target": 67500})}


class PublishRedis(CapRedis):
    def __init__(self):
        super().__init__()
        self.published = []

    async def publish(self, channel, message):
        self.published.append((channel, message))

    async def eval(self, script, numkeys, *args):
        if "INCR" in script:  # the candidate's daily slot reservation
            return 1
        return await super().eval(script, numkeys, *args)


async def run_debate(main, h, payload, models, redis=None):
    m = models.model()
    with override_model(FLASH, m), override_model(PRO, m), override_model(FLASH_LITE, m), \
            override_model(EMBED, FakeEmbedder()):
        await main.process_debate_trigger(h.pool, redis or PublishRedis(), payload, Gateway(pool=h.pool))
    return await h.pool.fetchrow("SELECT * FROM debate_runs ORDER BY created_at DESC LIMIT 1")


@pytest.mark.parametrize("level", [1, 2, 3])
async def test_e2e_a_hold_creates_a_proposal_and_queues_nothing_at_every_level(main, h, level):
    """Invariants 1 and 2: never ``_approval`` or ``_enqueue``, the run stays NO_TRADE, policy B stays DEBATE_HOLD."""
    await profile(h, level=level)
    cid = await candidate(h)
    redis = PublishRedis()
    run = await run_debate(main, h, scanner_payload(cid), Models(lite=HOLD_LITE), redis)
    assert run["status"] == "NO_TRADE"
    row = await h.pool.fetchrow("SELECT * FROM setup_proposals WHERE candidate_id = $1", cid)
    assert row["run_id"] == run["id"] and row["status"] == "PENDING"
    assert await counts(h) == (0, 0)
    b = await h.pool.fetchrow("SELECT decision, reason FROM policy_decisions WHERE candidate_id = $1 AND policy = 'B'", cid)
    assert (b["decision"], b["reason"]) == ("SKIP", "DEBATE_HOLD")
    alerts = [json.loads(m) for ch, m in redis.published if ch == "channel:system_alerts"]
    assert [a["kind"] for a in alerts] == ["SCANNER_PROPOSAL"] and alerts[0]["severity"] == "INFO"

    # Unanswered it simply expires: reading writes EXPIRED and still nothing is queued, whatever the mandate's level
    await h.pool.execute("UPDATE setup_proposals SET expires_at = now() - interval '1 second'")
    async with h.client() as c:
        body = (await c.get(URL)).json()
    assert body["pending"] == 0 and body["proposals"][0]["status"] == "EXPIRED"
    assert await counts(h) == (0, 0)
    assert await h.pool.fetchval("SELECT status FROM debate_runs WHERE id = $1", run["id"]) == "NO_TRADE"


async def test_e2e_a_buy_verdict_keeps_its_route_and_makes_no_proposal(main, h):
    """Invariant 5."""
    await profile(h)
    cid = await candidate(h)
    run = await run_debate(main, h, scanner_payload(cid), Models())
    assert run["status"] == "PENDING_APPROVAL"
    assert await h.pool.fetchval("SELECT source FROM ai_approvals") == "debate"
    assert await h.pool.fetchval("SELECT count(*) FROM setup_proposals") == 0


async def test_e2e_an_operator_debate_makes_no_proposal(main, h):
    await profile(h)
    models = Models(pm={"recommendation": "HOLD", "conviction_score": 0.5, "allocation_pct": 0.0, "reasoning": "mixed"},
                    cro={"final_allocation_pct": 0.0, "conviction_level": "Hold", "risk_reasoning": "fine"})
    run = await run_debate(main, h, {"instrument_id": SPOT, "trigger_source": "user_on_demand"}, models)
    assert run["status"] == "NO_TRADE"
    assert await h.pool.fetchval("SELECT count(*) FROM setup_proposals") == 0


async def test_e2e_a_broken_helper_leaves_the_debate_result_intact(main, h, monkeypatch):
    await profile(h)
    cid = await candidate(h)

    async def broken(conn, debate, mandate):
        raise RuntimeError("proposal bug")

    monkeypatch.setattr(proposals, "_skip_reason", broken)
    run = await run_debate(main, h, scanner_payload(cid), Models(lite=HOLD_LITE))
    assert run["status"] == "NO_TRADE"
    assert await h.pool.fetchval("SELECT reason FROM policy_decisions WHERE candidate_id = $1 AND policy = 'B'", cid) == "DEBATE_HOLD"
    assert await h.pool.fetchval("SELECT count(*) FROM portfolio_decisions WHERE run_id = $1", run["id"]) == 1


# --- the API ------------------------------------------------------------------------------------------

async def test_approve_without_the_acknowledgement_is_refused_and_stays_pending(h):
    pid = await seeded(h, await profile(h))
    res = await post(h, f"{pid}/approve")
    assert res.status_code == 409 and res.json()["detail"].startswith("AGAINST_DEBATE_VERDICT")
    assert await status_of(h, pid) == "PENDING" and await counts(h) == (0, 0)


async def test_an_approve_queues_exactly_one_command_with_source_scanner_proposal(h):
    m = await profile(h, level=2)
    run = uuid.uuid4()
    await h.pool.execute("INSERT INTO debate_runs (id, instrument_id, status, trigger_source) "
                         "VALUES ($1, $2, 'NO_TRADE', 'scanner')", run, SPOT)
    pid = await seeded(h, m, run_id=run)
    res = await post(h, f"{pid}/approve", ACK)
    assert res.status_code == 200, res.text
    body = res.json()
    assert await counts(h) == (1, 1)
    approval = await h.pool.fetchrow("SELECT * FROM ai_approvals")
    assert approval["source"] == "scanner_proposal" and approval["source_ref"] == pid and approval["status"] == "APPROVED"
    command = await h.pool.fetchrow("SELECT * FROM execution_commands")
    payload = json.loads(command["payload"])
    assert command["idempotency_key"] == f"scanner-proposal:{pid}" and str(command["command_id"]) == body["command_id"]
    assert command["requester_id"].startswith("approval:web:operator:") and command["decision_trace_id"] is not None
    assert payload["source"] == "scanner_proposal" and payload["proposal_id"] == pid
    assert payload["against_debate_verdict"] == "HOLD" and payload["acknowledged_against_debate"] is True
    assert payload["debate_link"]["run_id"] == str(run) and payload["debate_link"]["against"] is True
    assert payload["approval_id"] == str(approval["id"]) and payload["mandate_id"] == str(m.id)
    assert payload["allocation_pct"] == 25.0 and float(payload["proposed_stop_loss"]) == STOP
    # H11's TP1 fields ride along and the fill note reads them as a Brain card's
    assert payload["tp1_r"] == 1.5 and payload["tp1_scale_pct"] == 50.0
    assert "tp1_text" in _fill_tp1(payload, 60000)
    # Invariant 2: the run is untouched, and invariant: not veto evidence although written at level 2
    assert await h.pool.fetchval("SELECT status FROM debate_runs WHERE id = $1", run) == "NO_TRADE"
    assert approval["level_at_creation"] == 2
    async with h.pool.acquire() as conn:
        assert (await gate_metrics(conn, await load_mandate(conn, m.id))).veto_rate_pct is None
    row = await h.pool.fetchrow("SELECT * FROM setup_proposals WHERE id = $1", uuid.UUID(pid))
    assert row["status"] == "APPROVED" and row["command_id"] == command["command_id"] and row["approval_id"] == approval["id"]
    assert row["decided_by"].startswith("operator:")


async def test_a_double_approve_queues_once(h):
    pid = await seeded(h, await profile(h))
    first = await post(h, f"{pid}/approve", ACK)
    second = await post(h, f"{pid}/approve", ACK)
    assert first.status_code == 200 and second.status_code == 409
    assert await counts(h) == (1, 1)


async def test_concurrent_approves_queue_once(h):
    pid = await seeded(h, await profile(h))
    results = await asyncio.gather(post(h, f"{pid}/approve", ACK), post(h, f"{pid}/approve", ACK))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert await counts(h) == (1, 1)


async def test_an_expired_proposal_is_refused(h):
    pid = await seeded(h, await profile(h))
    await h.pool.execute("UPDATE setup_proposals SET expires_at = now() - interval '1 second'")
    res = await post(h, f"{pid}/approve", ACK)
    assert res.status_code == 409 and "EXPIRED" in res.json()["detail"]
    assert await status_of(h, pid) == "EXPIRED" and await counts(h) == (0, 0)
    assert (await post(h, f"{pid}/preflight")).json()["code"] == "NOT_PENDING"


async def test_a_slip_below_the_floor_is_refused_and_stays_pending(h):
    pid = await seeded(h, await profile(h))
    await set_prices(h, 62500.0)  # reward 2500 / risk 4500
    flight = (await post(h, f"{pid}/preflight")).json()
    assert flight["ok"] is False and flight["code"] == "REWARD_TO_RISK_SLIPPED"
    assert flight["meets_rr_floor"] is False and flight["rr_floor"] == 1.5 and flight["live_rr"] == pytest.approx(0.5556, abs=1e-3)
    res = await post(h, f"{pid}/approve", ACK)
    assert res.status_code == 409 and res.json()["detail"].startswith("REWARD_TO_RISK_SLIPPED")
    row = await h.pool.fetchrow("SELECT status, refusal FROM setup_proposals WHERE id = $1", uuid.UUID(pid))
    assert row["status"] == "PENDING" and row["refusal"].startswith("REWARD_TO_RISK_SLIPPED")
    assert await counts(h) == (0, 0)


async def test_preflight_returns_the_plan_and_live_rr_and_writes_nothing(h):
    pid = await seeded(h, await profile(h))
    flight = (await post(h, f"{pid}/preflight")).json()
    assert flight["ok"] is True and flight["meets_rr_floor"] is True and flight["live_rr"] == pytest.approx(2.5)
    assert flight["allocation_pct"] == 25.0 and "tp1" in flight
    assert await status_of(h, pid) == "PENDING" and await counts(h) == (0, 0)


async def test_a_paused_mandate_refuses_approve_as_a_brain_card_is_refused(h):
    """Invariant 3: the Brain card's plan runs."""
    m = await profile(h)
    pid = await seeded(h, m)
    await h.pool.execute("UPDATE ai_mandates SET status = 'PAUSED' WHERE id = $1", m.id)
    res = await post(h, f"{pid}/approve", ACK)
    assert res.status_code == 409 and res.json()["detail"].startswith("MANDATE_HALTED")
    assert await status_of(h, pid) == "PENDING" and await counts(h) == (0, 0)


async def test_a_missing_exit_monitor_refuses_approve(h):
    pid = await seeded(h, await profile(h))
    await h.redis.delete("exit_monitor:heartbeat")
    res = await post(h, f"{pid}/approve", ACK)
    assert res.status_code == 409 and res.json()["detail"].startswith("EXIT_MONITOR_DOWN")
    assert await status_of(h, pid) == "PENDING"


async def test_an_opposite_position_opened_after_the_proposal_refuses_approve(h):
    m = await profile(h, markets=("PERP",), max_leverage=3)
    pid = await seeded(h, m)
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               current_price, quantity, status, leverage, margin_mode)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'SHORT', 60000, 60000, 0.01, 'OPEN', 2, 'ISOLATED')""",
        uuid.uuid4().hex, m.instance_id, PERP)
    res = await post(h, f"{pid}/approve", {**ACK, "totp_code": h.totp.now()})
    assert res.status_code == 409 and "OPPOSITE_POSITION_OPEN" in res.json()["detail"]
    assert await status_of(h, pid) == "PENDING" and await counts(h) == (0, 0)


async def test_a_queue_failure_reverts_the_claim(h, monkeypatch):
    pid = await seeded(h, await profile(h))

    async def boom(*a, **kw):
        raise RuntimeError("queue down")

    monkeypatch.setattr(proposal_routes, "record_operator_approval", boom)
    res = await post(h, f"{pid}/approve", ACK)
    assert res.status_code == 502
    assert await status_of(h, pid) == "PENDING" and await counts(h) == (0, 0)


async def test_reject_decides_it_and_nothing_can_approve_it_after(h):
    pid = await seeded(h, await profile(h))
    assert (await post(h, f"{pid}/reject")).json() == {"status": "REJECTED"}
    assert (await post(h, f"{pid}/approve", ACK)).status_code == 409
    assert (await post(h, f"{pid}/reject")).status_code == 409
    assert await status_of(h, pid) == "REJECTED" and await counts(h) == (0, 0)


async def test_list_filters_by_status_and_unknown_proposal_is_404(h):
    m = await profile(h)
    pid = await seeded(h, m)
    async with h.client() as c:
        all_rows = (await c.get(URL)).json()
        none = (await c.get(URL, params={"status": "APPROVED"})).json()
    assert all_rows["pending"] == 1 and all_rows["proposals"][0]["id"] == pid
    assert all_rows["proposals"][0]["mandate_name"] == m.name and none["proposals"] == []
    assert (await post(h, f"{uuid.uuid4()}/approve", ACK)).status_code == 404


async def test_a_perp_proposal_is_sized_to_fit_and_needs_totp(h):
    """The mandate's whole position limit never fits under the exposure ceiling: approve sizes as a debate entry does."""
    m = await profile(h, markets=("PERP",), max_leverage=3)
    pid = await seeded(h, m)
    flight = (await post(h, f"{pid}/preflight")).json()
    assert flight["ok"] is True and flight["needs_totp"] is True and flight["leverage_plan"]["ok"] is True
    assert 1 <= flight["allocation_pct"] <= 25
    assert flight["meets_rr_floor"] is True
    assert (await post(h, f"{pid}/approve", ACK)).status_code in (401, 403)  # no TOTP
    assert await status_of(h, pid) == "PENDING" and await counts(h) == (0, 0)
    res = await post(h, f"{pid}/approve", {**ACK, "totp_code": h.totp.now()})
    assert res.status_code == 200, res.text
    payload = json.loads(await h.pool.fetchval("SELECT payload FROM execution_commands"))
    assert payload["instrument_id"] == PERP and payload["margin_mode"] == "ISOLATED" and payload["stop_loss"]
    assert payload["leverage"] >= 1 and payload["leverage_plan"]["binding_ceiling"]
    assert payload["allocation_pct"] == flight["allocation_pct"]


# --- consumers of the source --------------------------------------------------------------------------

async def gate_reason(h):
    row = await h.pool.fetchrow("SELECT * FROM execution_commands")
    payload = json.loads(row["payload"])
    quotes = SimpleQuoteCache()
    quotes.set_quote(payload["instrument_id"], 59999, 60001)
    dispatcher, _ = make_fresh_dispatcher(Decimal("100000"))
    intent = OrderIntent(
        client_order_id=str(row["command_id"]), instance_id=row["target_instance_id"], instrument_id=payload["instrument_id"],
        side=payload["side"], order_type=payload["order_type"], quantity=Decimal("0.001"),
        stop_loss=Decimal(str(payload["stop_loss"])), require_stop=True, leverage=payload.get("leverage"),
        margin_mode=payload.get("margin_mode"))
    async with h.pool.acquire() as conn:
        mandate = await load_mandate(conn, row["mandate_id"])
        return await gate_ai_order(conn, make_engine(dispatcher), row, payload, intent, mandate,
                                   quotes.quote(payload["instrument_id"]))


async def test_the_orchestrator_gate_takes_the_source_and_rechecks_an_opposite_position(h):
    m = await profile(h, markets=("PERP",), max_leverage=3)
    pid = await seeded(h, m)
    assert (await post(h, f"{pid}/approve", {**ACK, "totp_code": h.totp.now()})).status_code == 200
    assert await gate_reason(h) is None  # no unknown-source refusal: the operator's approval proves it
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               current_price, quantity, status, leverage, margin_mode)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'SHORT', 60000, 60000, 0.01, 'OPEN', 2, 'ISOLATED')""",
        uuid.uuid4().hex, m.instance_id, PERP)
    assert await gate_reason(h) == "OPPOSITE_POSITION_OPEN"


async def test_the_timeline_labels_the_entry_and_reflections_skip_it(h):
    m = await profile(h)
    pid = await seeded(h, m)
    assert (await post(h, f"{pid}/approve", ACK)).status_code == 200
    await h.pool.execute("UPDATE execution_commands SET status = 'COMPLETED', created_at = now() - interval '1 minute'")
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price, quantity,
               current_price, status, realized_pnl, closed_at)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'LONG', 60000, 0.01, 61000, 'CLOSED', 10, now())""",
        uuid.uuid4().hex, m.instance_id, SPOT)
    row = await h.pool.fetchrow("SELECT entry_source, entry_setup_grade, debate_run_id FROM agent_safe_trade_timeline")
    assert row["entry_source"] == "scanner_proposal" and row["entry_setup_grade"] == "B" and row["debate_run_id"] is None
    assert await h.pool.fetch(reflection.CARD_TRADES_SQL, 5) == []
