"""H10 scope addition: four findings from the 2026-11-03 Brain session 391c04c0.

1. Candidate e6d45e6d (XRPUSDT 15m, entry 1.2599, stop 1.2541, target 1.2686) was debated HOLD at conviction 0.0 by the
   automatic path (lite run 82d44582; policies A=TAKE, C=TAKE, B=SKIP:DEBATE_HOLD), yet the Brain listed it as tradeable
   12 minutes later and a BUY card filled with no verdict on it.
2. The Brain and the approval note said the card's stop was not used; the exit monitor enforces it.
3. An INVALID profile proposal's COIN_LOCKED never reached the reply, the 1% minimum check never ran without the API,
   13m was silently scanned as 15m, and "no setups on any timeframe" followed a 1h-only scan.
4. A closed Brain-card trade (ETH, STRUCTURAL_STOP, -$20.03) had no lesson: the learner reflected debate trades only.
"""
import json
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import httpx
import polars as pl
import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_agents import profile_tools, tools
from cryptonite_agents.brain.agent import Brain, profile_invalid_note, scan_notes
from cryptonite_agents.brain.widgets import OrderCardWidget, SwingCandidateItem
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import EMBED, FLASH, FLASH_LITE, PRO
from cryptonite_api.routes.brain import protection_text
from cryptonite_schemas.marks import quote_tick_key
from migrated_db import migrated_database
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from test_agent_safe_trade_timeline import (
    account,
    approval,
    command,
    instance,
    mandate,
    mandate_instance,
    order,
    position,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "debate_worker"))
import reflection  # noqa: E402

XRP = "XRPUSDT.BINANCE"
BTC = "BTCUSDT.BINANCE"
CANDIDATE = "e6d45e6d-4167-4400-af63-6813c62a7ded"
HOLD_RUN = "82d44582-0000-4000-8000-000000000000"
HOLD_VERDICT = {"run_id": HOLD_RUN, "instrument_id": XRP, "status": "NO_TRADE", "trigger_source": "scanner",
                "created_at": "2026-11-03T11:15:40+00:00", "recommendation": "HOLD", "conviction_score": 0.0,
                "failure_reason": None, "candidate_id": CANDIDATE, "side": "LONG",
                "policies": {"A": "TAKE", "B": "SKIP:DEBATE_HOLD", "C": "TAKE"}}
XRP_SETUP = {"instrument_id": XRP, "preset_slug": "swing-sniper-pullback-15m", "timeframe": "15m", "score": 60,
             "direction": "LONG", "entry": 1.2599, "stop": 1.2541, "target": 1.2686, "rr_ratio": 1.5,
             "source": "scanner", "age_minutes": 12, "candidate_id": CANDIDATE}


# --- 1. the automatic path's verdict on the same candidate ---------------------------------------------------------

def test_a_setup_the_debate_held_is_listed_as_debated_hold_with_its_verdict(monkeypatch):
    monkeypatch.setattr(tools, "recent_debate_verdicts", lambda insts, window_h=4: [HOLD_VERDICT])
    setup = dict(XRP_SETUP)
    tools.annotate_with_debates([setup])
    assert setup["debate_status"] == "debated: HOLD"
    assert setup["debate"]["run_id"] == HOLD_RUN and setup["debate"]["conviction_score"] == 0.0
    assert setup["debate"]["policies"] == {"A": "TAKE", "B": "SKIP:DEBATE_HOLD", "C": "TAKE"}
    assert setup["debate"]["against"] is True
    SwingCandidateItem(**setup)  # the radar widget accepts the annotated setup


def test_the_same_coin_and_side_within_the_window_counts_and_the_other_side_does_not():
    other = dict(XRP_SETUP, candidate_id=None)
    assert tools.debate_link([HOLD_VERDICT], XRP, "LONG")["against"] is True
    assert tools.debate_link([HOLD_VERDICT], "XRPUSDT-PERP.BINANCE", "LONG")["run_id"] == HOLD_RUN
    assert tools.debate_link([HOLD_VERDICT], XRP, "SHORT") is None
    assert tools.debate_link([{**HOLD_VERDICT, "side": None}], XRP, other["direction"]) is None  # operator: own only


