"""H10: one evidence snapshot per debate, facts from code, action-based verdicts, essential vs optional evidence.

Production 2026-11-02/03: a BTCUSDT spot debate told the analysts, Bull, Bear and PM there was no position while a BTC
perp 2x short was open (only the CRO saw it); the Bear persona said 'HOLD (no position)'; a single plausible number
passed the bar check and later roles were never checked; funding reached the agents as a raw 0.0001 and the Bear wrote
both '0.0001 (0.01%)' and '0.0001%' (debate b051ecd2). The regression tests are named after those failures; the four
``test_e2e_*`` cases drive the real worker against the CI Postgres (models stubbed), then read the app's evidence
endpoint, the persisted snapshot and verdict, the routing outcome and the approval state.
"""
import datetime
import json
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_agents import tools
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import EMBED, FLASH, FLASH_LITE, PRO
from cryptonite_execution.related_positions import opposite_positions, read_related_positions
from cryptonite_schemas.reward_risk import meets_min_rr, reward_to_risk
from migrated_db import migrated_database
from test_debate_gate import FakeEmbedder, Models, load
from test_debate_perp_gate import CapRedis

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "debate_worker"))
import evidence  # noqa: E402
import reflection  # noqa: E402

pytestmark = pytest.mark.usefixtures("complete_lake_bars")

SPOT = "BTCUSDT.BINANCE"
PERP = "BTCUSDT-PERP.BINANCE"
SHORT_LABEL = "Existing BTC perp SHORT 2x open"
REAL_SCANNER_EVIDENCE = tools.scanner_evidence  # captured before complete_lake_bars stubs it


@pytest.fixture(autouse=True)
def ai_triage_on(monkeypatch):
    monkeypatch.setenv("DEBATE_AI_TRIAGE", "1")


@pytest.fixture(scope="module")
def main():
    return load("debate_worker_main_h10", "main.py")


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_h10") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("TRUNCATE ai_profile_coin_locks, ai_profile_decisions, ai_approvals, "
                               "execution_commands, paper_positions, debate_runs CASCADE")
    await harness.pool.execute("UPDATE ai_mandates SET status='ARCHIVED'")
    await harness.pool.execute("UPDATE paper_accounts SET total_equity=10000, cash_balance=10000")
    from cryptonite_execution import profile_lifecycle
    profile_lifecycle.reset_switch_cache()
    try:
        yield harness
    finally:
        await close_harness(harness)


async def spot_profile(h):
    """An Ask me (L1) spot profile that owns BTC."""
    mid = await h.pool.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, status, autonomy_level,
           markets, max_leverage, direction, style, allow_perps)
           VALUES ($1, 10000, ARRAY[$2], 25, 'ACTIVE', 1, ARRAY['SPOT'], 1, 'LONG', 'SWING', false) RETURNING id""",
        f"h10-{uuid.uuid4().hex[:6]}", SPOT)
    await h.pool.execute("INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('BTC', $1)", mid)
    from cryptonite_execution.mandate_store import load_mandate
    from cryptonite_orchestrator.mandate_gate import ensure_mandate_instance
    async with h.pool.acquire() as conn:
        m = await load_mandate(conn, mid)
        await ensure_mandate_instance(conn, m, "PAPER-ACCOUNT-001")
    return m


async def open_perp_short(h, instance_id: str, redis: CapRedis):
    """The production position: BTC perp 2x short 0.02598 @ 76979.4, stop 77578.61 resting, TP 75581.41."""
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
           current_price, quantity, status, leverage, margin_mode)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'SHORT', 76979.4, 76900, 0.02598, 'OPEN', 2, 'ISOLATED')""",
        uuid.uuid4().hex, instance_id, PERP)
    redis.kv[f"exit:plan:{PERP}:{instance_id}"] = json.dumps({"stop_price": 77578.61, "take_profit_price": 75581.41})


def operator_payload() -> dict:
    return {"instrument_id": SPOT, "trigger_source": "user_on_demand"}


