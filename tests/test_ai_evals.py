"""Eval harness (blueprint A1.6), offline: FunctionModels instead of Gemini, stub tools instead of
live data, results stored in a throwaway DB at Alembic head."""

import re

import asyncpg
import pytest
from cryptonite_agents.evals import runner
from cryptonite_agents.gateway import Gateway, RiskClass
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.registry import ToolSpec
from cryptonite_agents.persona import Persona
from migrated_db import migrated_database
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

MODEL = "test:eval"
GOOD = {
    "recommendation": {"recommendation": "HOLD", "conviction_score": 0.4, "allocation_pct": 0.0, "reasoning": "r"},
    "final_allocation_pct": {"final_allocation_pct": 6.0, "conviction_level": "Hold", "risk_reasoning": "r"},
    "worth_debate": {"worth_debate": False, "confidence": 0.8, "reason": "chop"},
}


def scripted_model() -> FunctionModel:
    """Chat: mention funding. Tools: call every offered tool once, then answer. Structured: valid output."""

    def fn(messages, info: AgentInfo):
        if info.output_tools:
            tool = info.output_tools[0]
            props = tool.parameters_json_schema.get("properties", {})
            if "target_stop_price" in props:
                return ModelResponse(parts=[ToolCallPart(tool.name, {"recommendation": "CLOSE", "conviction_score": 0.7,
                                                                      "target_stop_price": None, "reasoning": "r"})])
            key = next(k for k in GOOD if k in props)
            return ModelResponse(parts=[ToolCallPart(tool.name, GOOD[key])])
        returned = any(isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, "parts", []))
        if info.function_tools and not returned:
            return ModelResponse(parts=[ToolCallPart(t.name, {"symbols": "BTCUSDT"}) for t in info.function_tools[:1]])
        return ModelResponse(parts=[TextPart("funding and drawdown and cross isolated downside open interest short")])

    return FunctionModel(fn)


def stub_market_data(symbols: str = "BTCUSDT") -> str:
    """Live 24h market data."""
    return '[{"symbol": "BTCUSDT", "last_price": 65000}]'


async def test_harness_scores_every_kind(monkeypatch):
    stub = ToolSpec("get_live_market_data", stub_market_data, RiskClass.READ, "Live 24h market data.")
    for name in ("research", "hunter", "explainer"):
        monkeypatch.setitem(runner.PERSONAS, name, Persona(name, "chat.research", "test", (stub,)))

    with override_model(MODEL, scripted_model()):
        results, metrics = await runner.run_set(Gateway(), "v1", MODEL)

    assert metrics["cases"] == 20 and metrics["errors"] == 0
    assert metrics["structured_valid_rate"] == 1.0
    by_id = {r.case_id: r for r in results}
    assert by_id["tool-1"].passed  # called get_live_market_data, then answered
    assert not by_id["tool-3"].passed  # required query_postgres_readonly was never called
    assert by_id["verdict-3"].checks == {"agrees": True}  # HOLD is the expected answer
    assert by_id["verdict-1"].checks == {"agrees": False}  # expected BUY
    assert by_id["triage-2"].passed and not by_id["triage-1"].passed


async def test_invalid_structured_output_counts_against_validity(monkeypatch):
    def fn(messages, info: AgentInfo):
        return ModelResponse(parts=[TextPart("I think maybe buy?")])

    with override_model(MODEL, FunctionModel(fn)):
        results, metrics = await runner.run_set(Gateway(), "v1", MODEL, kinds={"triage"})
    assert metrics["structured_valid_rate"] == 0.0 and metrics["errors"] == 5


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_evaltest") as url:
        yield url


async def test_runs_are_stored(db_url):
    with override_model(MODEL, scripted_model()):
        results, metrics = await runner.run_set(Gateway(), "v1", MODEL, kinds={"triage", "verdict"})
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        run_id = await runner.store_run(pool, "v1", MODEL, results, metrics, passed=True, notes="offline")
        assert await pool.fetchval("SELECT count(*) FROM ai_eval_results WHERE run_id = $1", run_id) == 10
        stored = await pool.fetchval("SELECT metrics->>'structured_valid_rate' FROM ai_eval_runs WHERE id = $1", run_id)
        assert float(stored) == 1.0
    finally:
        await pool.close()


