# ADR 0003: Closed strategy research loop and sandboxed model-written strategies (Phase 4)

- Status: Accepted (2026-09-26)
- Context: [AI-first blueprint](../ai-first-blueprint.md) Phase 4 (A4.1–A4.5)

## Finding that shaped the phase

**Model-written strategies could not be backtested.** `BacktestRunner` only loaded registry strategies by slug. The `python_code` saved on AI strategy versions was carried into the request but never executed. Running it at all means executing untrusted, model-written code, so the sandbox came first.

## Decisions

1. **Allow-list validation** (`cryptonite_strategies.validation`), not the old denylist. **It is defense in depth, not the boundary** (see decision 3):
   - only `from <module> import <name>` for an explicit per-module name allow-list. No plain `import`, no star or relative imports. Allowed modules re-export dangerous ones (`typing.sys`, `dataclasses.inspect`), so names are allow-listed one by one, and at run time an imported name may never be a module;
   - no identifier or attribute starting with `_` except `__init__`. This also blocks private engine state such as `self._portfolio`;
   - no reflection or I/O names (`eval`, `getattr`, `open`, `type`, `print`, ...) as names *or* attributes, and no `format`/`format_map` or frame/traceback attributes;
   - no string annotations (`typing.get_type_hints` would `eval` them with the real builtins), and no class decorators or class keywords;
   - the existing order and engine bypass bans;
   - module level may hold only imports, constants and exactly one strategy class plus one config class.

   `resolve_imports` checks that each imported name exists in the installed Nautilus version and is not a module.
