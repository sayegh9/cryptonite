"""Backtest sandbox for model-written strategies (AI-first blueprint Phase 4, ADR 0003).

**The security boundary is the ``strategy_sandbox`` container** (infra/docker-compose.yml): no
network, no env file, read-only root, its own PID namespace (the worker's ``/proc/*/environ`` is out
of reach), pids/memory/CPU limits, only the capabilities the server needs to drop privileges. The
backtest worker, which holds the database credentials, never runs model code.

Two privilege levels inside the container:

- the **server** (``serve``) owns the ``sandbox_jobs`` volume it shares with the worker;
- each job's **child** runs the model code as a separate uid (``--child-uid``) in a private scratch
  dir under the container's ``/tmp``. It cannot reach the jobs volume, signal or ptrace the server,
  or see other jobs. After the job every process but the server is killed (``kill(-1)``), the
  scratch dir is reclaimed, and only the expected outputs are copied out.

  worker   ``submit``  -> jobs/pending/<job>.json          (request, written atomically)
  server   ``serve``   -> claims it (rename to running/), runs ONE child per job with RLIMIT_CPU,
                          RLIMIT_AS, RLIMIT_FSIZE, no core dumps, stderr to a file, a wall-clock
                          timeout, then copies result.json + the three artifacts into
                          jobs/done/<job>/ (no symlinks followed, regular files only, size-capped)
  worker   ``collect`` -> validates result.json against ``SandboxResult`` (bounded numbers, typed
                          shapes), copies the artifacts the same careful way into its own staging
                          dir, writes its own manifest.json with fresh hashes, deletes the job dir

Results produced inside the sandbox are attacker-influenced by construction (the strategy runs in
the same process that computes them); they are marked ``sandboxed`` and only ever feed research
recommendations, never live trading. Model code never runs in the paper orchestrator either: it
only loads registry strategies.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from cryptonite_backtester.runner import SANDBOX_ENV_FLAG, BacktestRunner, BacktestRunRequest
from cryptonite_backtester.storage import BacktestArtifactStore

DEFAULT_TIMEOUT_S = 900
CLAIM_TIMEOUT_S = 120
COLLECT_TIMEOUT_S = 120
MAX_JOBS_AHEAD = 5  # caps how long a worker waits for a busy sandbox
DEFAULT_JOBS_DIR = "/app/data/sandbox_jobs"
KILL_ALL_ENV_FLAG = "CRYPTONITE_SANDBOX_KILL_ALL"
CPU_S = 600
MEMORY_BYTES = 6 * 1024**3
MAX_FILE_BYTES = 512 * 1024**2
MAX_STDERR_TAIL = 4096
ARTIFACT_FILES = ("equity_curve.parquet", "trades.parquet", "orders.parquet")
MAX_ARTIFACT_BYTES = 256 * 1024**2
MAX_JSON_BYTES = 8 * 1024**2
DONE_TTL_S = DEFAULT_TIMEOUT_S + 300
_JOB_NAME = re.compile(r"^[A-Za-z0-9-]{1,80}-[0-9a-f]{8}\.json$")


class SandboxError(RuntimeError):
    pass


def _finite(v: float | None) -> float | None:
    if v is not None and (math.isnan(v) or math.isinf(v) or abs(v) > 1e9):
        raise ValueError("metric out of range")
    return v


class EquityPoint(BaseModel):
    ts: int
    equity: float
    drawdown: float

    @field_validator("equity", "drawdown")
    @classmethod
    def _bounded(cls, v: float) -> float:
        return _finite(v)


class SandboxResult(BaseModel):
    """The only shape the worker accepts from the sandbox: bounded, finite numbers. Unknown keys are
    dropped; the catalog and artifact manifests are rebuilt by the worker."""

    backtest_run_id: str = Field(max_length=100)
    total_return: float | None = None
    cagr: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    max_drawdown: float | None = None
    profit_factor: float | None = None
    win_rate: float | None = None
    total_trades: int = Field(ge=0, le=1_000_000)
    winning_trades: int = Field(ge=0, le=1_000_000)
    losing_trades: int = Field(ge=0, le=1_000_000)
    avg_trade_pnl: float | None = None
    max_consecutive_losses: int = Field(ge=0, le=1_000_000)
    downsampled_equity: list[EquityPoint] = Field(default_factory=list, max_length=600)
    monthly_returns: dict[str, dict[str, float]] = Field(default_factory=dict, max_length=100)

    @field_validator("total_return", "cagr", "sharpe_ratio", "sortino_ratio", "max_drawdown",
                     "profit_factor", "win_rate", "avg_trade_pnl")
    @classmethod
    def _bounded(cls, v: float | None) -> float | None:
        return _finite(v)

    @field_validator("monthly_returns")
    @classmethod
    def _months(cls, v: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        for year, months in v.items():
            if not re.fullmatch(r"\d{4}", year) or len(months) > 12:
                raise ValueError("bad monthly_returns")
            for month, ret in months.items():
                if not re.fullmatch(r"\d{1,2}", month):
                    raise ValueError("bad monthly_returns")
                _finite(ret)
        return v


# --- Safe file access (both sides read directories the other party could have shaped) -----------

def _read_under(root: Path, *parts: str, limit: int) -> bytes:
    """Read root/parts... without following any symlink and without blocking on FIFOs or devices."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)
    with os.fdopen(leaf, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise SandboxError(f"{'/'.join(parts)} is not a regular file under {limit} bytes")
        data = f.read(limit + 1)
    if len(data) > limit:
        raise SandboxError(f"{'/'.join(parts)} is larger than {limit} bytes")
    return data


def _staging_name(request: BacktestRunRequest) -> str:
    return f".tmp_{request.run_id}_{request.attempt_id}"


# --- Worker side ---------------------------------------------------------------------------------

def _dirs(jobs_dir: Path) -> tuple[Path, Path, Path]:
    return jobs_dir / "pending", jobs_dir / "running", jobs_dir / "done"


def _group_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o770)  # worker and sandbox server share the group, not the uid
    except PermissionError:
        pass  # created by the other side, which already made it group-writable


