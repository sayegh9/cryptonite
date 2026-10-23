"""Soak report for the staging stack (compose project ``cryptonite-staging``; plan P0.7, gate V).

Reads the staging DB (``docker exec stg_postgres psql``) and the staging container logs for a window
and prints per-service error counts, ``ai_profile_decisions`` per stage/outcome, command counts
(including dry-run mandates) and restart counts. Exits 1 when any service logged an error/traceback,
a container restarted or was OOM-killed, so a deploy script can gate on it. Exit 2: staging not found.

    uv run python scripts/staging_soak_report.py [--hours 24] [--json]

It only ever talks to the ``cryptonite-staging`` project and ``stg_postgres``; it never touches production.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

PROJECT = "cryptonite-staging"
PG = "stg_postgres"
ERROR_RE = re.compile(r"Traceback \(most recent call last\)|\b(ERROR|CRITICAL|FATAL)\b|Unhandled exception", re.IGNORECASE)


def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, input=stdin, capture_output=True, text=True, check=False)


def containers() -> list[str]:
    out = _run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}", "--format", "{{.Names}}"])
    return sorted(n for n in out.stdout.split() if n.startswith("stg_"))


def log_errors(name: str, hours: float) -> tuple[int, int, list[str]]:
    """(error lines, traceback count, up to 3 sample lines) for one container."""
    res = _run(["docker", "logs", "--since", f"{int(hours * 60)}m", name])
    lines = (res.stdout + "\n" + res.stderr).splitlines()
    errors = [ln for ln in lines if ERROR_RE.search(ln)]
    tracebacks = sum(1 for ln in lines if "Traceback (most recent call last)" in ln)
    return len(errors), tracebacks, [e[:200] for e in errors[:3]]


def restart_info(name: str) -> tuple[int, bool]:
    res = _run(["docker", "inspect", "-f", "{{.RestartCount}} {{.State.OOMKilled}}", name])
    parts = res.stdout.split()
    return (int(parts[0]), parts[1] == "true") if len(parts) == 2 else (0, False)


def sql(query: str) -> list[list[str]] | None:
    """Rows as lists of strings, or None when the query fails (e.g. the table does not exist)."""
    res = _run(["docker", "exec", "-i", PG, "psql", "-U", "cryptonite", "-d", "cryptonite", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", query])
    if res.returncode != 0:
        return None
    return [ln.split("\t") for ln in res.stdout.splitlines() if ln]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=float, default=24.0, help="window in hours (default 24)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = ap.parse_args()

    names = containers()
    if not names:
        print(f"no containers found for project {PROJECT}; is staging up?", file=sys.stderr)
        return 2

    window = f"{args.hours:g} hours"
    report: dict = {"window_hours": args.hours, "services": {}, "decisions": None, "commands": None, "dry_run_commands": None}
    total_errors = 0
    bad_restarts = 0
    for n in names:
        errs, tbs, sample = log_errors(n, args.hours)
        restarts, oom = restart_info(n)
        report["services"][n] = {"error_lines": errs, "tracebacks": tbs, "restarts": restarts, "oom_killed": oom, "sample": sample}
        total_errors += errs
        bad_restarts += restarts + (1 if oom else 0)

    interval = f"now() - interval '{window}'"
    dec = sql(f"SELECT stage, outcome, count(*) FROM ai_profile_decisions WHERE ts >= {interval} GROUP BY 1,2 ORDER BY 1,2")
    report["decisions"] = None if dec is None else [{"stage": r[0], "outcome": r[1], "count": int(r[2])} for r in dec]
    cmds = sql(f"SELECT command_type, status, count(*) FROM execution_commands WHERE created_at >= {interval} GROUP BY 1,2 ORDER BY 1,2")
    report["commands"] = None if cmds is None else [{"type": r[0], "status": r[1], "count": int(r[2])} for r in cmds]
    dry = sql(
        "SELECT c.command_type, c.status, count(*) FROM execution_commands c JOIN ai_mandates m ON m.id = c.mandate_id "
        f"WHERE m.dry_run AND c.created_at >= {interval} GROUP BY 1,2 ORDER BY 1,2"
    )
    report["dry_run_commands"] = None if dry is None else [{"type": r[0], "status": r[1], "count": int(r[2])} for r in dry]
    report["total_errors"] = total_errors
    report["restarts_or_oom"] = bad_restarts
    report["pass"] = total_errors == 0 and bad_restarts == 0

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Staging soak report, last {window}")
        print(f"{'service':<24}{'errors':>8}{'tracebacks':>12}{'restarts':>10}  oom")
        for n, s in report["services"].items():
            print(f"{n:<24}{s['error_lines']:>8}{s['tracebacks']:>12}{s['restarts']:>10}  {'YES' if s['oom_killed'] else '-'}")
            for line in s["sample"]:
                print(f"    | {line}")
        for title, key in (("ai_profile_decisions by stage/outcome", "decisions"), ("execution_commands by type/status", "commands"),
                           ("dry-run mandate commands by type/status", "dry_run_commands")):
            rows = report[key]
            print(f"\n{title}:")
            if rows is None:
                print("  (unavailable: table or column absent, e.g. migration 050 not applied)")
            elif not rows:
                print("  (none in window)")
            else:
                for r in rows:
                    label = f"{r.get('stage', r.get('type'))}/{r.get('outcome', r.get('status'))}"
                    print(f"  {label:<40}{r['count']:>8}")
        print(f"\nTOTAL error lines: {total_errors}   restarts+OOM: {bad_restarts}   => {'PASS' if report['pass'] else 'FAIL'}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
