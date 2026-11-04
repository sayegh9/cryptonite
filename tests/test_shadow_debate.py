"""H13d: a reworked lite prompt answers in shadow only, and a pass mark fixed before any data judges it (docs/adr/0012).

The worker tests drive the real debate against the CI Postgres with the live models stubbed; ``ShadowGateway`` scripts only
the shadow role, so a call is proven to happen (or not) and its input can be read.
"""
import asyncio
import datetime
import hashlib
import importlib.util
import json
import random
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_agents.debate_fleet import DEBATE_LITE, DEBATE_LITE_SHADOW
from cryptonite_agents.gateway import BudgetExceeded, Gateway, GatewayError, ModelUnavailable
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import (
    DEFAULT_ROUTES,
    EMBED,
    FLASH,
    FLASH_LITE,
    PRO,
    BudgetPriority,
)
from cryptonite_execution import profile_lifecycle
from cryptonite_schemas.debate import LiteDebateDecision
from cryptonite_schemas.marks import quote_tick_key
from migrated_db import migrated_database
from test_debate_gate import FakeEmbedder, Models, load
from test_setup_proposals import (
    ENTRY,
    MARK_KEY,
    SPOT,
    PublishRedis,
    candidate,
    profile,
    scanner_payload,
    set_prices,
    set_tick,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "debate_worker"))
import gate  # noqa: E402
import shadow_debate  # noqa: E402
import shadow_eval as se  # noqa: E402
from shadow_eval import Row  # noqa: E402

pytestmark = pytest.mark.usefixtures("complete_lake_bars")

ROLE = DEBATE_LITE_SHADOW.role
LIVE_SECRET = "LIVE_VERDICT_REASONING_7f3a"
SHADOW_SECRET = "SHADOW_ONLY_REASONING_91bc"
REMOVED = "Prefer HOLD when the data is mixed, stale or thin"
LIVE_HOLD = {"bull_case": LIVE_SECRET, "bear_case": LIVE_SECRET, "recommendation": "HOLD", "conviction_score": 0.6,
             "allocation_pct": 0.0, "reasoning": LIVE_SECRET}
LIVE_TABLES = ("debate_runs", "debate_turns", "portfolio_decisions", "policy_decisions", "setup_proposals",
               "ai_approvals", "execution_commands", "ai_profile_decisions", "candidate_funnel_stops")


def shadow_take() -> SimpleNamespace:
    output = LiteDebateDecision(bull_case="b", bear_case="r", recommendation="BUY", conviction_score=0.7, allocation_pct=2.0,
                                reasoning=SHADOW_SECRET)
    return SimpleNamespace(output=output, model="test:shadow", cost_usd=Decimal("0.003"))


class ShadowGateway(Gateway):
    """The real gateway for every live role; the shadow role is scripted and its calls recorded."""

    def __init__(self, pool, behave=None):
        super().__init__(pool=pool)
        self.behave = behave
        self.shadow_calls: list[dict] = []
        self.live_prompts: dict[str, str] = {}

    async def run(self, role, prompt, **kw):
        if role != ROLE:
            self.live_prompts[role] = prompt
            return await super().run(role, prompt, **kw)
        self.shadow_calls.append({"prompt": prompt, **kw})
        if self.behave:
            await self.behave()
        return shadow_take()


@pytest.fixture(autouse=True)
def shadow_on_by_default(monkeypatch):
    monkeypatch.setenv("DEBATE_AI_TRIAGE", "1")
    monkeypatch.delenv("DEBATE_SHADOW_ENABLED")  # the default is on


@pytest.fixture(scope="module")
def main():
    return load("debate_worker_main_h13d", "main.py")


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_h13d") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("TRUNCATE setup_proposals, ai_profile_coin_locks, ai_profile_decisions, ai_approvals, "
                               "notifications_outbox, execution_commands, paper_orders, paper_positions, debate_runs, "
                               "shadow_debate_verdicts, llm_cost_ledger, ai_decision_traces CASCADE")
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("UPDATE paper_accounts SET total_equity = 100000, cash_balance = 100000")
    profile_lifecycle.reset_switch_cache()
    await harness.redis.set(quote_tick_key(SPOT), json.dumps({
        "instrument_id": SPOT, "bid_price": "59999", "ask_price": "60001", "arrival_ns": time.time_ns()}), ex=60)
    await set_prices(harness, ENTRY)
    await set_tick(harness, "0.01")
    try:
        yield harness
    finally:
        await harness.redis.delete(MARK_KEY, "market:24h:BTCUSDT", quote_tick_key(SPOT))
        await close_harness(harness)