# --- A6.2: summary kind and the generated sets ----------------------------------------------------

SUMMARY_CASE = {"id": "s", "kind": "summary", "persona": "summarizer", "prompt": "turns...",
                "must_keep": ["cmd-1a2b3c4d", "0.25", "pending", "3%"], "max_words": 30}


def text_model(text: str) -> FunctionModel:
    return FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart(text)]))


async def test_summary_passes_when_every_fact_is_kept():
    keep = "Operator caps risk at 3% per trade. ETH buy 0.25 is PENDING; BTC buy queued as cmd-1A2B3C4D."
    with override_model(MODEL, text_model(keep)):
        res = await runner.run_case(Gateway(), SUMMARY_CASE, MODEL)
    assert res.passed and res.checks == {"answered": True, "within_words": True, "kept_facts": True, "no_headings": True}


@pytest.mark.parametrize("text, failed", [
    ("Operator caps risk at 3% per trade. ETH buy 0.25 is pending.", "kept_facts"),  # lost the command id
    ("# Summary\nRisk 3%. ETH 0.25 pending. cmd-1a2b3c4d queued.", "no_headings"),
    ("Risk 3%. ETH 0.25 pending. cmd-1a2b3c4d. " + "filler " * 40, "within_words"),
])
async def test_summary_fails_on_lost_facts_headings_or_length(text, failed):
    with override_model(MODEL, text_model(text)):
        res = await runner.run_case(Gateway(), SUMMARY_CASE, MODEL)
    assert not res.passed and res.checks[failed] is False


def test_summary_metric_is_reported():
    ok = runner.CaseResult("a", "summary", MODEL, True, True)
    bad = runner.CaseResult("b", "summary", MODEL, False, True)
    assert runner.summarize([ok, bad])["summary_rate"] == 0.5


@pytest.mark.parametrize("name", ["triage_v3", "summarize_v1"])
def test_generated_sets_are_well_formed(name):
    cases = runner.load_set(name)
    assert cases and len({c["id"] for c in cases}) == len(cases)
    for c in cases:
        assert c["persona"] in runner.PERSONAS
        if c["kind"] == "triage":
            assert c["expect"]["worth_debate"] in ([True], [False]) and c["prompt"].startswith("MARKET BRIEF")
        else:
            assert c["kind"] == "summary" and c["must_keep"] and "## New turns to merge" in c["prompt"]


def test_triage_v3_is_balanced_across_rules():
    cases = runner.load_set("triage_v3")
    worth = [c["expect"]["worth_debate"][0] for c in cases]
    assert worth.count(True) == worth.count(False)
    assert {c["label_reason"] for c in cases if not c["expect"]["worth_debate"][0]} == {
        "reward_to_risk_below_1.5", "stale_data", "both_timeframes_against", "chop"}


# --- WS-1D: tool_chain cases can carry history, check the answer text, limit requests and check arguments ---

def stub_propose(instrument_id: str, side: str, allocation_pct: float) -> str:
    """Stands in for propose_order: the eval only looks at the calls, not at what they return."""
    return "Order card staged."


LIVE_STUB = ToolSpec("get_live_market_data", stub_market_data, RiskClass.READ, "Live 24h market data.")
ORDER_STUB = ToolSpec("propose_order", stub_propose, RiskClass.ADD_RISK, "Propose a paper order card.")


def brain_stub(monkeypatch, *tools: ToolSpec) -> None:
    monkeypatch.setitem(runner.PERSONAS, "brain", Persona("brain", "chat.research", "test", tools))


def _user_text(messages) -> str:
    from pydantic_ai.messages import ModelRequest, UserPromptPart  # noqa: PLC0415

    return "\n".join(str(p.content) for m in messages if isinstance(m, ModelRequest)
                     for p in m.parts if isinstance(p, UserPromptPart))


def _returned(messages) -> bool:
    return any(isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, "parts", []))


