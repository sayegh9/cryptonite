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
    """Sharpe = 2 - |period - 20| / 10 in sample; out of sample scaled by ``oos_factor``. The code check
    rejects ``import os``; approvals are kept in ``approvals`` and decided by the test."""

    def __init__(self, oos_factor: float = 0.8):
        self.oos_factor = oos_factor
        self.saved, self.backtests = [], []
        self.history = {"bar_type": "bt", "start_ns": 0, "end_ns": 10**18}
        self.earlier_trials = 0
        self.approvals: dict[str, dict] = {}

    async def validate_strategy(self, python_code):
        return ["Line 1: import of 'os' is not allowed"] if "import os" in python_code else []

    async def request_approval(self, kind, summary, payload):
        aid = f"ap{len(self.approvals) + 1}"
        self.approvals[aid] = {"id": aid, "kind": kind, "status": "PENDING", "summary": summary, "payload": payload}
        return aid

    async def get_approval(self, approval_id):
        return self.approvals[approval_id]

    def approve(self, approval_id):
        self.approvals[approval_id]["status"] = "APPROVED"

    async def coverage(self, bar_type):
        return self.history

    async def trials_tested(self, instrument_id, timeframe, days):
        return self.earlier_trials

    async def save_strategy(self, name, description, python_code):
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
            code = "import os\nclass X: pass" if calls["draft"] == 1 else "class X: pass"
            return ModelResponse(parts=[ToolCallPart(tool.name, {
                "name": "EMA Trend", "description": "trend", "python_code": code,
                "parameter_space": [{"name": "period", "kind": "int", "low": 5, "high": 40}],
            })])
        calls["review"] += 1
        return ModelResponse(parts=[ToolCallPart(tool.name, {"verdict": verdict, "concerns": [], "summary": "ok"})])

    return FunctionModel(fn), calls


def spec(trials=16):
    return ResearchSpec("trend following", "BTCUSDT.BINANCE", "BTCUSDT.BINANCE-1-HOUR-LAST-EXTERNAL", "1h",
                        WINDOWS[0], WINDOWS[1], n_trials=trials)


async def approve_and_resume(api, loop, report):
    """The operator approves the pending draft; the worker then resumes it (same report id)."""
    if report.status != "PENDING_APPROVAL":
        return report
    approval_id = report.reason.rsplit("approval ", 1)[1].rstrip(")")
    api.approve(approval_id)
    return await loop.resume(await api.get_approval(approval_id))


async def run(api, model, tmp_path, trials=16):
    with override_model(FLASH, model), override_model(PRO, model):
        loop = ResearchLoop(Gateway(), api, reports_dir=tmp_path)
        return await approve_and_resume(api, loop, await loop.run(spec(trials)))


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
    # optimization (walk-forward folds), the full in-sample run and robustness never see OOS data
    oos_runs = [i for i, (_, w) in enumerate(api.backtests) if w == WINDOWS[1]]
    assert len(oos_runs) == 1
    assert all(w[2] <= WINDOWS[0][2] for _, w in api.backtests[:oos_runs[0]])
    assert {w for _, w in api.backtests[1:1 + 4]} == {(2, 12, 13), (4, 14, 15), (6, 16, 17), (8, 18, 20)}  # 4 folds
    assert api.backtests[0][0] == {} and report.baseline["run_id"] == "r1"  # baseline at defaults first
    assert len(report.folds) == 4 and report.dsr is None  # windows of a few ns: DSR not computable, not a reject
    assert report.robustness["fragile"] is False and {r["value"] for r in report.robustness["results"]} >= {
        report.best_parameters["period"] - 2, report.best_parameters["period"] + 2}
    assert report.holdout == {} and saved["instrument_id"] == "BTCUSDT.BINANCE"  # no holdout window in this spec


async def test_overfitting_is_rejected_before_paying_for_a_review(tmp_path):
    model, calls = scripted()
    report = await run(FakeApi(oos_factor=0.2), model, tmp_path, trials=6)
    assert report.status == "REJECT" and "overfitting guard" in report.reason
    assert calls["review"] == 0