async def drive(main, h, payload, gateway, lite=None, redis=None):
    """One debate through the real worker, then the detached shadow task to its end."""
    m = Models(lite=lite or LIVE_HOLD).model()
    redis = redis or PublishRedis()
    with override_model(FLASH, m), override_model(PRO, m), override_model(FLASH_LITE, m), \
            override_model(EMBED, FakeEmbedder()):
        await main.process_debate_trigger(h.pool, redis, payload, gateway)
        await asyncio.gather(*shadow_debate._TASKS)
    return await h.pool.fetchrow("SELECT * FROM debate_runs ORDER BY created_at DESC LIMIT 1")


async def live_rows(h) -> dict[str, list[str]]:
    out = {}
    for table in LIVE_TABLES:
        rows = await h.pool.fetch(f"SELECT to_jsonb(t)::text AS j FROM {table} t ORDER BY 1")  # noqa: S608
        out[table] = [r["j"] for r in rows]
    return out


async def verdict(h, cid):
    return await h.pool.fetchrow("SELECT * FROM shadow_debate_verdicts WHERE candidate_id = $1", cid)


# ------------------------------------------------------------------------------------ invariant 1: reaches nothing live


async def test_the_debate_workers_role_can_write_a_verdict_and_live_models_cannot_read_one(h):
    """The workers connect as cryptonite_api. A SELECT-only grant lost every answer and blinded the spend cap."""
    async with h.pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("SET CONSTRAINTS ALL DEFERRED")
            await conn.execute("SET LOCAL ROLE cryptonite_api")
            await conn.fetchval(shadow_debate._SPENT_TODAY)
            assert await conn.fetchval(
                "SELECT has_table_privilege('cryptonite_api', 'shadow_debate_verdicts', 'INSERT')")
            assert not await conn.fetchval(
                "SELECT has_table_privilege('cryptonite_agent_ro', 'shadow_debate_verdicts', 'SELECT')")
        finally:
            await tx.rollback()


async def test_a_shadow_take_on_a_live_hold_changes_no_live_row(main, h):
    await profile(h)
    cid = await candidate(h)
    seen = {}

    async def behave():
        seen["rows"] = await live_rows(h)

    run = await drive(main, h, scanner_payload(cid), ShadowGateway(h.pool, behave))
    row = await verdict(h, cid)
    assert (row["state"], row["recommendation"], row["run_id"]) == ("OK", "BUY", run["id"])
    assert row["reasoning"] == SHADOW_SECRET and row["prompt_version"] == se.PROMPT_VERSION
    assert await live_rows(h) == seen["rows"]
    assert run["status"] == "NO_TRADE"
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
    assert await h.pool.fetchval("SELECT reason FROM policy_decisions WHERE candidate_id = $1 AND policy = 'B'", cid) == "DEBATE_HOLD"
    assert await h.pool.fetchval("SELECT status FROM setup_proposals WHERE candidate_id = $1", cid) == "PENDING"
    turns = await h.pool.fetch("SELECT agent_role, content FROM debate_turns WHERE run_id = $1", run["id"])
    assert [t["agent_role"] for t in turns] == [DEBATE_LITE.name]
    assert not any(SHADOW_SECRET in t["content"] for t in turns)
    assert not any(SHADOW_SECRET in j for rows in (await live_rows(h)).values() for j in rows)