def test_an_unreadable_verdict_is_said_never_read_as_not_debated(monkeypatch):
    def boom(insts, window_h=4):
        raise ConnectionError("db down")
    monkeypatch.setattr(tools, "recent_debate_verdicts", boom)
    setup = dict(XRP_SETUP)
    tools.annotate_with_debates([setup])
    assert setup["debate_status"].startswith("unknown")


async def test_a_card_against_the_debate_hold_is_flagged_and_noted():
    async def link(card):
        return tools.debate_link([HOLD_VERDICT], card["instrument_id"], "LONG")
    brain = Brain(gateway=None, store=None, debates=link)
    got, against = await brain._card_debate({"id": "c1", "instrument_id": XRP, "side": "BUY"})
    assert got["run_id"] == HOLD_RUN
    assert against.startswith("against the debate verdict: debate 82d44582") and "ended HOLD (conviction 0.0)" in against
    widget = OrderCardWidget(id="c1", instrument_id=XRP, side="BUY", allocation_pct=5, debate_link=got,
                             against_debate_verdict=against)
    assert widget.against_debate_verdict == against


async def test_a_failed_verdict_read_still_needs_the_acknowledgement():
    async def boom(card):
        raise TimeoutError
    _, against = await Brain(gateway=None, store=None, debates=boom)._card_debate({"id": "c1"})
    assert against.startswith("UNKNOWN")


# --- approval of a card against the verdict (real migrated Postgres, the API route) ----------------------------------

@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_h10_brain") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE paper_accounts SET total_equity = 1000000")
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_positions CASCADE")
    await harness.redis.set(quote_tick_key(BTC), json.dumps({
        "instrument_id": BTC, "bid_price": "59999", "ask_price": "60001", "arrival_ns": time.time_ns()}), ex=60)
    try:
        yield harness
    finally:
        await harness.redis.delete(quote_tick_key(BTC))
        await close_harness(harness)


