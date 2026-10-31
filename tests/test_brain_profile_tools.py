"""Focused contract tests for the Brain's view-only profile tools."""

import datetime as dt
import json
import uuid
from decimal import Decimal

from cryptonite_agents import profile_tools as tools
from cryptonite_agents.brain.widgets import widget_for_tool


def test_proposal_defaults_daily_loss_normalizes_bare_assets_and_reads_only_allowed_sources(monkeypatch):
    """A prose BTC/ETH request becomes a dry-run Ask-me proposal with a deterministic daily limit."""
    reads = []

    def rows(sql, params=None, **kwargs):
        reads.append(sql)
        assert "agent_safe_mandate_health" in sql
        return []

    monkeypatch.setattr(tools, "_readonly_rows", rows)
    monkeypatch.setattr(tools, "_fed_symbols", lambda: frozenset({"BTCUSDT", "ETHUSDT"}))
    result = tools._propose({
        "name": "BTC ETH swing", "coins": "BTC/ETH", "markets": ["PERP"],
        "style": "SWING", "direction": "BOTH", "max_leverage": 3,
    })

    assert result["status"] == "PROPOSED", result
    assert result["proposal"]["coins"] == ["BTCUSDT", "ETHUSDT"]
    assert result["proposal"]["daily_loss_limit_pct"] == "3"
    assert result["proposal"]["daily_loss_defaulted"] is True
    assert result["proposal"]["mode"] == "ASK" and result["proposal"]["dry_run"] is True
    assert any(warning.startswith("Negative replay:") for warning in result["warnings"])
    assert len(reads) == 1


def test_proposal_invalid_schema_still_returns_card_data_and_unknown_feed(monkeypatch):
    """Cross-field errors produce INVALID while an unreadable feed stays an explicit warning."""
    monkeypatch.setattr(tools, "_readonly_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(tools, "_fed_symbols", lambda: None)
    result = tools._propose({
        "name": "Spot short", "coins": ["BTCUSDT"], "markets": ["SPOT"],
        "direction": "SHORT", "max_leverage": 3, "max_risk_per_trade_pct": "2",
        "max_drawdown_pct": "4",
    })

    assert result["status"] == "INVALID"
    assert {"SHORT_NEEDS_MARGIN_OR_PERP", "LEVERAGE_NEEDS_MARGIN_OR_PERP"} <= set(result["errors"])
    assert result["proposal"]["daily_loss_limit_pct"] == "4"
    assert "feed unknown" in result["warnings"]


def test_lookup_refuses_ambiguous_name_and_explanation_is_code_built(monkeypatch):
    """Several exact-name matches are never guessed; a stopping explanation uses the recorded row."""
    first_id, second_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    decision = {
        "id": 2, "mandate_id": first_id, "ts": dt.datetime.now(dt.UTC),
        "instrument_id": "BTCUSDT-PERP.BINANCE", "stage": "ROUTE", "outcome": "NO_TRADE",
        "reason_text": "NO_STRUCTURE", "detail_json": {"explanation": "untrusted text"},
        "run_id": run_id, "command_id": None,
    }

    def rows(sql, params=None, **kwargs):
        if "lower(name)" in sql:
            if params["name"] == "duplicate":
                return [{"id": first_id, "name": "Duplicate"}, {"id": second_id, "name": "duplicate"}]
            return [{"id": first_id, "name": "One"}]
        if "agent_safe_debate_verdicts" in sql:
            return [{"recommendation": "BUY", "conviction_score": Decimal("0.8"),
                     "reasoning": "model-authored reasoning"}]
        if "agent_safe_profile_decisions" in sql:
            return [decision]
        return []

    monkeypatch.setattr(tools, "_readonly_rows", rows)
    ambiguous = json.loads(tools.get_profile.func("duplicate"))
    explained = json.loads(tools.explain_decision.func("One", "BTCUSDT-PERP.BINANCE"))

    assert ambiguous["status"] == "AMBIGUOUS" and len(ambiguous["candidates"]) == 2
    assert explained["stopped_at"] == "ROUTE"
    assert explained["answer_text"] == "No trade at ROUTE: NO_STRUCTURE."
    assert "untrusted text" not in explained["answer_text"]
    assert explained["decisions"][0]["verdict"]["recommendation"] == "BUY"


def test_list_and_get_report_breaker_state_without_exposing_position_rows(monkeypatch):
    """The safe view's count and breaker decision are enough for the Brain's read surface."""
    profile_id = uuid.uuid4()
    row = {
        "id": profile_id, "name": "Pausing desk", "status": "PAUSED", "autonomy_level": 2,
        "instruments": ["BTCUSDT.BINANCE"], "markets": ["SPOT"], "direction": "LONG",
        "style": "SWING", "dry_run": True, "open_positions": 1,
        "paused_reason": "operator request", "halted_reason": None,
        "session_date": dt.datetime.now(dt.UTC).date(), "session_pnl": Decimal("-12"),
        "session_start_equity": Decimal("1000"), "daily_loss_limit_pct": Decimal("3"),
    }
    decision = {
        "id": 7, "mandate_id": profile_id, "ts": dt.datetime.now(dt.UTC),
        "instrument_id": "BTCUSDT.BINANCE", "stage": "BREAKER", "outcome": "PAUSED",
        "reason_text": "operator request", "detail_json": {}, "run_id": None, "command_id": None,
    }

    def rows(sql, params=None, **kwargs):
        if "agent_safe_mandate_health" in sql:
            return [row]
        if "agent_safe_profile_decisions" in sql:
            return [decision]
        raise AssertionError(sql)

    monkeypatch.setattr(tools, "_readonly_rows", rows)
    listed = json.loads(tools.list_profiles.func())
    detailed = json.loads(tools.get_profile.func(str(profile_id)))

    assert "1 open positions" in listed["profiles"][0]["line_text"]
    assert "Paused (operator request)" in listed["profiles"][0]["line_text"]
    assert detailed["breaker"]["latest_decision"]["stage"] == "BREAKER"
    assert detailed["breaker"]["reason_text"] == "operator request"
    assert detailed["profile"]["open_positions"] == 1
    assert "positions" not in detailed["profile"]


def test_past_day_summary_keeps_pnl_unavailable(monkeypatch):
    """Decision detail cannot be used to invent realized P&L for a historical day."""
    profile_id = uuid.uuid4()
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)

    def rows(sql, params=None, **kwargs):
        if "lower(name)" in sql:
            return [{"id": profile_id, "name": "One", "session_pnl": Decimal("9"),
                     "session_date": dt.datetime.now(dt.UTC).date()}]
        return [{"stage": "EXIT", "outcome": "CLOSED", "ts": dt.datetime.now(dt.UTC)}]

    monkeypatch.setattr(tools, "_readonly_rows", rows)
    result = json.loads(tools.profile_summary.func("One", yesterday.isoformat()))

    assert result["status"] == "OK"
    assert result["counts"]["EXIT"] == {"CLOSED": 1}
    assert result["pnl_usd"] is None and result["pnl_source"] == "unavailable_for_past_days"


