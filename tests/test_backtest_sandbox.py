"""Sandboxed backtests of model-written strategies (blueprint Phase 4, ADR 0003).

The job-dir protocol runs here in-process (``serve_once`` in a thread stands in for the
strategy_sandbox container); the container's own isolation is probed separately (HANDOFF)."""

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptonite_backtester import sandbox
from cryptonite_backtester.runner import BacktestRunner, BacktestRunRequest
from cryptonite_backtester.sandbox import (
    SandboxError,
    collect,
    fail_orphans,
    run_in_sandbox,
    run_job,
    serve_once,
    submit,
)
from cryptonite_backtester.storage import BacktestArtifactStore
from test_strategy_code_validation import GOOD
from test_warmup_no_execution import _setup_warmup_test_catalog

ATTEMPT = "00000000-0000-0000-0000-0000000000a1"


def request(inst_id="BTCUSDT.BINANCE", bar_type="bt", warmup=0, start=1, end=2, code=GOOD, slug="ai-ema-demo"):
    return BacktestRunRequest(
        run_id=f"run-{slug}", strategy_slug=slug, strategy_version_id="00000000-0000-0000-0000-0000000000aa",
        instrument_id=inst_id, bar_type=bar_type, timeframe="1m", parameters={"period": 5},
        warmup_start_ns=warmup, evaluation_start_ns=start, evaluation_end_ns=end,
        execution_profile="optimistic", initial_balance=10000.0, python_code=code, attempt_id=ATTEMPT,
    )


@contextmanager
def serving(jobs: Path, catalog: Path, timeout_s: float = 300):
    """Stand-in for the strategy_sandbox container's serve loop."""
    stop = threading.Event()
    work = jobs.parent / "sandbox_tmp"
    work.mkdir(exist_ok=True)

    def loop():
        while not stop.is_set():
            if serve_once(jobs, catalog, timeout_s, work_root=work) is None:
                time.sleep(0.05)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=30)


@pytest.fixture
def catalog(tmp_path: Path):
    return tmp_path / "nautilus_catalog", tmp_path / "jobs", _setup_warmup_test_catalog(tmp_path / "nautilus_catalog")


async def test_model_written_strategy_backtests_through_the_job_protocol(catalog, tmp_path):
    cat, jobs, (inst, bar_type, warmup, start, end) = catalog
    store = BacktestArtifactStore(tmp_path / "worker_artifacts")
    with serving(jobs, cat):
        result = await run_in_sandbox(request(inst, bar_type, warmup, start, end), store, jobs, timeout_s=300, poll_s=0.1)
    assert result["backtest_run_id"] == "run-ai-ema-demo" and result["sandboxed"] is True
    assert result["total_trades"] > 0  # the oscillating fixture crosses the EMA repeatedly
    staged = store.get_staging_dir("run-ai-ema-demo", ATTEMPT)
    assert {p.name for p in staged.iterdir()} == {*sandbox.ARTIFACT_FILES, "manifest.json"}
    assert result["artifact_manifest"]["trades.parquet"]["size_bytes"] == (staged / "trades.parquet").stat().st_size
    assert not any((jobs / "done").iterdir())  # the job dir is removed after collection
    assert not any((tmp_path / "sandbox_tmp").iterdir())  # and the child's scratch dir
    manifest = json.loads((staged / "manifest.json").read_text())
    assert manifest["sandboxed"] is True and manifest["artifacts"]["trades.parquet"] == result["artifact_manifest"]["trades.parquet"]


def test_the_in_process_runner_refuses_model_code(catalog, monkeypatch):
    monkeypatch.delenv("CRYPTONITE_STRATEGY_SANDBOX", raising=False)
    cat, _, (inst, bar_type, warmup, start, end) = catalog
    with pytest.raises(RuntimeError, match="sandbox"):
        BacktestRunner(cat, cat.parent / "a").run(request(inst, bar_type, warmup, start, end))


async def test_malicious_code_fails_inside_the_sandbox(catalog, tmp_path):
    cat, jobs, (inst, bar_type, warmup, start, end) = catalog
    evil = GOOD + "\nimport os\n"
    with serving(jobs, cat), pytest.raises(SandboxError, match="rejected|not allowed"):
        await run_in_sandbox(request(inst, bar_type, warmup, start, end, evil, "ai-evil"),
                             BacktestArtifactStore(tmp_path / "wa"), jobs, timeout_s=120, poll_s=0.1)