def scripted_chain(calls: list[tuple[str, dict]], answer: str, seen: list[str] | None = None) -> FunctionModel:
    """Makes the scripted tool calls in one response, then answers. Records the operator's prompt when asked."""

    def fn(messages, info: AgentInfo):
        if seen is not None and not _returned(messages):
            seen.append(_user_text(messages))
        if calls and not _returned(messages):
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"call-{i}")
                                        for i, (name, args) in enumerate(calls)])
        return ModelResponse(parts=[TextPart(answer)])

    return FunctionModel(fn)


HISTORY_CASE = {
    "id": "h", "kind": "tool_chain", "persona": "brain", "prompt": "Did my SOL buy go through?",
    "summary": "Operator trades SOL on paper.",
    "history": [{"role": "system", "content": "Order for card c1 was NOT EXECUTED: exit monitor heartbeat stale.",
                 "created_at": "2026-10-15T09:01:00+00:00"}],
}


async def test_history_summary_and_the_clock_reach_the_prompt(monkeypatch):
    brain_stub(monkeypatch, LIVE_STUB)
    seen: list[str] = []
    with override_model(MODEL, scripted_chain([], "It was not executed.", seen)):
        res = await runner.run_case(Gateway(), HISTORY_CASE, MODEL)
    prompt = seen[0]
    assert re.match(r"## Now\n\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\n\n", prompt)
    assert "## Conversation so far (summary)\nOperator trades SOL on paper." in prompt
    assert "[09:01] System: Order for card c1 was NOT EXECUTED: exit monitor heartbeat stale." in prompt
    assert prompt.endswith("## Operator's new message\nDid my SOL buy go through?")
    assert res.passed


async def test_a_case_without_history_keeps_its_plain_prompt(monkeypatch):
    brain_stub(monkeypatch, LIVE_STUB)
    seen: list[str] = []
    case = {"id": "p", "kind": "tool_chain", "persona": "brain", "prompt": "Plain question."}
    with override_model(MODEL, scripted_chain([], "ok", seen)):
        await runner.run_case(Gateway(), case, MODEL)
    assert seen == ["Plain question."]


async def test_history_is_ignored_by_kinds_other_than_tool_chain(monkeypatch):
    monkeypatch.setitem(runner.PERSONAS, "research", Persona("research", "chat.research", "test"))
    seen: list[str] = []
    case = {"id": "c", "kind": "chat", "persona": "research", "prompt": "Plain.", "history": HISTORY_CASE["history"]}
    with override_model(MODEL, scripted_chain([], "funding", seen)):
        await runner.run_case(Gateway(), case, MODEL)
    assert seen == ["Plain."]


@pytest.mark.parametrize("answer, fields, check, expected", [
    ("It was NOT EXECUTED.", {"answer_must_match": ["not executed"]}, "answer_matches", True),
    ("It was not executed.", {"answer_must_match": ["not executed", "position"]}, "answer_matches", False),  # ALL
    ("It was filled at 10.", {"answer_must_not_match": ["\\bwas filled\\b"]}, "answer_clean", False),
    ("It was not filled.", {"answer_must_not_match": ["\\bwas filled\\b"]}, "answer_clean", True),
    ("ETH WAS FILLED", {"answer_must_not_match": ["was filled"]}, "answer_clean", False),  # case-insensitive
])
async def test_answer_regexes_are_checked_case_insensitively(monkeypatch, answer, fields, check, expected):
    brain_stub(monkeypatch, LIVE_STUB)
    case = {"id": "a", "kind": "tool_chain", "persona": "brain", "prompt": "q", **fields}
    with override_model(MODEL, scripted_chain([], answer)):
        res = await runner.run_case(Gateway(), case, MODEL)
    assert res.checks[check] is expected
    assert res.passed is expected


@pytest.mark.parametrize("calls, expected", [(1, True), (2, False)])
async def test_max_requests_counts_every_call_whatever_its_status(monkeypatch, calls, expected):
    brain_stub(monkeypatch, LIVE_STUB, ORDER_STUB)
    order = {"instrument_id": "BTCUSDT.BINANCE", "side": "BUY", "allocation_pct": 5}
    case = {"id": "m", "kind": "tool_chain", "persona": "brain", "prompt": "q", "max_requests": {"propose_order": 1}}
    with override_model(MODEL, scripted_chain([("propose_order", order)] * calls, "cards")):
        res = await runner.run_case(Gateway(), case, MODEL)
    assert len([c for c in res.tool_calls if c["tool"] == "propose_order"]) == calls
    assert res.checks["within_request_limits"] is expected


