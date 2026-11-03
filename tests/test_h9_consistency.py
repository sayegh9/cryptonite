"""H9 (lead review of the 2026-11-02 22:19-22:29 UTC Brain sessions): one regression per conflict found."""

import asyncio
import datetime as dt
import time
from decimal import Decimal
from types import SimpleNamespace

import polars as pl
import pytest
from cryptonite_agents import profile_tools, tools
from cryptonite_agents.debate_fleet import (
    AGGRESSIVE_RISK,
    BEAR,
    BULL,
    CHIEF_RISK_OFFICER,
    CONSERVATIVE_RISK,
    NEUTRAL_RISK,
    RISK_PANEL,
)
from cryptonite_scanner.brief import _reward_to_risk, hard_rejects
from cryptonite_scanner.freshness import fresh_row_age_s
from cryptonite_scanner_service import worker as worker_mod
from cryptonite_schemas.reward_risk import meets_min_rr, reward_to_risk
from test_debate_gate import load

# The first BTC perp short (11-02 21:00 setup): the scanner's target sits at exactly 1.5R
ENTRY, STOP, TARGET = 76779.73, 77578.61, 75581.41
HOUR_NS = 3_600_000_000_000


@pytest.fixture(scope="module")
def gate():
    return load("debate_worker_gate_h9", "gate.py")


@pytest.fixture(scope="module")
def main():
    return load("debate_worker_main_h9", "main.py")


# --- 1. one R:R check everywhere ------------------------------------------------------------------

def test_the_exact_1_5r_setup_meets_the_floor_despite_float_error():
    rr = _reward_to_risk(ENTRY, STOP, TARGET)
    assert rr < 1.5  # 1.4999999999999818: the float error the strict check rejected
    assert meets_min_rr(rr)
    assert meets_min_rr(reward_to_risk(ENTRY, STOP, TARGET))  # the Decimal path agrees
    assert meets_min_rr(Decimal(str(rr)))


@pytest.mark.parametrize("rr", [1.49, 1.4999, Decimal("1.49"), -2.0, None, "x", float("nan")])
def test_below_the_floor_is_still_rejected(rr):
    assert not meets_min_rr(rr)


def test_the_gate_and_policy_a_accept_the_1_5r_short_and_reject_1_49(gate):
    data = {"direction": "SHORT", "candidate": {"entry": ENTRY, "stop": STOP, "target": TARGET, "timeframe": "1h",
                                                "reward_to_risk": _reward_to_risk(ENTRY, STOP, TARGET)},
            "timeframes": []}
    assert "REWARD_TO_RISK" not in hard_rejects(data)
    assert gate._rule_rejection(data) is None
    data["candidate"]["reward_to_risk"] = 1.49
    assert gate._rule_rejection(data) == "TRIAGE_RULE_REWARD_TO_RISK"


# --- 2./6. bar-close freshness; the scanner stores only what it counts ------------------------------

def _row(close_s: float, tf: str = "1h", trigger="COMMITTED_BAR_EVENT", created_s: float | None = None):
    return {"timeframe": tf, "bar_close_inclusive_ns": int(close_s * 1e9), "trigger_reason": trigger,
            "created_at": dt.datetime.fromtimestamp(created_s or close_s + 30, tz=dt.UTC)}


def test_freshness_is_by_bar_close_never_created_at():
    now = time.time()
    assert fresh_row_age_s(_row(now - 600, "15m"), now) == pytest.approx(600)
    # an ETH 15m bar from two days ago stored by a rescan after the lake repair: created now, still history
    assert fresh_row_age_s(_row(now - 2 * 86400, "15m", created_s=now - 60), now) is None
    assert fresh_row_age_s(_row(now - 4 * 900, "15m"), now) is None  # older than 3 bars
    assert fresh_row_age_s(_row(now - 600, "15m", trigger="MANUAL"), now) is None