@pytest.fixture
def against_card(monkeypatch):
    from cryptonite_api.routes import brain as brain_routes

    card = {"kind": "order_card", "id": "card-1", "instrument_id": BTC, "side": "BUY", "order_type": "MARKET",
            "limit_price": None, "allocation_pct": 5.0, "stop_loss": 58000.0, "take_profit": None,
            "status": "PENDING_APPROVAL", "trace_id": str(uuid.uuid4()), "command_id": None,
            "debate_link": {**HOLD_VERDICT, "instrument_id": BTC},
            "against_debate_verdict": "against the debate verdict: debate 82d44582 ended HOLD (conviction 0.0)"}
    notes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"session": {}, "messages": [{"role": "assistant", "widgets": [card]}]})
        body = json.loads(request.content)
        notes.append(body)
        if body.get("expect_status") and card["status"] != body["expect_status"]:
            return httpx.Response(409)
        card.update({k: body[k] for k in ("status", "command_id") if body.get(k)})
        return httpx.Response(200, json={"status": "ok"})

    real = httpx.AsyncClient
    monkeypatch.setattr(brain_routes.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return card, notes


async def test_a_card_against_a_debate_hold_needs_an_explicit_acknowledgement(h, against_card):
    card, notes = against_card
    await h.mandate(instruments=(BTC,))
    path = f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/card-1/approve"
    async with h.client() as c:
        refused = await c.post(path, headers=h.headers)
        assert refused.status_code == 409 and refused.json()["detail"].startswith("AGAINST_DEBATE_VERDICT")
        assert card["status"] == "PENDING_APPROVAL" and notes == []
        assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
        ok = await c.post(path, json={"acknowledge_against_debate": True}, headers=h.headers)
    assert ok.status_code == 200, ok.text
    payload = await h.pool.fetchval("SELECT payload FROM execution_commands WHERE command_id = $1",
                                    uuid.UUID(ok.json()["command_id"]))
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload["acknowledged_against_debate"] is True and payload["debate_link"]["run_id"] == HOLD_RUN
    # 2. the queued note states the effective stop: the structural stop, the exit monitor's stops as backstops
    queued = notes[-1]["content"]
    assert "the active stop is the structural stop at 58000" in queued and "are backstops" in queued


# --- 2. the effective stop ---------------------------------------------------------------------------------------------

def test_the_xrp_card_states_its_structural_stop_as_the_active_stop():
    protection = {"stop_loss_pct": 5.0, "trail_pct": 2.0, "trail_activate_pct": 3.0}
    text = protection_text({"stop_loss": 1.2541, "grade_inputs": {"entry": 1.2599}}, Decimal("1.2541"), protection, False)
    assert text.startswith("the active stop is the structural stop at 1.2541 (-0.46% from entry 1.2599)")
    assert "5.0% hard stop and 2.0% trailing stop (after +3.0%) are backstops" in text
    assert "not by" not in text
    perp = protection_text({"stop_loss": 77578.61}, None, protection, True)
    assert perp.startswith("the active stop is a resting stop at 77578.61")


def test_the_brain_prompt_no_longer_says_the_card_stop_is_unused():
    from cryptonite_agents.brain.agent import BRAIN

    assert "not by your stop price" not in BRAIN.instructions
    assert "The card's stop_loss is the active structural stop" in BRAIN.instructions


# --- 3. profile proposals and timeframes -------------------------------------------------------------------------------

def test_an_invalid_profile_proposal_lists_every_error_in_the_reply():
    note = profile_invalid_note({"kind": "profile_proposal", "status": "INVALID", "errors": [
        "COIN_LOCKED: XRP is already in profile Alt swing; edit the existing mandate instead",
        "ORDER_MIN_UNREACHABLE: on margin the largest order ($100.0) is below the 1% minimum order ($995.0)"]})
    assert "COIN_LOCKED: XRP" in note and "ORDER_MIN_UNREACHABLE" in note and note.startswith("The profile proposal is INVALID")
    assert profile_invalid_note({"kind": "profile_proposal", "status": "PROPOSED"}) is None


def test_the_minimum_order_is_checked_locally_when_the_api_cannot_be_asked(monkeypatch):
    monkeypatch.setattr(profile_tools, "_order_min_feasibility", lambda p: (None, "no agent API token"))
    monkeypatch.setattr(profile_tools, "_fed_symbols", lambda: {"XRPUSDT"})

    def rows(sql, params=None):
        if "agent_safe_account_equity" in sql:
            return [{"total_equity": Decimal("99500")}]
        if "agent_safe_mandate_health" in sql:
            return [{"id": uuid.uuid4(), "name": "Alt swing", "status": "ACTIVE", "instruments": [XRP]}]
        return []
    monkeypatch.setattr(profile_tools, "_readonly_rows", rows)
    out = profile_tools._propose({"name": "XRP margin", "coins": ["XRP"], "markets": ["SPOT_MARGIN"],
                                  "direction": "BOTH", "capital_budget_usd": 1000, "max_position_pct": 10})
    assert out["status"] == "INVALID"
    codes = [e.split(":")[0] for e in out["errors"]]
    assert "COIN_LOCKED" in codes and "ORDER_MIN_UNREACHABLE" in codes  # both, not only the first
    assert not any(w.startswith("ORDER_MIN_UNKNOWN") for w in out["warnings"])


@pytest.mark.parametrize("asked, used, noted", [("13m", "15m", True), ("15m", "15m", False), ("2h", "1h", True),
                                                ("3h", "4h", True), ("weekly", "1h", True)])
def test_an_unsupported_timeframe_names_the_nearest_supported_one(asked, used, noted):
    tf, note = tools.nearest_scan_timeframe(asked)
    assert tf == used and (note is not None) is noted
    if noted and asked == "13m":
        assert note == ("Timeframe 13m is not supported; the nearest supported timeframe, 15m, was scanned instead "
                        "(supported: 15m, 1h, 4h).")


def test_the_reply_states_which_timeframes_were_scanned():
    content = json.dumps({"candidates": [], "scanned_timeframes": ["1h"],
                          "timeframe_note": "Timeframe 13m is not supported; ...",
                          "scope_note": "Scanned MUBARAKUSDT with live presets on: 1h; plus stored scanner setups of "
                                        "the last 4 h. Other timeframes were not scanned, so nothing is claimed about them."})
    notes = scan_notes(content)
    assert notes[0].startswith("Timeframe 13m is not supported") and "live presets on: 1h" in notes[1]
    assert scan_notes("not json") == []


# --- 4. Brain-card trades produce TRADE lessons ------------------------------------------------------------------------

class _OneConn:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


class _EmptyLake:
    def query_bars_df(self, **kw):
        return pl.DataFrame()


class _Embed:
    async def embed_texts(self, texts):
        return [[0.0] * 768 for _ in texts], 1


async def test_a_closed_brain_card_trade_gets_a_trade_lesson_once(db_url):
    conn = await asyncpg.connect(db_url)
    tx = conn.transaction()
    await tx.start()
    try:
        await account(conn)
        mid = await mandate(conn, "card")
        target = mandate_instance(mid)
        await instance(conn, target)
        opened = datetime.now(UTC) - timedelta(days=1)
        entry = await command(conn, "SUBMIT_ORDER", target,
                              {"instrument_id": XRP, "side": "BUY", "brain_card_id": "card-eth",
                               "debate_link": {"candidate_id": CANDIDATE}}, "operator:tester",
                              opened - timedelta(seconds=5), mid=mid)
        await order(conn, target, f"AI-{entry.hex[:12].upper()}", XRP, "BUY", opened - timedelta(seconds=4), 1, 2300)
        await approval(conn, mid, entry, "brain_card", "sid-1:card-eth", "BUY XRPUSDT MARKET at 5% of mandate")
        await position(conn, target, opened, qty=1, entry=2300, realized=-20.03, closed_at=opened + timedelta(hours=5))
        unfilled = await command(conn, "SUBMIT_ORDER", target, {"instrument_id": "SOLUSDT.BINANCE", "side": "BUY"},
                                 "operator:tester", opened, mid=mid)
        await approval(conn, mid, unfilled, "brain_card", "sid-1:card-sol", "BUY SOL")  # never filled: no lesson

        lesson = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("The structural stop held the loss small.")]))
        engine = reflection.ReflectionEngine(_OneConn(conn), _EmptyLake(), Gateway())
        with override_model(FLASH, lesson), override_model(PRO, lesson), override_model(FLASH_LITE, lesson), \
                override_model(EMBED, _Embed()):
            assert await engine.process_card_trade_reflections() == 1
            assert await engine.process_card_trade_reflections() == 0  # once per entry command
        r = await conn.fetchrow("SELECT * FROM reflections WHERE entry_command_id = $1", entry)
        assert (r["kind"], r["run_id"], r["brain_card_id"], str(r["candidate_id"])) == ("TRADE", None, "card-eth",
                                                                                         CANDIDATE)
        assert r["outcome"] == "LOSS" and r["instrument_type"] == XRP
        assert not r["lessons_learned"].startswith(reflection.WHAT_IF_LABEL)
        shown = await conn.fetchval("SELECT reflection_text FROM agent_safe_trade_timeline WHERE entry_command_id = $1",
                                    entry)
        assert shown == "The structural stop held the loss small."
        safe = await conn.fetchrow("SELECT instrument_id, kind FROM agent_safe_reflections WHERE entry_command_id = $1",
                                   entry)
        assert (safe["instrument_id"], safe["kind"]) == (XRP, "TRADE")
    finally:
        await tx.rollback()
        await conn.close()