async def test_no_valid_draft_fails_cleanly(tmp_path):
    class AlwaysReject(FakeApi):
        async def validate_strategy(self, python_code):
            return ["rejected"]

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
    (is_warm, is_start, is_end), (_, oos_start, oos_end), (_, ho_start, ho_end) = s.in_sample, s.out_of_sample, s.holdout
    assert is_warm < is_start < is_end < oos_start < oos_end < ho_start < ho_end
    assert oos_start - is_start == 90 * rq.DAY_NS and ho_start - oos_start == 30 * rq.DAY_NS
    assert ho_end == 1_000 * rq.DAY_NS and ho_end - ho_start == rq.HOLDOUT_DAYS * rq.DAY_NS  # the newest data
    assert s.bar_type == "ETHUSDT.BINANCE-4-HOUR-LAST-EXTERNAL"
    no_holdout = rq.spec_from({"idea": "x", "instrument_id": "ETHUSDT.BINANCE", "timeframe": "1h", "holdout_days": 0},
                              now_ns=1_000 * rq.DAY_NS)
    assert no_holdout.holdout is None and no_holdout.out_of_sample[2] == 1_000 * rq.DAY_NS


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



async def test_sandbox_load_errors_after_approval_fail_the_report(tmp_path):
    # The baseline backtest runs after the operator approved the draft, so a sandbox error fails the report
    # with the error instead of re-drafting (a draft that was approved is not replaced behind the operator's back)
    class FailsFirstRun(FakeApi):
        async def backtest(self, slug, instrument_id, bar_type, timeframe, parameters, window):
            if not self.backtests:
                self.backtests.append(None)
                raise ResearchApiError("backtest r0 FAILED: sandboxed backtest failed: NameError: name 'type' is not defined")
            return await super().backtest(slug, instrument_id, bar_type, timeframe, parameters, window)

    model, calls = scripted()
    report = await run(FailsFirstRun(), model, tmp_path, trials=4)
    assert report.status == "FAILED" and "name 'type' is not defined" in report.reason
    assert calls["draft"] == 2  # the model drafted twice before approval (the code check), never after it


# --- Phase 5: research validation ----------------------------------------------------------------

D = rq.DAY_NS
REAL = rq.spec_from({"idea": "x", "instrument_id": "BTCUSDT.BINANCE", "timeframe": "1h", "is_days": 90,
                     "oos_days": 30, "holdout_days": 30, "n_trials": 50}, now_ns=1_000 * D)


async def run_spec(api, model, tmp_path, s):
    with override_model(FLASH, model), override_model(PRO, model):
        loop = ResearchLoop(Gateway(), api, reports_dir=tmp_path)
        return await approve_and_resume(api, loop, await loop.run(s))


async def test_a_spec_longer_than_the_history_is_refused_before_any_call(tmp_path):
    """R5.0: 10 + 90 + 30 + 30 = 160 days needed, 120 available -> a clear FAILED, no model or backtest call."""
    api = FakeApi()
    api.history = {"bar_type": REAL.bar_type, "start_ns": 880 * D, "end_ns": 1_000 * D}
    model, calls = scripted()
    report = await run_spec(api, model, tmp_path, REAL)
    assert report.status == "FAILED" and report.reason.startswith("insufficient history")
    assert "160 days" in report.reason and "120 contiguous days" in report.reason
    assert calls["draft"] == 0 and api.backtests == []

    api.history = {"bar_type": REAL.bar_type, "start_ns": 800 * D, "end_ns": 1_000 * D - 3_600 * 10**9}
    from cryptonite_agents.research.validation import check_history
    check_history([REAL.in_sample, REAL.out_of_sample, REAL.holdout], api.history)  # 200 days, 1 h behind: fine


class RandomEntryApi(FakeApi):
    """Every parameter set is a coin flip: per-fold Sharpe is noise around 0, whatever the parameters."""

    def __init__(self, seed=0, holdout_return=0.03):
        super().__init__()
        import random
        self.rng, self.holdout_return = random.Random(seed), holdout_return

    async def backtest(self, slug, instrument_id, bar_type, timeframe, parameters, window):
        self.backtests.append((parameters, window))
        days = (window[2] - window[1]) / D
        sharpe = self.rng.gauss(0, (365 / days) ** 0.5)  # sampling spread of an annualized Sharpe of pure noise
        if window in (REAL.in_sample, REAL.out_of_sample):
            sharpe = 1.5  # let the full-window checks pass so that only the DSR stands in the way
        ret = self.holdout_return if window == REAL.holdout else 0.05
        return {"run_id": f"r{len(self.backtests)}", "sharpe_ratio": sharpe, "total_trades": 40,
                "total_return": ret, "max_drawdown": 0.04}


