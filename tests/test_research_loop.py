"""Strategy research loop (blueprint A4.1–A4.5), offline: scripted models, a fake API whose Sharpe
peaks at a known parameter, reports in a temp dir. No network, no paid calls."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import FLASH, PRO
from cryptonite_agents.research import queue as rq
from cryptonite_agents.research.client import ResearchApiClient, ResearchApiError
from cryptonite_agents.research.loop import ResearchLoop, ResearchSpec, overfitting_flags
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

WINDOWS = ((0, 10, 20), (15, 25, 35))


class FakeApi:
    """Sharpe = 2 - |period - 20| / 10 in sample; out of sample scaled by ``oos_factor``."""

    def __init__(self, oos_factor: float = 0.8, reject_first: bool = True):
        self.oos_factor, self.reject_first = oos_factor, reject_first
        self.saved, self.backtests = [], []

    async def save_strategy(self, name, description, python_code):
        if self.reject_first and not self.saved:
            self.saved.append(None)
            raise ResearchApiError('POST /strategies -> 422: {"violations": ["Line 3: import of \'os\' is not allowed"]}')
        self.saved.append(name)
        return {"slug": "ai-gen-abc123", "version_id": "v1"}

    async def backtest(self, slug, instrument_id, bar_type, timeframe, parameters, window):
        self.backtests.append((parameters, window))
        sharpe = 2 - abs(parameters.get("period", 20) - 20) / 10
        if window == WINDOWS[1]:
            sharpe *= self.oos_factor
        return {"run_id": f"r{len(self.backtests)}", "sharpe_ratio": sharpe, "total_trades": 40,
                "total_return": 0.05 if sharpe > 0 else -0.02, "max_drawdown": 0.04}


def scripted(verdict="PROMOTE"):
    calls = {"draft": 0, "review": 0, "draft_prompts": []}

    def fn(messages, info: AgentInfo):
        tool = info.output_tools[0]
        props = tool.parameters_json_schema.get("properties", {})
        if "python_code" in props:
            calls["draft"] += 1
            calls["draft_prompts"].append(" ".join(str(p.content) for m in messages if isinstance(m, ModelRequest)
                                                   for p in m.parts if hasattr(p, "content")))
            return ModelResponse(parts=[ToolCallPart(tool.name, {
                "name": "EMA Trend", "description": "trend", "python_code": "class X: pass",
                "parameter_space": [{"name": "period", "kind": "int", "low": 5, "high": 40}],
            })])
        calls["review"] += 1
        return ModelResponse(parts=[ToolCallPart(tool.name, {"verdict": verdict, "concerns": [], "summary": "ok"})])

    return FunctionModel(fn), calls


def spec(trials=16):
    return ResearchSpec("trend following", "BTCUSDT.BINANCE", "BTCUSDT.BINANCE-1-HOUR-LAST-EXTERNAL", "1h",
                        WINDOWS[0], WINDOWS[1], n_trials=trials)


async def run(api, model, tmp_path, trials=16):
    with override_model(FLASH, model), override_model(PRO, model):
        return await ResearchLoop(Gateway(), api, reports_dir=tmp_path).run(spec(trials))


async def test_full_loop_feeds_back_violations_optimizes_and_promotes(tmp_path):
    api = FakeApi()
    model, calls = scripted()
    report = await run(api, model, tmp_path)

    assert report.status == "PROMOTE", report.reason
    assert calls["draft"] == 2 and "not allowed" in calls["draft_prompts"][1]  # the rejection reached the model
    assert abs(report.best_parameters["period"] - 20) <= 3  # the optimizer found the peak, not the model
    assert len(report.trials) == 16 and report.out_of_sample["sharpe_ratio"] > 1
    assert calls["review"] == 1
    saved = json.loads((tmp_path / f"{report.id}.json").read_text())
    assert saved["status"] == "PROMOTE" and saved["strategy_slug"] == "ai-gen-abc123"
    assert all(window == WINDOWS[0] for _, window in api.backtests[:-1])  # optimization never sees OOS data
    assert api.backtests[0][0] == {} and report.baseline["run_id"] == "r1"  # baseline at defaults first


async def test_overfitting_is_rejected_before_paying_for_a_review(tmp_path):
    model, calls = scripted()
    report = await run(FakeApi(oos_factor=0.2), model, tmp_path, trials=6)
    assert report.status == "REJECT" and "overfitting guard" in report.reason
    assert calls["review"] == 0


async def test_no_valid_draft_fails_cleanly(tmp_path):
    class AlwaysReject(FakeApi):
        async def save_strategy(self, *a):
            raise ResearchApiError("422 rejected")

    model, _ = scripted()
    report = await run(AlwaysReject(), model, tmp_path)
    assert report.status == "FAILED" and "no valid draft" in report.reason


def test_overfitting_flags():
    good = {"sharpe_ratio": 1.2, "total_trades": 30, "total_return": 0.04}
    assert overfitting_flags({"sharpe_ratio": 1.5}, good) == []
    flags = overfitting_flags({"sharpe_ratio": 3.0}, {"sharpe_ratio": 0.5, "total_trades": 4, "total_return": -0.01})
    assert len(flags) == 3


def test_windows_are_ordered_and_bar_type_is_derived():
    s = rq.spec_from({"idea": "x", "instrument_id": "ETHUSDT.BINANCE", "timeframe": "4h", "is_days": 90, "oos_days": 30},
                     now_ns=1_000 * rq.DAY_NS)
    (is_warm, is_start, is_end), (_, oos_start, oos_end) = s.in_sample, s.out_of_sample
    assert is_warm < is_start < is_end < oos_start < oos_end
    assert oos_start - is_start == 90 * rq.DAY_NS and oos_end == 1_000 * rq.DAY_NS
    assert s.bar_type == "ETHUSDT.BINANCE-4-HOUR-LAST-EXTERNAL"


def test_digest_summarizes_recent_reports(tmp_path):
    now = datetime(2026, 9, 26, 7, 0, tzinfo=UTC)
    for i, (status, created) in enumerate([("PROMOTE", "2026-09-26T02:00:00+00:00"), ("REJECT", "2026-09-26T03:00:00+00:00"),
                                           ("PROMOTE", "2026-09-20T03:00:00+00:00")]):
        (tmp_path / f"r{i}.json").write_text(json.dumps({"id": f"r{i}", "idea": "i", "status": status, "reason": "why",
                                                          "created_at": created, "cost_usd": 0.1,
                                                          "out_of_sample": {"sharpe_ratio": 1.1}}))
    digest = rq.build_digest(tmp_path, hours=24, now=now)
    assert digest["count"] == 2 and digest["by_status"] == {"PROMOTE": 1, "REJECT": 1}
    assert digest["text"].splitlines()[1].startswith("- [PROMOTE]")


async def test_client_uses_the_machine_token_and_waits_for_a_terminal_status():
    seen = []
    statuses = iter(["PENDING", "RUNNING", "COMPLETED"])

    def handler(request: httpx.Request):
        seen.append((request.method, request.url.path, request.headers.get("X-API-Token")))
        if request.method == "POST":
            return httpx.Response(201, json={"id": "run-1"})
        if request.url.path.endswith("/results"):
            return httpx.Response(200, json={"metrics": {"sharpe_ratio": 1.0}})
        return httpx.Response(200, json={"status": next(statuses)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ResearchApiClient("http://api", "tok", http=http, poll_s=0)
        metrics = await client.backtest("s", "BTCUSDT.BINANCE", "bt", "1h", {}, (0, 1, 2))
    assert metrics == {"run_id": "run-1", "sharpe_ratio": 1.0}
    assert all(token == "tok" for *_, token in seen)
    assert [p for m, p, _ in seen][:2] == ["/api/v1/backtests", "/api/v1/backtests/run-1"]


def test_report_reader_rejects_path_tricks(tmp_path, monkeypatch):
    from cryptonite_agents.research import tools

    monkeypatch.setattr(tools, "REPORTS_DIR", tmp_path)
    (tmp_path / "abc-1.json").write_text(json.dumps({"id": "abc-1", "status": "REJECT", "trials": [1, 2]}))
    assert json.loads(tools.read_research_report("abc-1")) == {"id": "abc-1", "status": "REJECT"}
    assert tools.read_research_report("../../etc/passwd").startswith("No research report")


@pytest.mark.parametrize("tf", ["2h", "weekly"])
def test_unsupported_timeframes_are_refused(tf):
    from cryptonite_agents.research.tools import queue_strategy_research

    assert queue_strategy_research("x", "BTCUSDT.BINANCE", tf).startswith("Error")



async def test_sandbox_load_errors_are_fed_back_to_the_drafter(tmp_path):
    class FailsFirstRun(FakeApi):
        async def backtest(self, slug, instrument_id, bar_type, timeframe, parameters, window):
            if not self.backtests:
                self.backtests.append(None)
                raise ResearchApiError("backtest r0 FAILED: sandboxed backtest failed: NameError: name 'type' is not defined")
            return await super().backtest(slug, instrument_id, bar_type, timeframe, parameters, window)

    model, calls = scripted()
    report = await run(FailsFirstRun(reject_first=False), model, tmp_path, trials=4)
    assert report.status == "PROMOTE"
    assert calls["draft"] == 2 and "name 'type' is not defined" in calls["draft_prompts"][1]