async def test_channels_alerts_and_heartbeats_match_a_run_with_the_switch_off(main, h, monkeypatch):
    async def one_run():
        await h.pool.execute("TRUNCATE setup_proposals, ai_profile_coin_locks, ai_profile_decisions, debate_runs, "
                             "policy_decisions, candidate_funnel_stops, shadow_debate_verdicts CASCADE")
        await h.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
        await profile(h)
        cid = await candidate(h)
        redis = PublishRedis()
        run = await drive(main, h, scanner_payload(cid), ShadowGateway(h.pool), redis=redis)
        channels = [ch for ch, _ in redis.published]
        kinds = [json.loads(m).get("type") or json.loads(m).get("kind") for _, m in redis.published]
        dump = repr(vars(redis))
        assert SHADOW_SECRET not in dump and DEBATE_LITE_SHADOW.name not in dump and ROLE not in dump
        counts = {t: await h.pool.fetchval(f"SELECT count(*) FROM {t}") for t in LIVE_TABLES}  # noqa: S608
        return run["status"], channels, kinds, counts

    with_shadow = await one_run()
    monkeypatch.setenv("DEBATE_SHADOW_ENABLED", "0")
    assert await one_run() == with_shadow


async def test_the_shadow_call_never_receives_the_live_verdict_or_reasoning(main, h):
    await profile(h)
    cid = await candidate(h)
    gw = ShadowGateway(h.pool)
    await drive(main, h, scanner_payload(cid), gw)
    (call,) = gw.shadow_calls
    assert call["prompt"] == gw.live_prompts["debate.lite"]  # the lite call's own text, unchanged
    assert LIVE_SECRET not in json.dumps(call, default=str)


async def test_the_call_has_no_tools_no_mandate_and_no_parent_trace(main, h):
    await profile(h)
    gw = ShadowGateway(h.pool)
    await drive(main, h, scanner_payload(await candidate(h)), gw)
    (call,) = gw.shadow_calls
    assert call["tools"] == () and call["parent_trace_id"] is None
    assert call["context"].role == ROLE and call["context"].mandate_id is None
    assert call["instructions"] == DEBATE_LITE_SHADOW.instructions
    assert call["output_type"] is DEBATE_LITE.output_type


async def test_through_the_real_gateway_cost_is_attributed_to_the_shadow_role_and_not_to_the_profile(main, h):
    mandate = await profile(h)
    cid = await candidate(h)
    await drive(main, h, scanner_payload(cid), Gateway(pool=h.pool))
    row = await verdict(h, cid)
    assert row["state"] == "OK" and row["model"] and row["cost_usd"] is not None
    assert await h.pool.fetchval("SELECT count(*) FROM llm_cost_ledger WHERE role = $1", ROLE) == 1
    trace = await h.pool.fetchrow("SELECT mandate_id, parent_id FROM ai_decision_traces WHERE role = $1", ROLE)
    assert trace["mandate_id"] is None and trace["parent_id"] is None
    # the live lite call keeps its own attribution
    assert await h.pool.fetchval("SELECT mandate_id FROM ai_decision_traces WHERE role = 'debate.lite'") in (None, str(mandate.id))


async def test_models_and_the_brain_cannot_read_the_table(h):
    source = (ROOT / "infra/migrations/versions/065_shadow_debate_verdicts.py").read_text()
    assert "GRANT SELECT ON shadow_debate_verdicts TO cryptonite_agent_ro" not in source
    if await h.pool.fetchval("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro')"):
        assert not await h.pool.fetchval("SELECT has_table_privilege('cryptonite_agent_ro', 'shadow_debate_verdicts', 'SELECT')")


def test_no_live_module_reads_the_shadow_output():
    """Only the shadow step, its report and the migration name the table or the persona."""
    allowed = {"shadow_debate.py", "shadow_prompt_report.py", "065_shadow_debate_verdicts.py", "debate_fleet.py"}
    found = set()
    for folder in ("services", "packages", "apps/web/src"):
        for path in (ROOT / folder).rglob("*"):
            if path.suffix in (".py", ".ts", ".tsx") and "node_modules" not in path.parts:
                text = path.read_text(errors="ignore")
                if "shadow_debate_verdicts" in text or "DEBATE_LITE_SHADOW" in text:
                    found.add(path.name)
    assert found <= allowed
    main_source = (ROOT / "services/debate_worker/main.py").read_text()
    assert main_source.count("shadow_debate.") == 1 and "= shadow_debate." not in main_source


# ------------------------------------------------------------------------------------ invariant 2: never affects the debate


