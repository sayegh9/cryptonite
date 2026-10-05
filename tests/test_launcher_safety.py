"""A5 (F-M2): the desktop launcher must not migrate the DB or touch held services.

Runs the real scripts/launcher/cryptonite-up.sh with docker, curl and pnpm replaced by fakes on PATH
that log every call. Nothing reaches Docker, the DB or the network."""

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "scripts" / "launcher" / "cryptonite-up.sh"
HELD = ("orchestrator", "scanner", "debate_worker", "learner")
SERVICES = ("redis", "postgres", "exit_monitor", "api", "notifier", "derivatives_feed", "orchestrator",
            "strategy_sandbox", "agent_worker", "backtester", "learner", "outbox", "sentiment",
            "debate_worker", "ingest", "scanner")

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
case " $* " in
  *" config --services "*) printf '%s\\n' $FAKE_SERVICES ;;
  *" alembic "*" current "*) echo "$FAKE_ALEMBIC_CURRENT" ;;
  *" ps -a -q "*) svc="${@: -1}"; [[ " $FAKE_EXISTING " == *" $svc "* ]] && echo "id-$svc" ;;
esac
exit 0
"""


@pytest.fixture
def fake_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    for name in ("curl", "pnpm"):  # curl: every URL answers, so the web/browser branches are inert
        (bin_dir / name).write_text("#!/usr/bin/env bash\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir


def run_launcher(fake_bin, tmp_path, *, alembic_current, held=None, existing=HELD):
    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CRYPTONITE_NO_BROWSER": "1",
        "FAKE_LOG": str(log),
        "FAKE_SERVICES": " ".join(SERVICES),
        "FAKE_ALEMBIC_CURRENT": alembic_current,
        "FAKE_EXISTING": " ".join(existing),
    }
    if held is not None:
        env["CRYPTONITE_HELD_SERVICES"] = held
    proc = subprocess.run(["bash", str(LAUNCHER)], env=env, capture_output=True, text=True, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def up_calls(calls):
    return [c for c in calls if " up " in f" {c} "]


def test_pending_migration_is_reported_not_applied(fake_bin, tmp_path):
    proc, calls = run_launcher(fake_bin, tmp_path, alembic_current="035_cross_mandate_exposure")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not [c for c in calls if "upgrade" in c], calls
    assert any("alembic" in c and c.endswith("current") for c in calls)
    assert "schema is NOT at head" in proc.stdout


def test_schema_at_head_passes_quietly(fake_bin, tmp_path):
    proc, _ = run_launcher(fake_bin, tmp_path, alembic_current="036_shadow_mode (head)")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "schema at head" in proc.stdout and "NOT at head" not in proc.stdout


def test_held_services_are_never_upped_or_recreated(fake_bin, tmp_path):
    proc, calls = run_launcher(fake_bin, tmp_path, alembic_current="036 (head)")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    ups = up_calls(calls)
    assert ups, calls
    for c in ups:
        args = c.split()
        assert "--no-recreate" in args, c
        assert not set(HELD) & set(args), c
    stack_up = ups[-1].split()
    assert "--no-deps" in stack_up
    assert set(SERVICES) - set(HELD) <= set(stack_up)
    # Existing held containers are only `start`ed (never created or recreated).
    starts = [c for c in calls if " start " in f" {c} "]
    assert sorted(c.split()[-1] for c in starts) == sorted(HELD)
    assert not [c for c in calls if "--force-recreate" in c or " restart " in f" {c} "]


def test_held_service_without_a_container_is_left_alone(fake_bin, tmp_path):
    proc, calls = run_launcher(fake_bin, tmp_path, alembic_current="036 (head)", existing=("scanner",))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [c.split()[-1] for c in calls if " start " in f" {c} "] == ["scanner"]
    assert "held: orchestrator has no container" in proc.stdout


def test_schema_check_does_not_converge_dependencies(fake_bin, tmp_path):
    # A plain `compose run` would recreate postgres/redis if their config changed (e.g. A4 logging).
    _, calls = run_launcher(fake_bin, tmp_path, alembic_current="036 (head)")
    runs = [c for c in calls if " run " in f" {c} "]
    assert runs and all("--no-deps" in c.split() for c in runs), runs


def test_clearing_the_hold_starts_everything(fake_bin, tmp_path):
    proc, calls = run_launcher(fake_bin, tmp_path, alembic_current="036 (head)", held="")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert set(SERVICES) <= set(up_calls(calls)[-1].split())
    assert not [c for c in calls if " start " in f" {c} "]


def test_launcher_is_executable():  # the .desktop entry execs it directly
    assert os.access(LAUNCHER, os.X_OK)
