"""H15 PR C: an Auto paper profile trades a setup that would have become a card. Anything else still gets the card."""

import json
import uuid

import pytest
from cryptonite_execution import profile_lifecycle
from test_debate_gate import Models
from test_setup_proposals import (  # noqa: F401
    HOLD_LITE,
    PERP,
    SPOT,
    PublishRedis,
    candidate,
    counts,
    create,
    db_url,
    h,
    hold,
    main,
    profile,
    proposals,
    run_debate,
    scanner_payload,
)

pytestmark = pytest.mark.usefixtures("complete_lake_bars")
AUTO = {"level": 3, "markets": ("PERP",), "max_leverage": 3}


@pytest.fixture(autouse=True)
def ai_triage_on(monkeypatch):
    monkeypatch.setenv("DEBATE_AI_TRIAGE", "1")


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(proposals.RULE_ENTRIES_FLAG, "1")


async def switch(h, on: bool) -> None:  # noqa: F811
    await h.pool.execute("INSERT INTO system_settings (key, value) VALUES ('autotrade.enabled', $1::jsonb) "
                         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", "1" if on else "0")
    profile_lifecycle.reset_switch_cache()


async def open_position(h, m, side="LONG"):  # noqa: F811
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               current_price, quantity, status, leverage, margin_mode)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, $4, 60000, 60000, 0.01, 'OPEN', 2, 'ISOLATED')""",
        uuid.uuid4().hex, m.instance_id, PERP, side)


async def commands(h):  # noqa: F811
    return await h.pool.fetch("SELECT payload, status FROM execution_commands")


async def cards(h):  # noqa: F811
    return await h.pool.fetchval("SELECT count(*) FROM setup_proposals")


async def test_an_auto_paper_profile_places_one_tagged_order_with_its_stop_and_no_card(h, flag_on):  # noqa: F811
    await switch(h, True)
    m = await profile(h, **AUTO)
    debate = hold(await candidate(h))
    assert await create(h, debate, m) is None
    rows = await commands(h)
    assert len(rows) == 1 and await cards(h) == 0
    payload = json.loads(rows[0]["payload"])
    assert payload["entry_source"] == proposals.RULE_ENTRY_TAG and payload["side"] == "BUY"
    assert payload["stop_loss"] and payload["take_profit"] and payload["instrument_id"] == PERP
    assert payload["run_id"] == str(debate.run_id) and payload["setup_grade"] in ("A", "B", "C")
    route = await h.pool.fetchrow("SELECT outcome, detail FROM ai_profile_decisions WHERE stage = 'ROUTE'")
    assert route["outcome"] == "ENQUEUED" and proposals.RULE_ENTRY_TAG in json.dumps(route["detail"])
    assert not await h.pool.fetchval("SELECT count(*) FROM ai_profile_decisions WHERE stage = 'DEBATE'")


async def test_a_redelivery_places_no_second_order(h, flag_on):  # noqa: F811
    await switch(h, True)
    m = await profile(h, **AUTO)
    debate = hold(await candidate(h))
    await create(h, debate, m)
    await create(h, debate, m)
    assert len(await commands(h)) == 1


@pytest.mark.parametrize(("flag", "level", "on"), [("1", 1, True), ("1", 2, True), ("1", 3, False), ("0", 3, True)],
                         ids=["ask", "veto", "switch-off", "flag-off"])
async def test_anything_else_still_gets_a_card_and_no_order(h, monkeypatch, flag, level, on):  # noqa: F811
    monkeypatch.setenv(proposals.RULE_ENTRIES_FLAG, flag)
    await switch(h, on)
    m = await profile(h, **{**AUTO, "level": level})
    assert await create(h, hold(await candidate(h)), m) is not None
    assert await cards(h) == 1 and await counts(h) == (0, 0)


async def test_a_non_paper_profile_never_trades_by_rule(h, flag_on):  # noqa: F811
    await switch(h, True)
    m = await profile(h, **AUTO)
    live = m.model_copy(update={"environment": "LIVE"})
    assert await proposals.trades_by_rule(None, live) is False


@pytest.mark.parametrize("over", [
    {"non_actionable": "EVIDENCE_INCOMPLETE: bars"}, {"stop_loss": None}, {"take_profit": 60500.0},
    {"trigger_source": "brain_on_demand"}, {"recommendation": "BUY"}], ids=str)
async def test_every_skip_reason_places_nothing(h, flag_on, over):  # noqa: F811
    await switch(h, True)
    m = await profile(h, **AUTO)
    await create(h, hold(await candidate(h), **over), m)
    assert await counts(h) == (0, 0) and await cards(h) == 0


async def test_policy_a_no_trade_places_nothing(h, flag_on):  # noqa: F811
    await switch(h, True)
    m = await profile(h, **AUTO)
    await create(h, hold(await candidate(h, decision="SKIP", reason="GRADE_NO_TRADE")), m)
    assert await counts(h) == (0, 0)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
async def test_an_open_position_on_the_coin_blocks_it_either_way(h, flag_on, side):  # noqa: F811
    await switch(h, True)
    m = await profile(h, **AUTO)
    await open_position(h, m, side)
    await create(h, hold(await candidate(h)), m)
    assert await counts(h) == (0, 0) and await cards(h) == 0


async def test_a_queued_command_blocks_a_second_entry_and_is_recorded(h, flag_on):  # noqa: F811
    await switch(h, True)
    m = await profile(h, **AUTO)
    await create(h, hold(await candidate(h)), m)
    second = await candidate(h)
    await create(h, hold(second), m)
    assert len(await commands(h)) == 1
    assert await h.pool.fetchval("SELECT reason FROM candidate_funnel_stops WHERE candidate_id = $1", second) == "ENTRY_ALREADY_QUEUED"


async def test_a_route_refusal_is_recorded_with_no_retry_and_no_card(h, flag_on, monkeypatch):  # noqa: F811
    await switch(h, True)
    m = await profile(h, **AUTO)
    cid = await candidate(h)
    calls = []

    async def refuse(*a, **k):
        calls.append(a)
        return "NO_TRADE", m
    monkeypatch.setattr(proposals, "route_entry", refuse)
    await create(h, hold(cid), m)
    assert len(calls) == 1 and await counts(h) == (0, 0) and await cards(h) == 0
    assert await h.pool.fetchval("SELECT reason FROM candidate_funnel_stops WHERE candidate_id = $1", cid) == "RULE_ENTRY_NO_TRADE"


async def test_e2e_a_real_debate_hold_trades_by_rule_and_keeps_its_verdict(main, h, flag_on):  # noqa: F811
    await switch(h, True)
    await profile(h, **AUTO)
    cid = await candidate(h)
    redis = PublishRedis()
    redis.kv["perp:mark:BTCUSDT"] = json.dumps({"type": "perp.mark", "mark_price": "65000", "funding_rate": "0.0001",
                                                "ts_event_ns": 1})
    run = await run_debate(main, h, scanner_payload(cid), Models(lite=HOLD_LITE), redis)
    assert run["status"] == "NO_TRADE"
    rows = await commands(h)
    assert len(rows) == 1 and json.loads(rows[0]["payload"])["entry_source"] == proposals.RULE_ENTRY_TAG
    b = await h.pool.fetchrow("SELECT decision, reason FROM policy_decisions WHERE candidate_id = $1 AND policy = 'B'", cid)
    assert (b["decision"], b["reason"]) == ("SKIP", "DEBATE_HOLD")
