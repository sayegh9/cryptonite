"""Eval CLI.

    python -m cryptonite_agents.evals run --set v1 --model google:gemini-2.5-flash [--repeat 2]
        [--kinds verdict,triage] [--min-tool-chain 0.95] [--min-structured 0.99] [--store]

Prints a JSON report; exits 1 when a threshold is given and missed. ``--store`` writes the run to
``ai_eval_runs`` / ``ai_eval_results`` via EVAL_DATABASE_URL (default DATABASE_URL). Tools that read
the database use DATABASE_URL, so point that at the read-only agent role.
"""

import argparse
import asyncio
import json
import os
import sys

from cryptonite_agents.brain import brain_gateway_routes
from cryptonite_agents.evals.runner import run_set, store_run
from cryptonite_agents.gateway import BudgetGuard, Gateway


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cryptonite_agents.evals")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--set", default="v1")
    run.add_argument("--model", required=True)
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--kinds", default="")
    run.add_argument("--personas", default="")
    run.add_argument("--min-tool-chain", type=float)
    run.add_argument("--min-structured", type=float)
    run.add_argument("--store", action="store_true")
    run.add_argument("--notes")
    args = parser.parse_args(argv)

    # Eval spend is reported in the run itself and kept out of the production budget/ledger.
    gateway = Gateway(routes=brain_gateway_routes(), budget=BudgetGuard(default_cap_usd=10**6))
    kinds = {k for k in args.kinds.split(",") if k} or None
    personas = {p for p in args.personas.split(",") if p} or None
    results, metrics = await run_set(gateway, args.set, args.model, args.repeat, kinds, personas)

    passed = None
    failures = []
    if args.min_tool_chain is not None and metrics["tool_chain_rate"] is not None:
        if metrics["tool_chain_rate"] < args.min_tool_chain:
            failures.append(f"tool_chain_rate {metrics['tool_chain_rate']} < {args.min_tool_chain}")
    if args.min_structured is not None and metrics["structured_valid_rate"] is not None:
        if metrics["structured_valid_rate"] < args.min_structured:
            failures.append(f"structured_valid_rate {metrics['structured_valid_rate']} < {args.min_structured}")
    if args.min_tool_chain is not None or args.min_structured is not None:
        passed = not failures

    report = {"set": args.set, "model": args.model, "repeat": args.repeat, "metrics": metrics,
              "passed": passed, "failures": failures,
              "cases": [{"id": r.case_id, "kind": r.kind, "passed": r.passed, "valid": r.valid, "checks": r.checks,
                         "latency_ms": r.latency_ms, "cost_usd": str(r.cost_usd), "error": r.error,
                         "output": r.output if r.kind in ("verdict", "triage") else None} for r in results]}

    if args.store:
        import asyncpg

        pool = await asyncpg.create_pool(os.environ.get("EVAL_DATABASE_URL") or os.environ["DATABASE_URL"], min_size=1, max_size=2)
        try:
            report["run_id"] = str(await store_run(pool, args.set, args.model, results, metrics, passed, args.notes))
        finally:
            await pool.close()

    print(json.dumps(report, indent=2, default=str))
    return 1 if passed is False else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