def test_the_brain_recent_setups_drop_rescanned_history(monkeypatch):
    now = time.time()
    base = {"suggested_entry": ENTRY, "suggested_stop": STOP, "suggested_target": TARGET, "matched_rules": [],
            "price": None, "preset_slug": "swing-squeeze-breakdown-short-1h", "instrument_id": "BTCUSDT.BINANCE"}
    rows = [{**base, **_row(now - 1800)},
            {**base, "instrument_id": "ETHUSDT.BINANCE", **_row(now - 2 * 86400, "15m", created_s=now - 60)}]
    seen = {}

    def fake_rows(sql, *a, **k):
        seen["sql"] = sql
        return rows

    monkeypatch.setattr(tools, "_readonly_rows", fake_rows)
    out = tools._recent_scanner_setups(None, {})
    assert [c["instrument_id"] for c in out] == ["BTCUSDT.BINANCE"]
    assert out[0]["age_minutes"] == 30
    assert "created_at >" not in seen["sql"] and "bar_close_inclusive_ns >" in seen["sql"]


def _cand(bar_ns: int, tf: str = "1h"):
    return SimpleNamespace(bar_close_inclusive_ns=bar_ns, interval_end_exclusive_ns=bar_ns + 1, timeframe=tf,
                           suggested_entry=Decimal(100), suggested_stop=Decimal(98), suggested_target=Decimal(104))


def test_a_live_scan_stores_only_the_counted_bar_and_it_counts_as_a_trigger():
    now = time.time_ns()
    newest = now - now % HOUR_NS - 1
    df = pl.DataFrame({"bar_close_inclusive_ns": [newest - 2 * HOUR_NS, newest - HOUR_NS, newest]})
    history = [_cand(newest - 2 * HOUR_NS), _cand(newest - HOUR_NS)]
    # history only: counted NO_TRIGGER, and nothing may be stored for it
    assert worker_mod.latest_bar_outcome(df, history, now)[1] == "NO_TRIGGER"
    assert worker_mod.live_store_candidates(df, history) == []
    stored = worker_mod.live_store_candidates(df, [*history, _cand(newest)])
    assert [c.bar_close_inclusive_ns for c in stored] == [newest]
    assert worker_mod.latest_bar_outcome(df, stored, now)[1] != "NO_TRIGGER"


# --- 3. debate analysts always get the fresh scanner setups ---------------------------------------

def test_scanner_evidence_reads_the_spot_instrument_for_a_perp(monkeypatch):
    asked = []
    now = time.time()

    def fake_rows(inst):
        asked.append(inst)
        return [{"suggested_entry": ENTRY, "suggested_stop": STOP, "suggested_target": TARGET, "metrics": {},
                 **_row(now - 1200)}]

    monkeypatch.setattr(tools, "_scanner_rows", fake_rows)
    out = tools.scanner_evidence("BTCUSDT-PERP.BINANCE")
    assert asked == ["BTCUSDT.BINANCE"] and out["scanner_instrument"] == "BTCUSDT.BINANCE"
    [setup] = out["fresh_setups"]
    assert (setup["side"], setup["entry"], setup["stop"], setup["target"]) == ("SHORT", ENTRY, STOP, TARGET)
    assert setup["age_minutes"] == pytest.approx(20, abs=0.2)


def test_no_fresh_setup_says_none_fresh(monkeypatch, main):
    monkeypatch.setattr(tools, "_scanner_rows", lambda inst: [{"suggested_entry": 1, "suggested_stop": 0.9,
                                                               "suggested_target": 1.2, "metrics": {},
                                                               **_row(time.time() - 86400)}])
    out = tools.scanner_evidence("ETHUSDT.BINANCE")
    assert out["fresh_setups"] == [] and out["note"].startswith("none fresh")
    assert "none fresh" in main.scanner_evidence_block(out)


# --- 4. coverage direction and profile refusals ----------------------------------------------------

def test_coverage_states_each_mandate_direction():
    scopes = [{"name": "BTC swing", "instruments": ["BTCUSDT-PERP.BINANCE"], "markets": ["PERP"], "direction": "LONG"},
              {"name": "XRP both", "instruments": ["XRPUSDT.BINANCE"], "markets": ["SPOT_MARGIN"],
               "direction": "BOTH"}]
    assert [(c["mandate"], c["direction"], c["coins"]) for c in tools.mandate_coverage(scopes)] == [
        ("BTC swing", "LONG-only", ["BTCUSDT"]), ("XRP both", "BOTH", ["XRPUSDT"])]
    route, why = tools._setup_route("BTCUSDT.BINANCE", "SHORT", scopes)
    assert route is None and "LONG-only" in why