async def test_dsr_rejects_a_random_entry_strategy_over_50_trials(tmp_path):
    api = RandomEntryApi()
    api.earlier_trials = 25
    model, calls = scripted()
    report = await run_spec(api, model, tmp_path, REAL)
    assert report.status == "REJECT" and "deflated Sharpe ratio" in report.reason, report.reason
    assert report.dsr is not None and report.dsr < 0.95 and report.trials_tested == 75
    assert calls["review"] == 0 and report.holdout == {}
    assert all(w[2] < REAL.holdout[1] for _, w in api.backtests)  # the holdout was never touched


def test_deflated_sharpe_ratio_units():
    import random

    from cryptonite_agents.research.validation import deflated_sharpe_ratio

    rng = random.Random(42)
    noise = [rng.gauss(0, (365 / 72) ** 0.5) for _ in range(50)]  # 50 random-entry trials over 72 days
    assert deflated_sharpe_ratio(max(noise), noise, 50, 72) < 0.95
    assert deflated_sharpe_ratio(max(noise), noise, 1, 72) > deflated_sharpe_ratio(max(noise), noise, 50, 72)
    strong = [4.0 + rng.gauss(0, 0.2) for _ in range(50)]  # a real edge, stable across trials, over a year
    assert deflated_sharpe_ratio(max(strong), strong, 50, 365) > 0.95
    assert deflated_sharpe_ratio(1.0, [1.0], 1, 1) is None


def test_anchored_folds_cover_the_in_sample_window_after_the_first_segment():
    from cryptonite_agents.research.validation import anchored_folds

    folds = anchored_folds(REAL.in_sample)
    _, start, end = REAL.in_sample
    assert len(folds) == 4 and folds[0][1] == start + (end - start) // 5 and folds[-1][2] == end
    assert all(a[2] < b[1] for a, b in zip(folds, folds[1:]))
    assert all(f[1] - f[0] == REAL.in_sample[1] - REAL.in_sample[0] for f in folds)  # same warm-up


class StablePromotable(FakeApi):
    def __init__(self, holdout_return):
        super().__init__()
        self.holdout_return = holdout_return

    async def backtest(self, slug, instrument_id, bar_type, timeframe, parameters, window):
        m = await super().backtest(slug, instrument_id, bar_type, timeframe, parameters, window)
        m["sharpe_ratio"] = 6 + 0.2 * m["sharpe_ratio"]  # a strong edge, stable across parameters: DSR passes
        if window == REAL.holdout:
            m["total_return"] = self.holdout_return
        return m


@pytest.mark.parametrize("holdout_return, status", [(0.02, "PROMOTE"), (-0.02, "REJECT")])
async def test_holdout_is_scored_once_and_only_for_promote(tmp_path, holdout_return, status):
    api = StablePromotable(holdout_return)
    model, calls = scripted("PROMOTE")
    report = await run_spec(api, model, tmp_path, REAL)
    assert report.status == status, report.reason
    assert [w for _, w in api.backtests].count(REAL.holdout) == 1 and api.backtests[-1][1] == REAL.holdout
    assert report.holdout["total_return"] == holdout_return and report.dsr > 0.95
    if status == "REJECT":
        assert report.reason.startswith("holdout: holdout return is not positive")

    api, (model, _) = StablePromotable(0.02), scripted("REVISE")
    report = await run_spec(api, model, tmp_path, REAL)
    assert report.status == "REVISE" and report.holdout == {} and REAL.holdout not in [w for _, w in api.backtests]


async def test_a_fragile_optimum_is_flagged_for_the_review(tmp_path):
    class Peaky(StablePromotable):
        async def backtest(self, slug, instrument_id, bar_type, timeframe, parameters, window):
            m = await super().backtest(slug, instrument_id, bar_type, timeframe, parameters, window)
            if window == REAL.in_sample and parameters.get("period") != self.best:
                m["sharpe_ratio"] = 0.2
            return m

    api = Peaky(0.02)
    api.best = None
    model, _ = scripted("PROMOTE")
    with override_model(FLASH, model), override_model(PRO, model):
        loop = ResearchLoop(Gateway(), api, reports_dir=tmp_path)
        original = loop._optimize

        async def remember_best(*a):
            params, full = await original(*a)
            api.best = params["period"]
            full["sharpe_ratio"] = 6.0
            return params, full

        loop._optimize = remember_best
        report = await approve_and_resume(api, loop, await loop.run(REAL))
    assert report.robustness["fragile"] is True and report.robustness["worst_retention"] < 0.5
    assert len(report.robustness["results"]) == 2