def test_proposal_widget_uses_tool_result_not_model_arguments():
    """Only the validated tool result can create the inline proposal card."""
    content = '{"status":"INVALID","proposal":{"name":"Review me","coins":["BTCUSDT"],"markets":["SPOT"]},"errors":["SHORT_NEEDS_MARGIN_OR_PERP"],"warnings":[]}'
    widget = widget_for_tool("propose_profile", {"spec": {"name": "forged"}}, call_id="proposal-1", content=content)
    assert widget is not None and widget["kind"] == "profile_proposal"
    assert widget["proposal"]["name"] == "Review me" and widget["status"] == "INVALID"
    assert widget_for_tool("propose_profile", {}, call_id="proposal-2", content=content, status="DENIED") is None


def test_brain_has_the_profiles_prompt_section_and_the_five_read_only_tools():
    """P3.4 static check (no model): the prompt section is present once and the tools are registered as READ."""
    from cryptonite_agents.brain.agent import BRAIN_INSTRUCTIONS
    from cryptonite_agents.brain.toolset import BRAIN_TOOLS

    assert BRAIN_INSTRUCTIONS.count("\nProfiles:\n") == 1
    assert "Never say a profile is running, trading or paused unless a tool in this turn confirms it." in BRAIN_INSTRUCTIONS
    names = {getattr(t, "name", getattr(t, "__name__", "")) for t in BRAIN_TOOLS}
    assert {"list_profiles", "get_profile", "explain_decision", "profile_summary", "propose_profile"} <= names


def test_decision_detail_reaches_the_brain_labelled_and_clipped():
    """Gate R: decision detail (route provenance, refusal text) is `_text` data, at most 1000 characters."""
    row = tools._decision_row({"id": 1, "reason_text": "x", "detail_json": {"explanation": "y" * 5000}})
    assert "detail_json" not in row and len(row["detail_text"]) == 1000
    assert row["detail_text"].startswith('{"explanation": "yyy')


def test_a_brain_call_renders_a_card_through_the_backends_result_conversion(monkeypatch):
    """Gate L regression (2026-10-31): the model saw no field names (a free-form spec) and the dict result reached
    the widget as Python repr, so no card rendered. The tool now has typed fields and returns JSON."""
    import inspect

    monkeypatch.setattr(tools, "_readonly_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(tools, "_fed_symbols", lambda: frozenset({"BTCUSDT", "ETHUSDT"}))
    params = inspect.signature(tools.propose_profile.func).parameters
    required = {n for n, p in params.items() if p.default is inspect.Parameter.empty}
    assert required == {"name", "coins", "markets", "direction"}
    assert params["mode"].default == "ASK" and params["dry_run"].default is True

    result = tools.propose_profile.func(name="BTC ETH swing", coins=["BTC", "ETH"], markets=["PERP"],
                                        direction="BOTH", max_leverage=3)
    content = result if isinstance(result, str) else str(result)  # pydantic_ai_backend's tool_result conversion
    widget = widget_for_tool("propose_profile", {}, call_id="call-1", content=content)
    assert widget is not None and widget["status"] == "PROPOSED", content
    assert widget["proposal"]["coins"] == ["BTCUSDT", "ETHUSDT"]
    assert (widget["proposal"]["mode"], widget["proposal"]["dry_run"], widget["proposal"]["max_leverage"]) == ("ASK", True, 3)