def test_the_child_gets_no_secrets(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "REDIS_URL", "GEMINI_API_KEY", "CRYPTONITE_AGENT_API_TOKEN", "INTERNAL_HMAC_SECRET"):
        monkeypatch.setenv(key, "secret")
    out = tmp_path / "env.txt"
    monkeypatch.setattr(sandbox, "_child_argv", lambda c, work: ["sh", "-c", f"env > {out}"])
    jobs = tmp_path / "jobs"
    fail_orphans(jobs)
    submit(jobs, request())
    job = next((jobs / "pending").glob("*.json"))
    claimed = jobs / "running" / job.name
    job.rename(claimed)
    run_job(jobs, claimed, tmp_path, timeout_s=10, work_root=tmp_path)
    env = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    allowed = {"PATH", "HOME", "TMPDIR", "CRYPTONITE_STRATEGY_SANDBOX", "PWD", "SHLVL", "_"}
    allowed |= {"MALLOC_ARENA_MAX", "RAYON_NUM_THREADS", "OMP_NUM_THREADS"}  # non-secret memory/thread caps
    assert set(env) <= allowed
    assert env["CRYPTONITE_STRATEGY_SANDBOX"] == "1"


def test_timeout_kills_the_whole_process_group(tmp_path, monkeypatch):
    pidfile = tmp_path / "grandchild.pid"
    monkeypatch.setattr(sandbox, "_child_argv", lambda c, w: ["sh", "-c", f"sleep 120 & echo $! > {pidfile}; wait"])
    jobs = tmp_path / "jobs"
    fail_orphans(jobs)
    submit(jobs, request())
    started = time.monotonic()
    assert serve_once(jobs, tmp_path, timeout_s=1, work_root=tmp_path) is not None
    assert time.monotonic() - started < 10
    status = json.loads(next((jobs / "done").glob("*/status.json")).read_text())
    assert status == {"ok": False, "returncode": None, "error": "timed out after 1 s"}
    pid = int(pidfile.read_text())
    time.sleep(0.2)
    assert not _alive(pid), "the grandchild outlived the job"