async def test_it_runs_after_the_commit_policy_b_and_the_alert(main, h):
    await profile(h)
    cid = await candidate(h)
    redis = PublishRedis()
    seen = {}

    async def behave():
        seen["status"] = await h.pool.fetchval("SELECT status FROM debate_runs WHERE candidate_id = $1", cid)
        seen["b"] = await h.pool.fetchval("SELECT reason FROM policy_decisions WHERE candidate_id = $1 AND policy = 'B'", cid)
        seen["alerts"] = [json.loads(m)["kind"] for ch, m in redis.published if ch == "channel:system_alerts"]

    await drive(main, h, scanner_payload(cid), ShadowGateway(h.pool, behave), redis=redis)
    assert seen == {"status": "NO_TRADE", "b": "DEBATE_HOLD", "alerts": ["SCANNER_PROPOSAL"]}


@pytest.mark.parametrize(("failure", "state"), [
    (RuntimeError("provider bug"), "ERROR"),
    (GatewayError("unknown role"), "ERROR"),
    (ModelUnavailable("debate.lite.shadow", "no model"), "ERROR"),
    (BudgetExceeded(ROLE, "DAILY_BUDGET_RESERVED_FOR_CORE_ROLES"), "SKIPPED_BUDGET"),
])
async def test_a_failure_writes_its_state_and_leaves_the_debate_alone(main, h, failure, state):
    await profile(h)
    cid = await candidate(h)

    async def behave():
        raise failure

    run = await drive(main, h, scanner_payload(cid), ShadowGateway(h.pool, behave))
    assert (await verdict(h, cid))["state"] == state
    assert run["status"] == "NO_TRADE" and run["failure_reason"] is None
    assert await h.pool.fetchval("SELECT reason FROM policy_decisions WHERE candidate_id = $1 AND policy = 'B'", cid) == "DEBATE_HOLD"
    assert await h.pool.fetchval("SELECT count(*) FROM setup_proposals WHERE candidate_id = $1", cid) == 1


async def test_a_timeout_writes_timeout_and_leaves_the_debate_alone(main, h, monkeypatch):
    monkeypatch.setattr(shadow_debate, "SHADOW_TIMEOUT_S", 0.01)
    await profile(h)
    cid = await candidate(h)

    async def behave():
        await asyncio.sleep(5)

    run = await drive(main, h, scanner_payload(cid), ShadowGateway(h.pool, behave))
    row = await verdict(h, cid)
    assert row["state"] == "TIMEOUT" and row["recommendation"] is None and run["status"] == "NO_TRADE"


async def seed_spend(h, cost, age=datetime.timedelta(0)):
    await h.pool.execute("INSERT INTO shadow_debate_verdicts (candidate_id, run_id, prompt_version, state, cost_usd, created_at) "
                         "VALUES ($1, $2, 'shadow-v1', 'OK', $3, now() - $4::interval)",
                         await candidate(h), uuid.uuid4(), cost, age)


async def test_the_daily_cap_skips_the_call_and_writes_skipped_budget(main, h):
    assert shadow_debate.SHADOW_DAILY_USD_CAP == Decimal("1.00")
    await profile(h)
    cid = await candidate(h)
    await seed_spend(h, Decimal("1.00"))
    gw = ShadowGateway(h.pool)
    run = await drive(main, h, scanner_payload(cid), gw)
    assert gw.shadow_calls == [] and (await verdict(h, cid))["state"] == "SKIPPED_BUDGET"
    assert run["status"] == "NO_TRADE"


async def test_the_cap_reads_only_today_utc_and_only_this_table(main, h):
    await profile(h)
    cid = await candidate(h)
    await seed_spend(h, Decimal("5"), datetime.timedelta(days=2))
    await h.pool.execute("INSERT INTO llm_cost_ledger (request_id, agent_name, model_name, input_tokens, output_tokens, "
                         "total_cost_usd, role) VALUES ($1, 'x', 'm', 0, 0, 9, 'debate.lite')", str(uuid.uuid4()))
    await seed_spend(h, Decimal("0.99"))
    gw = ShadowGateway(h.pool)
    await drive(main, h, scanner_payload(cid), gw)
    assert len(gw.shadow_calls) == 1 and (await verdict(h, cid))["state"] == "OK"