def submit(jobs_dir: Path, request: BacktestRunRequest) -> str:
    pending, _, _ = _dirs(jobs_dir)
    _group_dir(pending)
    job_id = f"{request.run_id}-{uuid.uuid4().hex[:8]}"
    tmp = pending / f".{job_id}.tmp"
    tmp.write_text(request.model_dump_json())
    os.chmod(tmp, 0o660)
    tmp.rename(pending / f"{job_id}.json")
    return job_id


def collect(jobs_dir: Path, job_id: str, request: BacktestRunRequest, store: BacktestArtifactStore) -> dict[str, Any]:
    """Validate the sandbox's result and copy its artifacts into the worker's own staging dir. The
    manifest is written here from trusted request fields and fresh hashes."""
    if not request.attempt_id:
        raise SandboxError("sandboxed runs need an attempt_id (artifacts are staged per attempt)")
    done = _dirs(jobs_dir)[2]
    try:
        status = json.loads(_read_under(done, job_id, "status.json", limit=MAX_JSON_BYTES))
        if not isinstance(status, dict) or not status.get("ok"):
            error = str(status.get("error", "")) if isinstance(status, dict) else ""
            raise SandboxError(f"sandboxed backtest failed: {error[:1000]}")
        result = SandboxResult.model_validate_json(_read_under(done, job_id, "result.json", limit=MAX_JSON_BYTES)).model_dump()

        dst_dir = store.get_staging_dir(str(request.run_id), str(request.attempt_id))
        dst_dir.mkdir(parents=True, exist_ok=True)
        artifacts: dict[str, Any] = {}
        for name in ARTIFACT_FILES:
            data = _read_under(done, job_id, "artifacts", _staging_name(request), name, limit=MAX_ARTIFACT_BYTES)
            (dst_dir / name).write_bytes(data)
            artifacts[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        manifest = json.dumps({
            "run_id": str(request.run_id), "attempt_id": str(request.attempt_id), "sandboxed": True,
            "strategy_slug": request.strategy_slug, "strategy_version_id": str(request.strategy_version_id),
            "instrument_id": request.instrument_id, "bar_type": request.bar_type, "timeframe": request.timeframe,
            "parameters": request.parameters, "execution_profile": request.execution_profile,
            "warmup_start_ns": request.warmup_start_ns, "evaluation_start_ns": request.evaluation_start_ns,
            "evaluation_end_ns": request.evaluation_end_ns, "artifacts": artifacts,
        }, indent=2, sort_keys=True, default=str).encode()
        (dst_dir / "manifest.json").write_bytes(manifest)
        artifacts["manifest.json"] = {"sha256": hashlib.sha256(manifest).hexdigest(), "size_bytes": len(manifest)}
        result["artifact_manifest"] = artifacts
        result["sandboxed"] = True
        return result
    except (OSError, ValueError, RecursionError) as e:  # pydantic ValidationError and JSONDecodeError are ValueErrors
        raise SandboxError(f"invalid sandbox output: {e}") from e
    finally:
        shutil.rmtree(done / job_id, ignore_errors=True)


def _queue_depth(jobs_dir: Path) -> int:
    return sum(1 for d in _dirs(jobs_dir)[:2] if d.exists() for f in d.iterdir() if _JOB_NAME.match(f.name))


async def run_in_sandbox(request: BacktestRunRequest, store: BacktestArtifactStore, jobs_dir: str | Path,
                         timeout_s: float = DEFAULT_TIMEOUT_S, poll_s: float = 1.0,
                         claim_timeout_s: float = CLAIM_TIMEOUT_S) -> dict[str, Any]:
    """Submit a job to the strategy_sandbox container and wait for its validated result. Jobs run one
    at a time per sandbox replica; the wait for a busy sandbox is capped at ``MAX_JOBS_AHEAD`` jobs."""
    jobs_dir = Path(jobs_dir)
    ahead = min(_queue_depth(jobs_dir), MAX_JOBS_AHEAD)
    job_id = submit(jobs_dir, request)
    pending, running, done = _dirs(jobs_dir)
    status_file = done / job_id / "status.json"
    started = time.monotonic()
    claimed_at: float | None = None
    try:
        while not status_file.exists():
            now = time.monotonic()
            if claimed_at is None and not (pending / f"{job_id}.json").exists():
                claimed_at = now
            if claimed_at is None and now - started > claim_timeout_s + timeout_s * ahead:
                raise SandboxError("the strategy sandbox did not pick up the job (is the strategy_sandbox service running?)")
            if claimed_at is not None and now - claimed_at > timeout_s + 60:  # the sandbox enforces timeout_s itself
                raise SandboxError(f"no result from the strategy sandbox within {timeout_s + 60:.0f} s")
            await asyncio.sleep(poll_s)
    except BaseException:
        (pending / f"{job_id}.json").unlink(missing_ok=True)
        (running / f"{job_id}.json").unlink(missing_ok=True)
        shutil.rmtree(done / job_id, ignore_errors=True)
        raise
    # Off the event loop, so reading up to ~1 GB of artifacts never starves the lease heartbeat
    try:
        return await asyncio.wait_for(asyncio.to_thread(collect, jobs_dir, job_id, request, store), COLLECT_TIMEOUT_S)
    except TimeoutError as e:
        raise SandboxError(f"collecting sandbox output took longer than {COLLECT_TIMEOUT_S} s") from e


# --- Sandbox side --------------------------------------------------------------------------------

def _limits(child_uid: int | None, work: Path) -> None:
    os.setsid()
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_S, CPU_S))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if child_uid is not None:
        os.setgroups([])
        os.setgid(child_uid)
        os.setuid(child_uid)
    os.chdir(work)  # after the uid drop: the server cannot enter the child's dir (no DAC_OVERRIDE)