def _propose(monkeypatch, owners, feasibility):
    monkeypatch.setattr(profile_tools, "_readonly_rows", lambda *a, **k: owners)
    monkeypatch.setattr(profile_tools, "_fed_symbols", lambda: frozenset({"XRPUSDT"}))
    monkeypatch.setattr(profile_tools, "_order_min_feasibility", lambda proposal: feasibility)
    return profile_tools._propose({"name": "XRP shorts", "coins": ["XRP"], "markets": ["PERP"], "direction": "SHORT",
                                   "max_leverage": 2, "capital_budget_usd": 100, "max_position_pct": 5})


def test_a_locked_coin_is_refused_with_edit_the_existing_mandate(monkeypatch):
    owners = [{"id": "m1", "name": "XRP swing", "status": "ACTIVE", "instruments": ["XRPUSDT.BINANCE"]}]
    out = _propose(monkeypatch, owners, ({}, None))
    assert out["status"] == "INVALID"
    assert any(e.startswith("COIN_LOCKED:") and "edit the existing mandate" in e for e in out["errors"])


def test_an_order_below_the_1pct_minimum_is_refused(monkeypatch):
    feas = {"perp": {"max_notional": 10.0, "min_notional": 100.0, "code": "ORDER_MIN_UNREACHABLE",
                     "suggested_budget_usd": 1000}}
    out = _propose(monkeypatch, [], (feas, None))
    assert out["status"] == "INVALID"
    assert any(e.startswith("ORDER_MIN_UNREACHABLE:") and "$1000" in e for e in out["errors"])
    ok = _propose(monkeypatch, [], ({"perp": {"code": "OK"}}, None))
    assert ok["status"] == "PROPOSED", ok


# --- 7. small items ------------------------------------------------------------------------------

def test_the_risk_panel_has_no_lake_sql_but_bull_and_bear_keep_it():
    names = lambda p: {t.name for t in p.tools}  # noqa: E731
    for persona in (AGGRESSIVE_RISK, CONSERVATIVE_RISK, NEUTRAL_RISK, RISK_PANEL, CHIEF_RISK_OFFICER):
        assert "query_parquet_duckdb" not in names(persona), persona.name
    assert "query_parquet_duckdb" in names(BULL) and "query_parquet_duckdb" in names(BEAR)


def _card(**kw):
    return {"instrument_id": "BTCUSDT-PERP.BINANCE", "side": "SELL", "order_type": "MARKET",
            "stop_loss": str(STOP), "take_profit": str(TARGET), "grade_inputs": {"entry": ENTRY}, **kw}


def test_a_card_whose_rr_slipped_below_the_floor_is_refused():
    from cryptonite_api.routes.brain import slipped_below_rr_floor

    # the price fell toward the target since the card: reward 1198 -> ~898, risk 799 -> ~1099
    detail = asyncio.run(slipped_below_rr_floor(None, _card(), Decimal("76479.73")))
    assert detail is not None and detail.startswith("REWARD_TO_RISK_SLIPPED:")
    assert asyncio.run(slipped_below_rr_floor(None, _card(), Decimal(str(ENTRY)))) is None  # exactly 1.5R: allowed
    # a card proposed under the floor (an operator's explicit request) is not refused by this check
    low = _card(grade_inputs={"entry": 76479.73})
    assert asyncio.run(slipped_below_rr_floor(None, low, Decimal("76479.73"))) is None
    assert asyncio.run(slipped_below_rr_floor(None, _card(grade_inputs=None), Decimal("76479.73"))) is None


def test_a_held_card_is_traced_held_for_approval_and_a_refusal_stays_denied():
    from cryptonite_agents.gateway.providers.pydantic_ai_backend import _as_tool
    from cryptonite_agents.gateway.registry import HELD_FOR_APPROVAL_PREFIX, RiskClass, ToolSpec

    def order(symbol: str) -> str:
        """Order."""
        return "placed"

    for answer, status in ((f"{HELD_FOR_APPROVAL_PREFIX}: staged", "HELD_FOR_APPROVAL"), ("Refused: cap", "DENIED")):
        calls: list[dict] = []
        tool = _as_tool(ToolSpec("propose_order", order, RiskClass.ADD_RISK, "Order."), lambda spec: answer, calls)
        assert tool.function(symbol="BTCUSDT") == answer
        assert calls[0]["status"] == status