def scanner_payload_with_levels() -> dict:
    close_ns = int((time.time() - 60) * 1e9)
    return {"instrument_id": SPOT, "trigger_source": "scanner", "brief_key": "brief:k",
            "candidate_payload": json.dumps({"timeframe": "1h", "bar_close_inclusive_ns": close_ns,
                                             "suggested_entry": 65010,
                                             "suggested_stop": 64000, "suggested_target": 67500})}


async def run_debate(main, h, payload, models, redis):
    m = models.model()
    with override_model(FLASH, m), override_model(PRO, m), override_model(FLASH_LITE, m), \
            override_model(EMBED, FakeEmbedder()):
        await main.process_debate_trigger(h.pool, redis, payload, Gateway(pool=h.pool))
    return await h.pool.fetchrow("SELECT * FROM debate_runs ORDER BY created_at DESC LIMIT 1")


async def app_evidence(h, run_id) -> dict:
    async with h.client() as c:
        resp = await c.get(f"/api/v1/agents/debates/{run_id}/evidence")
    assert resp.status_code == 200, resp.text
    return resp.json()


class FullModels(Models):
    """Models that also keep every request's full prompt text (``Models`` keeps a repr, which truncates)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.full: list[str] = []

    def model(self):
        inner = super().model()

        def fn(messages, info):
            self.full.append("\n".join(str(getattr(part, "content", "")) for m in messages
                                        for part in getattr(m, "parts", [])))
            return inner.function(messages, info)
        from pydantic_ai.models.function import FunctionModel
        return FunctionModel(fn)


def all_prompts(models: FullModels) -> list[str]:
    return models.full


# --- unit fixtures ------------------------------------------------------------------------------------

def snap_with(**items) -> dict:
    """A minimal entry snapshot: every essential item OK unless overridden."""
    base = {
        "quote": evidence.item("OK", 65010.0, source="quote", unit="USD"),
        "positions": evidence.item("OK", [], source="paper_positions"),
        "profile": evidence.item("OK", None, source="ai_profile_coin_locks"),
        "bars": evidence.item("OK", {"timeframe": "1h", "expected": 48, "returned": 48, "missing": 0,
                                     "complete": True, "newest_bar_close": None, "newest_bar_age_minutes": 1,
                                     "summary": None}, source="lake"),
    }
    base.update(items)
    return {"schema_version": 1, "version": 1, "purpose": "ENTRY", "instrument_id": SPOT, "market": "SPOT",
            "related_instruments": {}, "built_at": "x", "items": base, "optional": list(evidence.OPTIONAL["ENTRY"])}


PERP_SHORT = {"instrument_id": PERP, "market": "PERP", "side": "SHORT", "quantity": 0.02598, "entry_price": 76979.4,
              "leverage": 2, "stop": 77578.61, "take_profit": 75581.41, "owner_instance": "x", "mandate_id": None,
              "mandate_name": None, "opened_at": None}


# --- regression 1: stale historical rescan rows -------------------------------------------------------

def test_stale_historical_rescan_rows_are_not_fresh_scanner_evidence(monkeypatch):
    now = time.time()
    row = {"timeframe": "1h", "bar_close_inclusive_ns": int((now - 600) * 1e9), "suggested_entry": 100.0,
           "suggested_stop": 98.0, "suggested_target": 106.0, "metrics": {},
           "created_at": datetime.datetime.fromtimestamp(now - 590, tz=datetime.UTC),
           "trigger_reason": "HISTORICAL_PRIMING"}
    monkeypatch.setattr(tools, "_scanner_rows", lambda inst: [row])
    it = evidence._scanner_item(REAL_SCANNER_EVIDENCE(SPOT))
    assert it["value"] == [] and it["status"] == "OK"
    assert "none fresh" in evidence.scanner_evidence_block(it)


# --- regression 2: missing or incomplete bars --------------------------------------------------------

def test_incomplete_bars_make_the_entry_debate_non_actionable():
    bars = evidence._bars_item({"timeframe": "1h", "expected": 48, "returned": 40, "missing": 8, "complete": False,
                                "reason": "8 bars missing", "bars": []})
    snap = snap_with(bars=bars)
    assert bars["status"] == "MISSING"
    assert evidence.non_actionable_reason(snap).startswith("EVIDENCE_INCOMPLETE: essential evidence not usable (bars")
    assert "NON-ACTIONABLE" in evidence.verified_facts(snap)


async def test_missing_bars_force_hold_through_the_worker(main, h, monkeypatch):
    def _no_bars(instrument_id, timeframe="1h", n=48, now_ns=None):
        return {"instrument_id": instrument_id, "timeframe": timeframe, "expected": n, "returned": 0, "missing": n,
                "complete": False, "newest_bar_close": None, "newest_bar_age_minutes": None, "bars": [],
                "reason": "no closed 1h bars in the lake for the last 48 bars"}
    monkeypatch.setattr(tools, "recent_bars", _no_bars)
    await spot_profile(h)
    run = await run_debate(main, h, scanner_payload_with_levels(), Models(), CapRedis())
    decision = await h.pool.fetchrow("SELECT * FROM portfolio_decisions WHERE run_id = $1", run["id"])
    assert (decision["recommendation"], run["status"], run["failure_reason"]) == ("HOLD", "NO_TRADE",
                                                                                  "EVIDENCE_INCOMPLETE")
    assert not await h.pool.fetchval("SELECT essential_ok FROM debate_evidence_snapshots WHERE run_id = $1", run["id"])
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0


# --- regression 3: the exact 1.5R setup ----------------------------------------------------------------

def test_the_1_4999999999999818_rr_setup_keeps_exact_levels_in_the_snapshot():
    entry, stop, target = 76779.73, 77578.61, 75581.41
    assert meets_min_rr(reward_to_risk(entry, stop, target))
    it = evidence._scanner_item({"scanner_instrument": SPOT, "fresh_setups": [
        {"timeframe": "1h", "side": "SHORT", "entry": entry, "stop": stop, "target": target, "age_minutes": 20}]})
    block = evidence.scanner_evidence_block(it)
    assert "76779.73" in block and "77578.61" in block and "75581.41" in block


# --- regression 4: funding units -------------------------------------------------------------------------

def funding_snap(raw=0.0001):
    deriv = {"funding_rate": {"value": raw, "as_of": "2026-11-03T08:00:00+00:00", "age_s": 600.0,
                              "kind": "last 8h settlement", "next_settlement_at": "2026-11-03T16:00:00+00:00"},
             "open_interest": {"unavailable": "x"}, "mark_price": {"unavailable": "x"}}
    return snap_with(**evidence._derivative_items(deriv))


def test_funding_is_rendered_as_a_code_computed_percentage():
    snap = funding_snap()
    assert snap["items"]["funding"]["value"]["rate_pct"] == 0.01
    assert "Funding: 0.01% per 8h (raw rate 0.0001" in evidence.verified_facts(snap)


def test_the_funding_unit_contradiction_0_0001_pct_vs_0_01_pct_is_flagged():
    snap = funding_snap()
    bear = ("Funding is 0.0001 (0.01%), neutral. If funding rises above 0.01% it would be crowded, "
            "not just 0.0001% as today.")
    flags = evidence.validate_turn("Bear Researcher", bear, snap)
    assert [(f["code"], f["severity"]) for f in flags] == [("FUNDING_UNIT_MISMATCH", "WARN")]
    assert evidence.validate_turn("Bear Researcher", "Funding is 0.01% per 8h (raw 0.0001).", snap) == []
    assert evidence.contradiction_reason(flags) is None  # funding is optional evidence: a warning, not a block


# --- regression 5: an open BTC perp SHORT during a BTCUSDT spot entry debate ------------------------------

def test_a_no_position_claim_is_an_essential_contradiction_when_the_perp_short_is_open():
    snap = snap_with(positions=evidence.item("OK", [PERP_SHORT], source="paper_positions"))
    flags = evidence.validate_turn("Bear Researcher", "I argue for HOLD (no position) here.", snap)
    assert [(f["code"], f["severity"]) for f in flags] == [("NO_POSITION_CLAIM", "BLOCK")]
    assert evidence.contradiction_reason(flags).startswith("EVIDENCE_CONTRADICTED: Bear Researcher NO_POSITION_CLAIM")
    # a true claim about a market the coin holds nothing in, and the HOLD wording, are not flagged
    assert evidence.validate_turn("PM", "There is no position on the spot market; HOLD adds no new position.",
                                  snap) == []
    facts = evidence.verified_facts(snap)
    assert SHORT_LABEL in facts and "decides only whether to ADD a new position" in facts
    assert "HOLD in this debate means: Add no new position" in facts


async def test_e2e_existing_position_every_role_knows_and_hold_reads_add_no_new_position(main, h):
    m = await spot_profile(h)
    redis = CapRedis()
    await open_perp_short(h, m.instance_id, redis)
    models = FullModels(pm={"recommendation": "HOLD", "conviction_score": 0.6, "allocation_pct": 0.0, "reasoning": "wait"},
                    cro={"final_allocation_pct": 0.0, "conviction_level": "Hold", "risk_reasoning": "agree"})
    run = await run_debate(main, h, operator_payload(), models, redis)
    prompts = all_prompts(models)
    assert len(prompts) == 6  # 2 analysts, Bull, Bear, PM, risk panel: every one of them
    assert all(SHORT_LABEL in p for p in prompts)
    assert all("stop 77578.61" in p and "take profit 75581.41" in p for p in prompts)
    out = await app_evidence(h, run["id"])
    assert out["verdict"]["action_label"] == "Add no new position"
    assert out["evidence_status"] == "OK" and out["routing"]["reason"] == "HOLD"
    pos = out["snapshot"]["items"]["positions"]["value"]
    assert [(p["instrument_id"], p["side"], p["leverage"], p["stop"]) for p in pos] == [(PERP, "SHORT", 2, 77578.61)]
    assert out["approval"] is None and run["status"] == "NO_TRADE"


async def test_e2e_a_bear_writing_hold_no_position_makes_the_debate_non_actionable(main, h):
    m = await spot_profile(h)
    redis = CapRedis()
    await open_perp_short(h, m.instance_id, redis)
    run = await run_debate(main, h, operator_payload(), FullModels(analyst_text="I argue for HOLD (no position)."), redis)
    out = await app_evidence(h, run["id"])
    assert out["evidence_status"] == "NON_ACTIONABLE" and run["failure_reason"] == "EVIDENCE_CONTRADICTED"
    assert out["verdict"]["recommendation"] == "HOLD"
    assert {f["code"] for f in out["validation"]} == {"NO_POSITION_CLAIM"}
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0


# --- regression 6: PM and CRO conflict ---------------------------------------------------------------------

async def test_a_cro_reversing_the_pm_is_clamped_to_hold_in_code(main, h):
    await spot_profile(h)
    models = FullModels(pm={"recommendation": "BUY", "conviction_score": 0.7, "allocation_pct": 8.0, "reasoning": "go"},
                    cro={"final_allocation_pct": 5.0, "conviction_level": "Sell", "risk_reasoning": "short it"})
    run = await run_debate(main, h, operator_payload(), models, CapRedis())
    d = await h.pool.fetchrow("SELECT * FROM portfolio_decisions WHERE run_id = $1", run["id"])
    assert (d["recommendation"], float(d["allocation_pct"]), float(d["conviction_score"])) == ("HOLD", 0.0, 0.0)
    assert "CRO clamp:" in d["reasoning"]
    assert (await app_evidence(h, run["id"]))["verdict"]["action_label"] == "Add no new position"


# --- regression 7: a tool error keeps ERROR status ---------------------------------------------------------

def test_a_derivatives_tool_error_is_error_status_and_only_warns():
    deriv = {f: {"unavailable": "derivatives context could not be read (TimeoutError)"}
             for f in tools.DERIVATIVES_FIELDS}
    snap = snap_with(**evidence._derivative_items(deriv))
    assert snap["items"]["funding"]["status"] == "ERROR"
    assert evidence.non_actionable_reason(snap) is None
    assert any(w.startswith("funding ERROR") for w in evidence.optional_warnings(snap))
    assert "Funding: unavailable [ERROR: derivatives context could not be read" in evidence.verified_facts(snap)


async def test_a_position_read_error_is_error_status_and_blocks():
    class Broken:
        def acquire(self):
            raise ConnectionError("db down")
    it = await evidence._positions_item(Broken(), None, SPOT)
    assert it["status"] == "ERROR"
    snap = snap_with(positions=it)
    assert evidence.non_actionable_reason(snap).startswith("EVIDENCE_INCOMPLETE")
    assert "UNRESOLVED [ERROR" in evidence.verified_facts(snap)


async def test_a_perp_debate_needs_a_fresh_mark():
    snap = snap_with(mark=evidence.item("STALE", 60000.0, source="feed", reason="mark 300 s old"))
    snap["market"] = "PERP"
    assert "mark STALE" in evidence.non_actionable_reason(snap)
    snap["market"] = "SPOT"
    assert evidence.non_actionable_reason(snap) is None


# --- regression 8: an unfilled decision is never labelled a trade ------------------------------------------

async def test_e2e_valid_proposal_reaches_an_unapproved_card_and_stays_a_what_if(main, h):
    m = await spot_profile(h)
    run = await run_debate(main, h, scanner_payload_with_levels(), Models(), CapRedis())
    assert run["status"] == "PENDING_APPROVAL" and run["mandate_id"] == m.id
    out = await app_evidence(h, run["id"])
    assert out["verdict"]["action_label"] == "BUY" and out["evidence_status"] == "OK"
    assert out["routing"]["outcome"] == "APPROVAL_CREATED"
    assert out["approval"]["status"] == "PENDING" and out["approval"]["action_class"] == "ADD_RISK"
    payload = json.loads(await h.pool.fetchval("SELECT payload FROM ai_approvals WHERE source_ref = $1",
                                               str(run["id"])))
    assert payload["proposed_stop_loss"] == 64000.0
    assert out["snapshot"]["version"] >= 1 and out["snapshot"]["purpose"] == "ENTRY"
    # never approved: no fill, so the learner reflects on it as a what-if, never a trade
    fill = await h.pool.fetchrow(reflection.RUN_FILLS_SQL.format(run="$1::uuid"), run["id"])
    assert reflection.reflection_kind(dict(fill)) == reflection.WHAT_IF
    assert "NO trade was executed" in reflection._execution_line(dict(fill))


# --- end-to-end: HOLD and a blocked proposal -------------------------------------------------------------

async def test_e2e_hold_is_add_no_new_position_with_the_snapshot_persisted(main, h):
    await spot_profile(h)
    models = FullModels(pm={"recommendation": "HOLD", "conviction_score": 0.5, "allocation_pct": 0.0, "reasoning": "mixed"},
                    cro={"final_allocation_pct": 0.0, "conviction_level": "Hold", "risk_reasoning": "fine"})
    run = await run_debate(main, h, operator_payload(), models, CapRedis())
    assert run["status"] == "NO_TRADE" and run["evidence_snapshot_version"] >= 1
    out = await app_evidence(h, run["id"])
    assert out["verdict"]["action_label"] == "Add no new position" and out["evidence_status"] == "OK"
    assert out["snapshot"]["items"]["positions"]["value"] == []
    assert out["routing"] == {"outcome": "NO_TRADE", "reason": "HOLD"} and out["approval"] is None
    # every role saw the same verified facts
    facts = [p for p in all_prompts(models) if "=== VERIFIED FACTS (evidence snapshot v1" in p]
    assert len(facts) == len(all_prompts(models))


async def test_e2e_opposite_proposal_is_blocked_with_a_named_reason(main, h):
    m = await spot_profile(h)
    redis = CapRedis()
    await open_perp_short(h, m.instance_id, redis)
    run = await run_debate(main, h, operator_payload(), Models(), redis)  # PM BUY 8%, CRO Overweight 6%
    out = await app_evidence(h, run["id"])
    assert run["status"] == "NO_TRADE" and run["failure_reason"] == "OPPOSITE_POSITION_OPEN"
    assert out["verdict"]["recommendation"] == "BUY" and out["evidence_status"] == "NON_ACTIONABLE"
    assert out["non_actionable_reason"].startswith("OPPOSITE_POSITION_OPEN: BUY is opposite to the open BTC perp SHORT 2x")
    assert out["routing"]["outcome"] == "NO_TRADE" and out["approval"] is None
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


# --- snapshot immutability, versions, old runs, related positions ------------------------------------------

async def test_snapshots_are_immutable_and_old_runs_show_no_snapshot(main, h):
    await spot_profile(h)
    run = await run_debate(main, h, operator_payload(), Models(), CapRedis())
    with pytest.raises(Exception, match="immutable"):
        await h.pool.execute("UPDATE debate_evidence_snapshots SET essential_ok = false WHERE run_id = $1", run["id"])
    old = uuid.uuid4()
    await h.pool.execute("INSERT INTO debate_runs (id, instrument_id, status, trigger_source) "
                         "VALUES ($1, $2, 'NO_TRADE', 'scanner')", old, SPOT)
    out = await app_evidence(h, old)
    assert (out["evidence_status"], out["snapshot"]) == ("NO_SNAPSHOT", None)


def test_a_changed_decision_quote_is_a_new_snapshot_version():
    snap = snap_with()
    assert evidence.refreshed(snap, SimpleNamespace(price=65010.0, age_s=1.0, source="quote")) is None
    new = evidence.refreshed(snap, SimpleNamespace(price=65100.0, age_s=1.0, source="quote"))
    assert (new["version"], new["items"]["quote"]["value"], new["refreshed"]) == (2, 65100.0, ["quote"])
    assert snap["items"]["quote"]["value"] == 65010.0  # version 1 is untouched


async def test_related_positions_keep_each_owner_and_market_separate(h):
    for inst, side, owner in ((PERP, "SHORT", "AI_DEBATE_WORKER"), (SPOT, "LONG", "MANUAL_TRADE")):
        await h.pool.execute(
            """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               current_price, quantity, status) VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, $4, 70000, 70000, 0.01,
               'OPEN')""", uuid.uuid4().hex, owner, inst, side)
    async with h.pool.acquire() as conn:
        rows = await read_related_positions(conn, "BTCUSDT-MARGIN.BINANCE", "PAPER-ACCOUNT-001")
    assert sorted((r["instrument_id"], r["side"], r["owner_instance"]) for r in rows) == [
        (PERP, "SHORT", "AI_DEBATE_WORKER"), (SPOT, "LONG", "MANUAL_TRADE")]
    assert [r["side"] for r in opposite_positions(rows, "BUY")] == ["SHORT"]
    assert [r["side"] for r in opposite_positions(rows, "SELL")] == ["LONG"]


def test_the_bear_persona_never_says_hold_no_position():
    from cryptonite_agents.debate_fleet import (
        BEAR,
        BULL,
        CHIEF_RISK_OFFICER,
        RISK_PANEL,
        research_manager,
    )
    assert "HOLD (no position)" not in BEAR.instructions
    assert "add no new position" in BEAR.instructions
    for p in (BULL, BEAR, research_manager(), RISK_PANEL, CHIEF_RISK_OFFICER):
        assert "VERIFIED FACTS" in p.instructions


def test_a_contradicted_bar_figure_blocks_and_a_matching_one_does_not():
    summary = {"net_change_pct": -1.2, "window_high": 77000.0, "window_low": 75000.0, "last_close": 75500.0,
               "lower_lows": 20, "higher_highs": 9, "first_close": 76400.0}
    bars = evidence.item("OK", {"timeframe": "1h", "expected": 48, "returned": 48, "missing": 0, "complete": True,
                                "newest_bar_close": None, "newest_bar_age_minutes": 1, "summary": summary},
                         source="lake")
    snap = snap_with(bars=bars)
    ok = "Measured: net change -1.2%, window high 77,000, window low 75000, last close 75500, 20 lower lows."
    assert evidence.validate_turn("Market Analyst", ok, snap) == []
    bad = "Measured: net change +3.5% with 31 lower lows."
    flags = evidence.validate_turn("Bull Researcher", bad, snap)
    assert [(f["code"], f["severity"]) for f in flags] == [("BAR_FIGURE_CONTRADICTION", "BLOCK")] * 2
