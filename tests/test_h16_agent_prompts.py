"""H16: agent instructions match what the system does, and shadow-v2 stays apart from the live prompt."""

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/debate_worker"))

import pytest  # noqa: E402
import shadow_debate as sd  # noqa: E402
import shadow_eval as se  # noqa: E402
from cryptonite_agents import debate_fleet as fleet  # noqa: E402
from cryptonite_agents import tools  # noqa: E402
from cryptonite_agents.brain.agent import BRAIN_INSTRUCTIONS  # noqa: E402
from reflection import REVIEWER  # noqa: E402

BRAIN = " ".join(BRAIN_INSTRUCTIONS.split())


def test_brain_does_not_force_levels_or_claim_shorts_are_perp_only():
    assert "end with a concrete plan" not in BRAIN and "never invent levels" in BRAIN
    assert "shorting is possible on perps only" not in BRAIN
    assert "Your order cards can open a new short on perps only" in BRAIN and "isolated spot margin" in BRAIN


def test_brain_reports_scan_results_and_operating_mode():
    assert "report the instrument, direction, timeframe, strategy, signal time and that status" in BRAIN
    assert "without inventing levels" in BRAIN
    assert "never say every proposal needs approval" in BRAIN and "paper_rule_entry" in BRAIN
    assert "auto_trade_switch" in BRAIN


@pytest.mark.parametrize(("rows", "expected"), [
    ([{"value_text": "1"}], True), ([{"value_text": "0"}], False), ([{"value_text": "x"}], False),
    ([{"value_text": None}], False), ([], False),
])
def test_switch_section_reads_like_the_runtime(monkeypatch, rows, expected):
    monkeypatch.setattr(tools, "_readonly_rows", lambda _sql: rows)
    assert tools._state_auto_trade_switch() == {"on": expected}


def test_researchers_are_not_told_to_force_a_rebuttal():
    for persona in (fleet.BULL, fleet.BEAR):
        text = persona.instructions
        assert "aggressively" not in text and "fiercely" not in text and "say why you reject" not in text
        assert "cannot settle a point" in text and "VERIFIED FACTS" in text
    assert "add no new position" in fleet.BEAR.instructions
    assert "Rebut" not in (ROOT / "services/debate_worker/main.py").read_text()


def test_pm_runs_before_the_risk_panel_and_names_material_arguments():
    text = fleet.research_manager().instructions
    assert "the risk panel reviews it afterwards" in text and "and the risk analysts" not in text
    assert "which arguments remain material" in text


def test_risk_wording_keeps_the_code_enforced_limits():
    for persona in (fleet.RISK_PANEL, fleet.CHIEF_RISK_OFFICER):
        text = persona.instructions
        assert "material risk" in text and "existing exposure" in text and "confirm or override" not in text
        assert "you can never turn a PM HOLD into a trade, reverse its direction or size it up" in text


def test_exit_agents_only_argue_what_the_schema_allows():
    assert "Add" not in fleet.EXIT_BEAR.instructions.replace("Adding exposure is not an option", "")
    assert "Hedge" not in fleet.EXIT_BULL.instructions
    assert "HOLD/ADD" not in (ROOT / "services/debate_worker/main.py").read_text()


def test_analysts_and_reviewer_handle_evidence():
    assert "direction and timeframe" in fleet.MARKET_ANALYST.instructions
    assert "untrusted" in fleet.SENTIMENT_ANALYST.instructions and "do not invent it" in fleet.SENTIMENT_ANALYST.instructions
    text = REVIEWER.instructions
    assert "whether costs are included" in text and "cannot prove" in text and "Bitcoin/Ethereum" not in text


def test_shadow_v2_is_frozen_and_the_live_prompt_is_unchanged():
    digest = hashlib.sha256(fleet.DEBATE_LITE_SHADOW_V2.instructions.encode()).hexdigest()
    assert digest == "d8a3e826aededf6283ffad0d5bc5fee2f000df41aa7a36b5a0dd2f0b9fd6233c"
    assert se.PROMPT_VERSIONS == ("shadow-v1", "shadow-v2")
    assert [v for v, _ in sd.SHADOW_PROMPTS] == list(se.PROMPT_VERSIONS)
    assert "Prefer HOLD when the data is mixed, stale or thin." in fleet.DEBATE_LITE.instructions
    assert fleet.DEBATE_LITE_SHADOW_V2.output_type is fleet.DEBATE_LITE.output_type


def test_the_report_prints_one_verdict_per_version():
    import datetime  # noqa: PLC0415

    sys.path.insert(0, str(ROOT / "scripts"))
    import shadow_prompt_report as report  # noqa: PLC0415

    now = datetime.datetime.now(datetime.UTC)
    raw = [{"state": "OK", "prompt_version": v, "recommendation": "HOLD", "cost_usd": 0.01, "created_at": now,
            "instrument_id": "BTCUSDT.BINANCE", "bar_close_inclusive_ns": int(now.timestamp() * 10**9), "direction": "LONG",
            "net_r_live": -1.0, "trigger_reason": "LIVE", "live_decision": "SKIP"} for v in se.PROMPT_VERSIONS]
    text = report.render(raw, {})
    assert "prompt shadow-v1" in text and "prompt shadow-v2" in text
    assert text.count("1 shadow rows") == 2