async def test_unknown_spend_makes_no_call_and_raises_nothing():
    class BrokenPool:
        def acquire(self):
            raise RuntimeError("pool closed")

    gw = SimpleNamespace(run=None)  # calling it would raise, and that is what must not happen
    await shadow_debate.run_shadow(BrokenPool(), gw, uuid.uuid4(), uuid.uuid4(), "p")


async def test_a_failed_write_and_a_cancel_end_inside_the_task(h, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("write failed")

    with monkeypatch.context() as m:
        m.setattr(shadow_debate, "_write", broken)
        await shadow_debate.run_shadow(h.pool, ShadowGateway(h.pool), await candidate(h), uuid.uuid4(), "p")

    async def forever():
        await asyncio.sleep(30)

    task = shadow_debate.spawn(h.pool, ShadowGateway(h.pool, forever), await candidate(h), uuid.uuid4(), "p")
    await asyncio.sleep(0.05)
    task.cancel()
    assert await asyncio.gather(task) == [None]  # ends normally, carrying nothing out


def test_spawn_never_raises_without_a_loop():
    assert shadow_debate.spawn(None, None, uuid.uuid4(), uuid.uuid4(), "p") is None


# ------------------------------------------------------------------------------------ when it runs


async def operator_like_debate(main, h, source, cid=None):
    payload = {"instrument_id": SPOT, "trigger_source": source}
    if cid:
        payload["candidate_payload"] = scanner_payload(cid)["candidate_payload"]
    gw = ShadowGateway(h.pool)
    run = await drive(main, h, payload, gw)
    return run, gw


@pytest.mark.parametrize("source", ["user_on_demand", *sorted(gate.BRAIN_SOURCES)])
async def test_operator_and_brain_debates_make_no_shadow_call(main, h, source):
    await profile(h)
    cid = await candidate(h)
    run, gw = await operator_like_debate(main, h, source, cid)
    assert run["trigger_source"] == source and run["status"] != "GATE_REFUSED"
    assert gw.shadow_calls == [] and await verdict(h, cid) is None


@pytest.mark.parametrize("value", ["0", "false", "off"])
async def test_the_switch_off_makes_no_call(main, h, monkeypatch, value):
    monkeypatch.setenv("DEBATE_SHADOW_ENABLED", value)
    await profile(h)
    cid = await candidate(h)
    gw = ShadowGateway(h.pool)
    run = await drive(main, h, scanner_payload(cid), gw)
    assert run["status"] == "NO_TRADE" and gw.shadow_calls == [] and await verdict(h, cid) is None


async def test_a_lite_verdict_that_escalates_still_reached_the_lite_call(main, h):
    await profile(h)
    cid = await candidate(h)
    big = {**LIVE_HOLD, "recommendation": "BUY", "allocation_pct": 8.0}
    gw = ShadowGateway(h.pool)
    await drive(main, h, scanner_payload(cid), gw, lite=big)
    assert len(gw.shadow_calls) == 1 and gw.shadow_calls[0]["prompt"] == gw.live_prompts["debate.lite"]


async def test_a_debate_refused_at_the_gate_makes_no_shadow_call(main, h):
    await profile(h)
    cid = await candidate(h)
    stale = {"candidate_id": str(cid), "timeframe": "1h", "bar_close_inclusive_ns": int((time.time() - 4 * 3600) * 1e9)}
    gw = ShadowGateway(h.pool)
    run = await drive(main, h, {**scanner_payload(cid), "candidate_payload": json.dumps(stale)}, gw)
    assert run["status"] == "GATE_REFUSED" and gw.shadow_calls == []


# ------------------------------------------------------------------------------------ the prompt


def test_the_shadow_prompt_drops_the_sentence_and_the_live_one_keeps_it():
    text = DEBATE_LITE_SHADOW.instructions
    assert REMOVED not in text and REMOVED in DEBATE_LITE.instructions
    for part in ("disagreement alone is not a reason to refuse", "Stale or missing essential data is still a reason to refuse",
                 "raw signals before any filter", "positive expected value", "only in the setup's own direction, else HOLD",
                 "probability that it reaches its target before its stop"):
        assert part in text
    assert "bull case" in text and "preferred_market" in text and "VERIFIED FACTS" in text


def test_shadow_v1_is_frozen_a_new_text_needs_a_new_version():
    digest = hashlib.sha256(DEBATE_LITE_SHADOW.instructions.encode()).hexdigest()
    assert se.PROMPT_VERSION == "shadow-v1" and digest == "864a681a39cc41e4ee75b06a278d3293ced95502646084a89a80e072569fbbdc"


def test_the_route_is_its_own_and_first_to_stop_on_budget():
    route = DEFAULT_ROUTES[ROLE]
    lite = DEFAULT_ROUTES["debate.lite"]
    assert route.priority is BudgetPriority.OPTIONAL and route.primary_model == lite.primary_model
    assert route.max_output_tokens == lite.max_output_tokens and route.thinking_budget == lite.thinking_budget
    assert route.timeout_s > shadow_debate.SHADOW_TIMEOUT_S and DEBATE_LITE_SHADOW.tools == ()
    assert route.daily_cap_usd == shadow_debate.SHADOW_DAILY_USD_CAP and shadow_debate.SHADOW_TIMEOUT_S == 45


# ------------------------------------------------------------------------------------ the pass mark


DAY0 = datetime.date(2026, 12, 1)


def row(day=0, take=True, net=0.5, regime=se.UP, direction="LONG", **over) -> Row:
    side = "BUY" if direction == "LONG" else "SELL"
    return Row(**{"day": DAY0 + datetime.timedelta(days=day), "state": "OK", "prompt_version": "shadow-v1",
                  "recommendation": side if take else "HOLD", "direction": direction, "net_r": net, "regime": regime, **over})


def dataset(*, days=30, per_day=6, takes_per_day=3, up=(0.5, -0.2), down=(0.5, -0.2)) -> list[Row]:
    """Odd days are UP, even days DOWN; (mean TAKE, mean SKIP) per regime with a small spread so no day is degenerate."""
    rows = []
    for d in range(days):
        regime, (t, s) = (se.UP, up) if d % 2 else (se.DOWN, down)
        wiggle = 0.02 * (d % 3 - 1)
        for i in range(per_day):
            take = i < takes_per_day
            rows.append(row(d, take, (t if take else s) + wiggle, regime))
    return rows


def test_a_clean_edge_passes():
    v = se.evaluate(dataset())
    assert v.result == se.PASS, v.reasons
    assert v.stats["overall"]["rows"] == 180 and v.stats["days"] == 30
    assert v.stats["regimes"][se.UP]["takes"] == 45 and v.stats["lower_bound"] > 0


@pytest.mark.parametrize(("rows", "missing"), [
    (dataset(days=33, per_day=3, takes_per_day=1), "99 labelled rows"),
    (dataset(days=20, per_day=6), "20 calendar days"),
])
def test_too_little_data_lists_what_is_missing(rows, missing):
    v = se.evaluate(rows)
    assert v.result == se.NOT_ENOUGH_DATA
    assert any(missing in reason for reason in v.reasons), v.reasons


def test_regime_counts_are_size_conditions():
    only_up = [r for r in dataset(days=40) if r.regime == se.UP]
    v = se.evaluate(only_up + [row(d, True, regime=se.DOWN) for d in range(29)])
    assert v.result == se.NOT_ENOUGH_DATA and any("29 labelled rows in regime DOWN" in r for r in v.reasons)
    few_takes = [r for r in dataset(days=40) if r.regime == se.UP]
    few_takes += [row(d, i < 1, regime=se.DOWN, net=0.5 if i < 1 else -0.2) for d in range(9) for i in range(4)]
    v = se.evaluate(few_takes)
    assert v.result == se.NOT_ENOUGH_DATA and v.reasons == ["9 TAKEs in regime DOWN, need 10"]


def test_a_failing_filter_stays_not_enough_data_until_the_size_is_met():
    rows = dataset(up=(-1, 0), down=(-1, 0))
    assert se.evaluate(rows[:60]).result == se.NOT_ENOUGH_DATA
    assert se.evaluate(rows).result == se.FAIL


def fails(rows, *expected):
    v = se.evaluate(rows)
    assert v.result == se.FAIL, v.reasons
    for text in expected:
        assert any(text in r for r in v.reasons), v.reasons
    return v


def test_take_rate_too_high_and_too_low_fail():
    assert len(fails(dataset(per_day=10, takes_per_day=8), "TAKE rate 80.0%").reasons) == 1
    assert len(fails(dataset(days=40, per_day=20, takes_per_day=1), "TAKE rate 5.0%").reasons) == 1


def test_always_take_and_never_take_both_fail():
    always = fails(dataset(takes_per_day=6), "TAKE rate 100.0%", "TAKE minus SKIP n/a overall")
    assert always.stats["overall"]["skips"] == 0
    never = fails(dataset(takes_per_day=0), "TAKE rate 0.0%", "mean net R of TAKE n/a")
    assert never.stats["overall"]["takes"] == 0


def test_a_take_mean_at_or_below_zero_fails_even_with_a_big_difference():
    assert len(fails(dataset(up=(-0.1, -0.9), down=(-0.1, -0.9)), "mean net R of TAKE").reasons) == 1


def test_a_small_difference_fails_overall_and_in_each_regime():
    v = fails(dataset(up=(0.3, 0.2), down=(0.3, 0.2)), "overall is below", "regime UP is below", "regime DOWN is below")
    assert len(v.reasons) == 3


def test_a_win_in_one_regime_only_fails():
    v = fails(dataset(up=(1.0, -0.5), down=(0.05, 0.0)), "regime DOWN is below")
    assert len(v.reasons) == 1 and v.stats["overall"]["edge"] >= se.MIN_EDGE_R


def test_an_edge_from_two_lucky_days_fails_on_the_lower_bound_only():
    rows = dataset(up=(-0.01, 0.0), down=(-0.01, 0.0))
    rows = [Row(**{**vars(r), "net_r": 15.0}) if r.recommendation == "BUY" and r.day in (DAY0, DAY0 + datetime.timedelta(days=1))
            else r for r in rows]
    v = fails(rows, "90% lower bound")
    assert len(v.reasons) == 1


def test_rows_outside_the_measure_are_filtered():
    good = dataset()
    noise = [row(0, True, -50, state="ERROR"), row(0, True, -50, state="TIMEOUT"), row(0, True, -50, prompt_version="shadow-v0"),
             row(0, True, None), row(0, True, -50, trigger_reason="HISTORICAL_PRIMING"), row(0, True, -50, state="SKIPPED_BUDGET")]
    v = se.evaluate(good + noise)
    assert v.result == se.PASS and v.stats["overall"]["rows"] == len(good)


def test_a_verdict_against_the_setup_is_a_skip():
    assert se.is_take(row(direction="LONG")) and se.is_take(row(direction="SHORT"))
    assert not se.is_take(row(take=False))
    assert not se.is_take(Row(**{**vars(row(direction="SHORT")), "recommendation": "BUY"}))
    assert not se.is_take(Row(**{**vars(row(direction="LONG")), "recommendation": "SELL"}))


def test_the_live_figure_is_reported_and_decides_nothing():
    plain = dataset()
    live = [Row(**{**vars(r), "live_decision": "SKIP"}) for r in plain]
    live[0] = Row(**{**vars(live[0]), "live_decision": "TAKE"})
    assert se.evaluate(plain).result == se.evaluate(live).result == se.PASS
    assert se.evaluate(live).stats["live_reference"]["takes"] == 1
    assert se.evaluate(live).stats["live_reference"]["rows"] == len(live)


def test_the_bootstrap_is_deterministic_with_its_seed_and_ignores_row_order():
    noise = random.Random(3)
    rows = [Row(**{**vars(r), "net_r": r.net_r + noise.gauss(0, 1)}) for r in dataset(up=(0.2, 0.0), down=(0.2, 0.0))]
    first = se.bootstrap_lower_bound(rows)
    shuffled = list(rows)
    random.Random(1).shuffle(shuffled)
    assert se.bootstrap_lower_bound(rows) == first == se.bootstrap_lower_bound(shuffled)
    assert se.bootstrap_lower_bound(rows, seed=7) != first


def test_the_bootstrap_resamples_whole_days():
    """All the edge on one day of 21. Resampling rows would see a tight positive difference; a resample of days misses
    that day about a third of the time, so its 10th percentile holds no edge at all."""
    rows = [row(d, i < 5, 0.0, se.UP) for d in range(1, 21) for i in range(10)]
    rows += [row(0, i < 50, 1.0 if i < 50 else 0.0, se.UP) for i in range(100)]
    assert sum(r.net_r for r in rows if se.is_take(r)) / sum(se.is_take(r) for r in rows) > 0.3
    assert se.bootstrap_lower_bound(rows) <= 0
    assert se.bootstrap_lower_bound([]) is None


HOUR = 3600 * 10**9


@pytest.mark.parametrize(("then", "now", "regime"), [(100, 110, se.UP), (110, 100, se.DOWN), (100, 100, None)])
def test_the_regime_is_the_sign_of_the_24h_close_change(then, now, regime):
    bars = [(i * HOUR, then) for i in range(25)] + [(25 * HOUR, now)]
    assert se.regime_at(bars, 25 * HOUR + 1) == regime


def test_the_regime_needs_bars_at_both_ends():
    bars = [(i * HOUR, 100.0) for i in range(30, 60)]
    assert se.regime_at(bars, 40 * HOUR) is None  # nothing 24h earlier
    assert se.regime_at(bars, 70 * HOUR) is None  # the newest bar is too old
    assert se.regime_at([], 70 * HOUR) is None


def test_nothing_promotes_the_prompt():
    """Invariant 3: the evaluation reads rows and returns a value; it imports nothing that can act."""
    source = (ROOT / "services/debate_worker/shadow_eval.py").read_text()
    for word in ("redis", "asyncpg", "gateway", "UPDATE ", "INSERT "):
        assert word not in source
    for path in (ROOT / "services").rglob("*.py"):
        if path.name not in ("shadow_debate.py", "shadow_eval.py"):
            assert "shadow_eval" not in path.read_text(errors="ignore"), path
    assert "prompt_version" not in (ROOT / "services/debate_worker/main.py").read_text()


# ------------------------------------------------------------------------------------ the report


class FakeLake:
    def query_bars_df(self, symbol, timeframe, start_ns, end_ns):
        assert timeframe == "1h" and "-PERP" not in symbol
        import polars as pl
        stamps = list(range(start_ns - start_ns % HOUR, end_ns + 1, HOUR))
        return pl.DataFrame({"ts_event_ns": stamps, "close": [100.0 + i for i in range(len(stamps))]},
                            schema={"ts_event_ns": pl.Int64, "close": pl.Float64})


def load_report():
    spec = importlib.util.spec_from_file_location("shadow_prompt_report", ROOT / "scripts/shadow_prompt_report.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def test_the_report_prints_the_verdict_counts_regimes_and_spend_and_writes_nothing(h):
    cid = await candidate(h)
    await h.pool.execute("UPDATE signal_candidates SET bar_close_inclusive_ns = $2 WHERE id = $1", cid, 1000 * HOUR)
    await h.pool.execute("INSERT INTO shadow_debate_verdicts (candidate_id, run_id, prompt_version, state, recommendation, cost_usd) "
                         "VALUES ($1, $2, 'shadow-v1', 'OK', 'BUY', 0.0031)", cid, uuid.uuid4())
    await h.pool.execute("INSERT INTO candidate_outcomes (candidate_id, direction, outcome, net_r_live) "
                         "VALUES ($1, 'LONG', 'TARGET', 0.8)", cid)
    await h.pool.execute("UPDATE policy_decisions SET reason = reason WHERE candidate_id = $1", cid)
    before = await h.pool.fetchval("SELECT to_jsonb(s)::text FROM shadow_debate_verdicts s WHERE candidate_id = $1", cid)
    text = await load_report().report(h.pool, FakeLake())
    assert "VERDICT NOT_ENOUGH_DATA" in text and "1 labelled rows, need 100" in text
    assert "1 usable over 1 days" in text and "$0.0031 today" in text
    assert "UP       rows    1  TAKE    1" in text and "live policy B" in text
    assert before == await h.pool.fetchval("SELECT to_jsonb(s)::text FROM shadow_debate_verdicts s WHERE candidate_id = $1", cid)


def test_the_report_is_read_only_and_documents_the_stdin_run():
    source = (ROOT / "scripts/shadow_prompt_report.py").read_text()
    assert "readonly=True" in source and "exec -T learner python - < scripts/shadow_prompt_report.py" in source
    for word in ("INSERT ", "UPDATE ", "DELETE ", "execute("):
        assert word not in source