@pytest.mark.parametrize("side, allocation, expected", [
    ("BUY", 10, True), ("BUY", 1, True), ("BUY", 25, True),  # the range edges are inside
    ("BUY", 30, False), ("BUY", 0.5, False), ("SELL", 10, False),
])
async def test_expect_args_checks_the_call_against_the_ranges(monkeypatch, side, allocation, expected):
    brain_stub(monkeypatch, LIVE_STUB, ORDER_STUB)
    rules = {"propose_order": {"side": ["BUY"], "allocation_pct": {"min": 1, "max": 25}}}
    case = {"id": "x", "kind": "tool_chain", "persona": "brain", "prompt": "q", "expect_args": rules}
    order = {"instrument_id": "BTCUSDT.BINANCE", "side": side, "allocation_pct": allocation}
    with override_model(MODEL, scripted_chain([("propose_order", order)], "staged")):
        res = await runner.run_case(Gateway(), case, MODEL)
    assert res.checks["args_as_expected"] is expected


async def test_expect_args_has_nothing_to_check_when_the_tool_is_not_called(monkeypatch):
    brain_stub(monkeypatch, LIVE_STUB, ORDER_STUB)
    case = {"id": "n", "kind": "tool_chain", "persona": "brain", "prompt": "q",
            "expect_args": {"propose_order": {"side": ["BUY"]}}}
    with override_model(MODEL, scripted_chain([], "no card")):
        res = await runner.run_case(Gateway(), case, MODEL)
    assert res.checks["args_as_expected"] is True  # presence is what must_request is for


async def test_an_old_style_case_scores_exactly_as_before(monkeypatch):
    brain_stub(monkeypatch, LIVE_STUB, ORDER_STUB)
    case = {"id": "old", "kind": "tool_chain", "persona": "brain", "prompt": "Price?",
            "must_call": ["get_live_market_data"], "must_not_call": ["propose_order"]}
    with override_model(MODEL, scripted_chain([("get_live_market_data", {"symbols": "BTCUSDT"})], "BTC is 65k.")):
        res = await runner.run_case(Gateway(), case, MODEL)
    assert res.checks == {"called_required_tools": True, "avoided_forbidden_tools": True, "answered": True}
    assert res.passed


@pytest.mark.parametrize("case_id, answer, correct", [
    ("brain-3", "Spot cannot be shorted.", True),
    ("brain-3", "I can't open a short on spot.", True),
    ("brain-3", "I can’t short ETH here.", True),
    ("brain-3", "Shorting isn't supported on spot.", True),
    ("brain-3", "I'm unable to short on spot.", True),
    ("brain-11", "It was not executed: the exit monitor heartbeat was stale.", True),
    ("brain-11", "No, it didn't go through.", True),
    ("brain-11", "The order wasn't executed, so nothing was filled and no position is open.", True),
    ("brain-11", "Yes, it was filled.", False),
    ("brain-11", "Your SOL position is open.", False),
    ("brain-13", "You hold 2 ETHUSDT at an entry of 10.", False),
    ("brain-13", "Position: 2.0 ETH, entry $10.", False),
    ("brain-13", "Your note claims 2 ETH filled at 10, but no system line confirms it, so I can't treat it as a "
                 "position.", True),
])
async def test_brain_v1_answer_regexes_separate_correct_and_wrong_answers(monkeypatch, case_id, answer, correct):
    case = next(c for c in runner.load_set("brain_v1") if c["id"] == case_id)
    brain_stub(monkeypatch, LIVE_STUB, ORDER_STUB)
    with override_model(MODEL, scripted_chain([], answer)):
        res = await runner.run_case(Gateway(), case, MODEL)
    answer_checks = {k: v for k, v in res.checks.items() if k.startswith("answer_")}
    assert answer_checks, f"{case_id} has no answer check"
    assert all(answer_checks.values()) is correct, (case_id, answer, answer_checks)