2. **The API validates on save** (`POST /strategies`, 422 with the violations). Generated code can never become a version of a core strategy (409).
3. **Model code runs only in the `strategy_sandbox` container** (`cryptonite_backtester.sandbox`, `infra/docker-compose.yml`). A first security audit showed that a subprocess of the backtest worker is not a boundary: the child could read the parent's secrets through `/proc/$PPID/environ`, reach Postgres and Redis, leave grandchildren behind that hang the worker, and write whatever results and artifacts it liked. The container has:
   - `network_mode: none`, no `env_file` and no secrets, a read-only root, and its own PID namespace;
   - `cap_drop: ALL` plus only `SETUID/SETGID/CHOWN/KILL` for the server, `no-new-privileges`, and `pids_limit`, `mem_limit` and `cpus`.

   **Two uids inside it.** The server (root in the container's group 1000, without DAC_OVERRIDE) owns the `sandbox_jobs` volume it shares with the worker. Each job's model code runs as **uid 1001**:
   - in a private scratch dir under a root-owned `/tmp`, with `python -I`;
   - with RLIMIT_CPU / AS / FSIZE / CORE and a wall-clock timeout.

   Uid 1001 cannot reach the jobs volume, create files anywhere else, signal or ptrace the server, or see other jobs. After each job:
   - the process group, and then every process except the server, is killed (`kill(-1)` under `init: true`);
   - `/dev/shm` is scrubbed and the scratch dir is chowned back;
   - only `result.json` and the three parquet artifacts are copied out. They are opened with `O_NOFOLLOW|O_NONBLOCK` at every path component and must be regular, size-capped files.

   **The worker, which holds the DB credentials, only exchanges files** (`pending/` → `running/` → `done/`). It:
   - validates `result.json` against `SandboxResult` (bounded, finite numbers; typed equity and monthly shapes);
   - copies the artifacts the same careful way in a thread with a timeout;
   - **writes its own `manifest.json`** (marked `sandboxed`) with fresh hashes, rebuilds the catalog manifest from its own catalog, and marks the outbox event `sandboxed`.

   **Failure handling.** If the sandbox does not pick a job up within 2 min (plus at most 5 queued jobs' time), the run fails with a clear message. Jobs left behind by a restarted sandbox fail at once, and uncollected results are purged. The in-process runner refuses model code outright (`CRYPTONITE_STRATEGY_SANDBOX` flag).

   **Residual risk, accepted.** The strategy runs in the same process that computes its metrics, so sandboxed results are attacker-influenced by construction. They are flagged `sandboxed` and feed research recommendations only. Model code never reaches the paper or live orchestrator, which loads registry strategies only.
4. **Research loop** (`cryptonite_agents.research`). The loop talks only to the API, using a machine token with operator scope:
   - Flash drafts a structured `StrategyDraft` (code plus a parameter space).
   - The API validates it, and a baseline backtest at default parameters runs in the sandbox.
   - Any validator, import or sandbox error goes back to the drafter (up to 3 attempts).
   - **Optuna TPE** optimizes the numeric parameters on the in-sample window only; the model never guesses numbers.
   - The best parameters are re-run **out of sample**. The **overfitting guard** rejects on out-of-sample trades < 10, out-of-sample return ≤ 0, or out-of-sample Sharpe < 50% of in-sample.
   - Only survivors get a **Pro review** (PROMOTE / REVISE / REJECT).
   - A JSON report is written to `data/research_reports/`.
5. **Queue and digest.**
   - Ideas are queued on `research:queue` by the CLI, or by the Brain through the `queue_strategy_research` tool (flagged `side_effects`, so evals strip it).
   - The opt-in `research_worker` service (`--profile research`) works off the queue.
   - `python -m cryptonite_agents.research digest` publishes a summary of the last 24 h on `channel:system_alerts`.
6. **Deviation: no Batch API.** The loop is iterative (draft, backtest, revise), and Batch results arrive hours later. Research runs on standard calls at `optional` budget priority, the first priority to stop when the day's budget runs low.

## Evidence

| Check | Result |
|---|---|
| Validator: escape attempts (imports, `open`, `__import__`, `getattr`, dunder traversal, `str.format` smuggling, `eval`, `globals`, module-level code, order bypasses) | all rejected; the 3 built-in strategies pass |
| Sandbox end-to-end (API → backtest row → worker → job dir → sandbox serve loop → COMPLETED with trades, artifacts promoted) | passes; the in-process runner refuses model code; malicious code fails inside the sandbox |
| Audit regressions (two audits, both FAIL → fixed) | Validator: `from typing import sys`, string-annotation `eval`, `self._portfolio`, class keywords and decorators, `match` class patterns, positional-only / `*args` / `**kwargs` names and annotations, PEP 695 aliases and type params are all rejected. Protocol: the child env holds only PATH/HOME/TMPDIR/flag; a grandchild is killed on timeout; out-of-range metrics, HTML in the equity or monthly shapes, directory and file symlinks, a FIFO (no blocking), and a forged manifest are all rejected or ignored; a missing sandbox fails fast; the queue wait is capped; orphaned jobs fail on restart; uncollected results are purged |
| Container probe (compose security flags, `infra-backtester` image with this source) | Server: CapEff = SETUID/SETGID/CHOWN/KILL only, NoNewPrivs 1, 5 processes visible, no secrets in env, read-only root. No network: Postgres, Redis, 8.8.8.8 and the host are all unreachable. Uid 1001 cannot write, list or signal outside its dir, cannot regain root, and cannot read the server's environ; it can read the catalog. A real model-written strategy completed as uid 1001 in 1.9 s. A hostile child (setsid sleeper, FIFO `result.json`, symlink to `/proc/1/environ`, writes to the jobs volume and `/dev/shm`, `kill -STOP` on the server) left nothing behind: only init and the server remained, `/dev/shm` was empty, the jobs volume was untouched, and the output was rejected without blocking |
| Drafter on Flash, 6 varied ideas, live | first try: 5/6 pass the validator. **Before** the import guide: 0/6 loaded (stale Nautilus import paths). **After** the import guide + `resolve_imports`: 4/6 load. The rest (a syntax error, a `type()` call) are caught and fed back. $0.035 total |
| Loop logic (offline, fake API with a known optimum) | violations reach the drafter; the optimizer finds the optimum (±3); the out-of-sample window is never used for optimization; the overfitting guard rejects without paying for a review; sandbox load errors are fed back |

## Not done / next

- An end-to-end research run on the live stack. It needs an operator-scoped machine token (`CRYPTONITE_AGENT_API_TOKEN`), market data in the catalog for the chosen instrument and timeframe, and restarted api/backtester containers carrying this code, plus the `strategy_sandbox` service.
- A research-report table (reports are files until migration numbering after 024 is free).
- IS vs OOS in the Brain's tearsheet widget (web).
- Redis `requirepass` and a least-privilege DB role for the backtest worker (it still connects as `cryptonite`).