# --- Research approvals: a draft waits for the operator before anything is saved or backtested ----------

async def test_a_draft_waits_for_approval_before_any_save_or_backtest(tmp_path):
    api = FakeApi()
    model, _ = scripted()
    with override_model(FLASH, model), override_model(PRO, model):
        report = await ResearchLoop(Gateway(), api, reports_dir=tmp_path).run(spec())
    assert report.status == "PENDING_APPROVAL" and "approval ap1" in report.reason
    assert api.saved == [] and api.backtests == []  # nothing ran on the draft yet
    (approval,) = api.approvals.values()
    assert approval["kind"] == "DRAFT_STRATEGY" and approval["status"] == "PENDING"
    assert approval["payload"]["report_id"] == report.id and approval["payload"]["draft"]["name"] == "EMA Trend"
    assert approval["payload"]["spec"]["instrument_id"] == "BTCUSDT.BINANCE"


async def test_approval_finishes_the_same_report_in_place(tmp_path):
    api = FakeApi()
    model, calls = scripted()
    with override_model(FLASH, model), override_model(PRO, model):
        loop = ResearchLoop(Gateway(), api, reports_dir=tmp_path)
        pending = await loop.run(spec())
        done = await approve_and_resume(api, loop, pending)
    assert done.id == pending.id and done.status == "PROMOTE"
    assert api.saved == ["EMA Trend"] and calls["draft"] == 2  # the drafter ran once per attempt, not after approval
    assert json.loads((tmp_path / f"{pending.id}.json").read_text())["status"] == "PROMOTE"


async def test_a_rejected_approval_never_reaches_the_backtester(tmp_path):
    import cryptonite_agents.research.queue as queue_mod

    api = FakeApi()
    model, _ = scripted()
    with override_model(FLASH, model), override_model(PRO, model):
        loop = ResearchLoop(Gateway(), api, reports_dir=tmp_path)
        await loop.run(spec())
    api.approvals["ap1"]["status"] = "REJECTED"

    class OneItem:
        def __init__(self, item):
            self.item = item

        async def blpop(self, key, timeout):
            return (key, json.dumps(self.item).encode())

    assert await queue_mod.work_once(OneItem({"resume_approval": "ap1"}), loop, block_s=0) is None
    assert api.saved == [] and api.backtests == []


async def test_work_once_resumes_an_approved_draft(tmp_path):
    import cryptonite_agents.research.queue as queue_mod

    api = FakeApi()
    model, _ = scripted()
    with override_model(FLASH, model), override_model(PRO, model):
        loop = ResearchLoop(Gateway(), api, reports_dir=tmp_path)
        pending = await loop.run(spec())
        api.approve("ap1")

        class OneItem:
            async def blpop(self, key, timeout):
                return (key, json.dumps({"resume_approval": "ap1"}).encode())

        assert await queue_mod.work_once(OneItem(), loop, block_s=0) == pending.id
    assert api.saved == ["EMA Trend"]


async def test_the_brain_request_waits_for_approval(monkeypatch):
    import cryptonite_agents.research.tools as tools

    seen = {}

    class Resp:
        status_code = 201

        def json(self):
            return {"id": "ap9"}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, body=json, headers=headers)
        return Resp()

    monkeypatch.setenv("CRYPTONITE_AGENT_API_TOKEN", "machine-token")
    monkeypatch.setattr(tools.httpx, "post", fake_post)
    out = tools.queue_strategy_research("buy dips", "BTCUSDT.BINANCE", "1h")
    assert "approval ap9" in out and "nothing is queued" in out
    assert seen["url"].endswith("/api/v1/research/approvals")
    assert seen["body"]["kind"] == "QUEUE_RESEARCH"
    assert seen["body"]["payload"] == {"idea": "buy dips", "instrument_id": "BTCUSDT.BINANCE", "timeframe": "1h"}
    assert seen["headers"] == {"X-API-Token": "machine-token"}