def _child_argv(catalog: Path, work: Path) -> list[str]:
    # -I: the scratch dir is never on sys.path and PYTHON* env vars are ignored
    return [sys.executable, "-I", "-m", "cryptonite_backtester.sandbox", "child", "--catalog", str(catalog),
            "--artifacts", str(work / "artifacts"), "--request", str(work / "request.json"),
            "--result", str(work / "result.json")]


def _take_back(root: Path) -> None:
    """Make a tree the child owned (and may have chmod-ed shut) the server's again, without following
    symlinks. Only called once every child process is dead."""
    uid, gid = os.geteuid(), os.getegid()
    os.lchown(root, uid, gid)
    if root.is_symlink():
        return
    os.chmod(root, 0o700)
    for dirpath, dirnames, filenames in os.walk(root):  # top-down: children are fixed before descent
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            os.lchown(p, uid, gid)
            if not os.path.islink(p):
                os.chmod(p, 0o700 if name in dirnames else 0o600)


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def _reclaim(path: Path) -> None:
    if path.exists() or path.is_symlink():
        _take_back(path)
        _remove(path)


def run_job(jobs_dir: Path, job_file: Path, catalog: Path, timeout_s: float = DEFAULT_TIMEOUT_S,
            work_root: Path = Path("/tmp"), child_uid: int | None = None, scrub: list[Path] | None = None) -> dict[str, Any]:
    """Run one claimed job; always writes done/<job>/status.json."""
    job_id = job_file.stem
    done = _dirs(jobs_dir)[2] / job_id
    shutil.rmtree(done, ignore_errors=True)
    done.mkdir(parents=True)
    work = work_root / f"job-{job_id}"
    status: dict[str, Any] = {"ok": False}
    try:
        _reclaim(work)
        work.mkdir(mode=0o700)
        raw = job_file.read_text()
        request = BacktestRunRequest.model_validate_json(raw)
        (work / "request.json").write_text(raw)
        if child_uid is not None:
            # file first: once the dir is the child's, the server (no DAC_OVERRIDE) cannot enter it
            os.chown(work / "request.json", child_uid, child_uid)
            os.chown(work, child_uid, child_uid)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(work), "TMPDIR": str(work), SANDBOX_ENV_FLAG: "1",
               # RLIMIT_AS counts virtual memory: per-thread allocator arenas / thread pools scale with core count
               "MALLOC_ARENA_MAX": "2", "RAYON_NUM_THREADS": "4", "OMP_NUM_THREADS": "4"}
        stderr_path = done / "stderr.log"
        with open(stderr_path, "wb") as stderr:
            proc = subprocess.Popen(
                _child_argv(catalog, work), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=stderr, preexec_fn=lambda: _limits(child_uid, work),
            )
            try:
                proc.wait(timeout=timeout_s)
                status = {"ok": proc.returncode == 0, "returncode": proc.returncode}
            except subprocess.TimeoutExpired:
                status = {"ok": False, "returncode": None, "error": f"timed out after {timeout_s:.0f} s"}
            finally:
                try:  # kill the whole group: detached grandchildren must not outlive the job
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if os.environ.get(KILL_ALL_ENV_FLAG) == "1":
                    # In the container (under an init) this server is the only process that may live
                    # between jobs; kill(-1) also reaches grandchildren that left the group with
                    # setsid(). Linux never delivers it to the caller. Never set outside the container.
                    try:
                        os.kill(-1, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                proc.wait()
        _take_back(work)
        if status["ok"]:
            (done / "result.json").write_bytes(_read_under(work, "result.json", limit=MAX_JSON_BYTES))
            out = done / "artifacts" / _staging_name(request)
            out.mkdir(parents=True)
            for name in ARTIFACT_FILES:
                (out / name).write_bytes(_read_under(work, "artifacts", _staging_name(request), name, limit=MAX_ARTIFACT_BYTES))
        elif "error" not in status:
            with open(stderr_path, "rb") as f:
                f.seek(max(0, stderr_path.stat().st_size - MAX_STDERR_TAIL))
                status["error"] = " | ".join(f.read().decode(errors="replace").strip().splitlines()[-5:])
    except (OSError, ValueError, SandboxError) as e:
        status = {"ok": False, "returncode": status.get("returncode"), "error": f"sandbox output rejected: {e}"}
    finally:
        for path in [work, *(entry for base in scrub or [] if base.is_dir() for entry in base.iterdir())]:
            try:
                _reclaim(path)
            except OSError:
                pass
        job_file.unlink(missing_ok=True)
        _write_status(done, status)
    return status


def _write_status(done: Path, status: dict[str, Any]) -> None:
    tmp = done / ".status.tmp"
    tmp.write_text(json.dumps(status))
    tmp.rename(done / "status.json")


def fail_orphans(jobs_dir: Path, work_root: Path | None = None) -> None:
    """Jobs left in running/ by a crashed or restarted sandbox get a failure status, so their workers
    stop waiting instead of timing out; stale scratch dirs are removed."""
    pending, running, done = _dirs(jobs_dir)
    for d in (pending, running, done):
        _group_dir(d)
    for job in running.glob("*.json"):
        (done / job.stem).mkdir(exist_ok=True)
        _write_status(done / job.stem, {"ok": False, "returncode": None, "error": "sandbox restarted during the job"})
        job.unlink(missing_ok=True)
    for stale in work_root.glob("job-*") if work_root else []:
        _reclaim(stale)


def purge_done(jobs_dir: Path, ttl_s: float = DONE_TTL_S) -> None:
    """Drop results no worker collected (it gave up or died), so the RAM-backed volume cannot fill."""
    done = _dirs(jobs_dir)[2]
    cutoff = time.time() - ttl_s
    for entry in done.iterdir() if done.exists() else []:
        if entry.lstat().st_mtime < cutoff:
            _remove(entry)


def serve_once(jobs_dir: Path, catalog: Path, timeout_s: float = DEFAULT_TIMEOUT_S, **job_kwargs: Any) -> str | None:
    pending, running, done = _dirs(jobs_dir)
    _group_dir(running)
    _group_dir(done)
    for job in sorted(pending.glob("*.json")) if pending.exists() else []:
        if not _JOB_NAME.match(job.name):
            continue
        claimed = running / job.name
        try:
            job.rename(claimed)
        except FileNotFoundError:
            continue  # taken by another sandbox replica
        run_job(jobs_dir, claimed, catalog, timeout_s, **job_kwargs)
        return claimed.stem
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cryptonite_backtester.sandbox")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--jobs", required=True)
    serve.add_argument("--catalog", required=True)
    serve.add_argument("--work", default="/tmp")
    serve.add_argument("--child-uid", type=int)
    serve.add_argument("--scrub", action="append", default=[], help="dirs emptied after every job (e.g. /dev/shm)")
    child = sub.add_parser("child")
    for flag in ("--catalog", "--artifacts", "--request", "--result"):
        child.add_argument(flag, required=True)
    args = parser.parse_args(argv)
    if os.environ.get(SANDBOX_ENV_FLAG) != "1":
        print("refusing to run outside the strategy sandbox", file=sys.stderr)
        return 2
    if args.cmd == "serve":
        os.umask(0o007)  # results must be readable and deletable by the worker's group
        jobs, work = Path(args.jobs), Path(args.work)
        fail_orphans(jobs, work)
        while True:
            purge_done(jobs)
            if serve_once(jobs, Path(args.catalog), work_root=work, child_uid=args.child_uid,
                          scrub=[Path(p) for p in args.scrub]) is None:
                time.sleep(1.0)
    request = BacktestRunRequest.model_validate_json(Path(args.request).read_text())
    result = BacktestRunner(Path(args.catalog), Path(args.artifacts)).run(request)
    Path(args.result).write_text(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