def _alive(pid: int) -> bool:
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():  # a reparented zombie still "exists" until its new parent reaps it
        return stat.read_text().rsplit(") ", 1)[1][0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _fake_done(jobs: Path, job_id: str, status: dict, result: dict | None = None) -> Path:
    done = jobs / "done" / job_id
    staged = done / "artifacts" / f".tmp_run-ai-ema-demo_{ATTEMPT}"
    staged.mkdir(parents=True)
    (done / "status.json").write_text(json.dumps(status))
    if result is not None:
        (done / "result.json").write_text(json.dumps(result))
    for name in sandbox.ARTIFACT_FILES:
        (staged / name).write_text("{}")
    return staged


RESULT = {"backtest_run_id": "run-ai-ema-demo", "total_trades": 3, "winning_trades": 2, "losing_trades": 1,
          "max_consecutive_losses": 1, "sharpe_ratio": 1.2, "artifact_manifest": {"forged": True}}


def test_collect_recomputes_the_manifest_and_cleans_up(tmp_path):
    jobs, store = tmp_path / "jobs", BacktestArtifactStore(tmp_path / "wa")
    _fake_done(jobs, "j1", {"ok": True}, RESULT)
    result = collect(jobs, "j1", request(), store)
    assert "forged" not in result["artifact_manifest"] and result["artifact_manifest"]["trades.parquet"]["size_bytes"] == 2
    assert json.loads((store.get_staging_dir("run-ai-ema-demo", ATTEMPT) / "manifest.json").read_text())["sandboxed"] is True
    assert not (jobs / "done" / "j1").exists()


@pytest.mark.parametrize("bad", [{"sharpe_ratio": float("nan")}, {"total_trades": -1}, {"sharpe_ratio": 1e12},
                                 {"downsampled_equity": [{"ts": 1, "equity": "<script>", "drawdown": 0}]},
                                 {"monthly_returns": {"<b>": {"1": 0.1}}}])
def test_collect_rejects_out_of_range_results(tmp_path, bad):
    jobs = tmp_path / "jobs"
    _fake_done(jobs, "j1", {"ok": True}, {**RESULT, **bad})
    with pytest.raises(SandboxError, match="invalid sandbox output"):
        collect(jobs, "j1", request(), BacktestArtifactStore(tmp_path / "wa"))


def test_collect_never_follows_symlinks_out_of_the_job_dir(tmp_path):
    secret = tmp_path / "worker_secret"
    secret.mkdir()
    for name in sandbox.ARTIFACT_FILES:
        (secret / name).write_text('{"secret": 1}')
    jobs, store = tmp_path / "jobs", BacktestArtifactStore(tmp_path / "wa")
    staged = _fake_done(jobs, "j1", {"ok": True}, RESULT)
    for name in sandbox.ARTIFACT_FILES:
        (staged / name).unlink()
    staged.rmdir()
    staged.symlink_to(secret)  # directory symlink pointing at the worker's files
    with pytest.raises(SandboxError):
        collect(jobs, "j1", request(), store)
    staged2 = _fake_done(jobs, "j2", {"ok": True}, RESULT)
    (staged2 / "trades.parquet").unlink()
    (staged2 / "trades.parquet").symlink_to(secret / "trades.parquet")
    with pytest.raises(SandboxError):
        collect(jobs, "j2", request(), store)
    assert not any(p.read_text() == '{"secret": 1}' for p in (tmp_path / "wa").rglob("*") if p.is_file())


def test_failed_status_is_reported(tmp_path):
    jobs = tmp_path / "jobs"
    _fake_done(jobs, "j1", {"ok": False, "error": "NameError: name 'type' is not defined"})
    with pytest.raises(SandboxError, match="name 'type' is not defined"):
        collect(jobs, "j1", request(), BacktestArtifactStore(tmp_path / "wa"))


async def test_a_missing_sandbox_fails_fast(tmp_path):
    with pytest.raises(SandboxError, match="did not pick up"):
        await run_in_sandbox(request(), BacktestArtifactStore(tmp_path / "wa"), tmp_path / "jobs",
                             timeout_s=60, poll_s=0.01, claim_timeout_s=0.1)
    assert not any((tmp_path / "jobs" / "pending").iterdir())


def test_orphaned_running_jobs_are_failed_on_restart(tmp_path):
    jobs = tmp_path / "jobs"
    fail_orphans(jobs)
    (jobs / "running" / "j9.json").write_text("{}")
    fail_orphans(jobs)
    assert json.loads((jobs / "done" / "j9" / "status.json").read_text())["ok"] is False
    assert not (jobs / "running" / "j9.json").exists()


def test_serve_refuses_to_run_outside_the_sandbox(monkeypatch):
    monkeypatch.delenv("CRYPTONITE_STRATEGY_SANDBOX", raising=False)
    assert sandbox.main(["serve", "--jobs", "/nonexistent", "--catalog", "/nonexistent"]) == 2



def test_a_fifo_never_blocks_the_worker(tmp_path):
    jobs = tmp_path / "jobs"
    staged = _fake_done(jobs, "j1", {"ok": True}, RESULT)
    (staged / "trades.parquet").unlink()
    os.mkfifo(staged / "trades.parquet")  # opening a FIFO for reading blocks until a writer appears
    started = time.monotonic()
    with pytest.raises(SandboxError, match="not a regular file"):
        collect(jobs, "j1", request(), BacktestArtifactStore(tmp_path / "wa"))
    assert time.monotonic() - started < 2


def test_the_server_copies_only_regular_outputs_from_the_scratch_dir(tmp_path, monkeypatch):
    secret = tmp_path / "server_secret.json"
    secret.write_text(json.dumps(RESULT))
    monkeypatch.setattr(sandbox, "_child_argv", lambda c, work: ["sh", "-c", f"ln -s {secret} {work}/result.json"])
    jobs = tmp_path / "jobs"
    fail_orphans(jobs)
    submit(jobs, request())
    serve_once(jobs, tmp_path, timeout_s=10, work_root=tmp_path)
    status = json.loads(next((jobs / "done").glob("*/status.json")).read_text())
    assert status["ok"] is False and "rejected" in status["error"]
    assert not list(tmp_path.glob("job-*"))


def test_the_queue_wait_is_capped(tmp_path):
    pending = tmp_path / "jobs" / "pending"
    pending.mkdir(parents=True)
    for i in range(50):
        (pending / f"junk-{i}-0000000{i % 10}.json").write_text("{}")
    assert sandbox._queue_depth(tmp_path / "jobs") == 50
    assert min(sandbox._queue_depth(tmp_path / "jobs"), sandbox.MAX_JOBS_AHEAD) == 5


def test_uncollected_results_are_purged(tmp_path):
    jobs = tmp_path / "jobs"
    _fake_done(jobs, "old", {"ok": True}, RESULT)
    os.utime(jobs / "done" / "old", (0, 0))
    _fake_done(jobs, "new", {"ok": True}, RESULT)
    sandbox.purge_done(jobs)
    assert [p.name for p in (jobs / "done").iterdir()] == ["new"]
