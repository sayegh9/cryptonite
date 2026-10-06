"""A5 (F-M2) and A4: the desktop launcher must not migrate the DB or touch held services, and hands
:3000 to the compose `web` service without ever starting a host `pnpm start`.

Runs the real scripts/launcher/cryptonite-up.sh with docker, curl, pnpm, setsid and notify-send
replaced by fakes on PATH that log every call. Nothing reaches Docker, the DB or the network."""

import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "scripts" / "launcher" / "cryptonite-up.sh"
HELD = ("orchestrator", "scanner", "debate_worker", "learner")
SERVICES = ("redis", "postgres", "exit_monitor", "api", "notifier", "derivatives_feed", "orchestrator",
            "strategy_sandbox", "agent_worker", "backtester", "learner", "outbox", "sentiment",
            "debate_worker", "ingest", "scanner")
OPT_IN = ("research_worker", "executor", "test", "test_volumes_init")

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
case " $* " in
  *" --profile * config --services "*) printf '%s\\n' $FAKE_ALL_SERVICES ;;
  *" config --services "*) printf '%s\\n' $FAKE_SERVICES ;;
  *" alembic "*" current "*) echo "$FAKE_ALEMBIC_CURRENT" ;;
  *" ps -q --status running web "*) [[ -f "$FAKE_STATE/web-up" ]] && echo id-web ;;
  *" ps -a -q "*) svc="${@: -1}"; [[ " $FAKE_EXISTING " == *" $svc "* ]] && echo "id-$svc" ;;
  *" up "*) [[ " $* " == *" web "* ]] && touch "$FAKE_STATE/web-up" ;;
esac
exit 0
"""

# :3000 answers once the fake compose started `web`, or while FAKE_PORT_HOLDER (a real pid) lives.
FAKE_CURL = """#!/usr/bin/env bash
url="${@: -1}"
if [[ "$url" == *":3000"* ]]; then
  [[ -f "$FAKE_STATE/web-up" ]] && exit 0
  [[ -n "${FAKE_PORT_HOLDER:-}" ]] && kill -0 "$FAKE_PORT_HOLDER" 2>/dev/null && exit 0
  exit 7
fi
exit 0
"""

FAKE_LOGGER = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$FAKE_CALLS"
exit 0
"""


@pytest.fixture
def fake_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "curl").write_text(FAKE_CURL)
    for name in ("pnpm", "setsid", "notify-send", "xdg-open"):
        (bin_dir / name).write_text(FAKE_LOGGER)
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir


def run_launcher(fake_bin, tmp_path, *, alembic_current="036 (head)", held=None, existing=HELD,
                 services=SERVICES, port_holder=None):
    log, calls_log, state = tmp_path / "docker.log", tmp_path / "calls.log", tmp_path / "fake-state"
    state.mkdir(exist_ok=True)
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CRYPTONITE_NO_BROWSER": "1",
        "FAKE_LOG": str(log),
        "FAKE_CALLS": str(calls_log),
        "FAKE_STATE": str(state),
        "FAKE_SERVICES": " ".join(services),
        "FAKE_ALL_SERVICES": " ".join((*services, *OPT_IN)),
        "FAKE_ALEMBIC_CURRENT": alembic_current,
        "FAKE_EXISTING": " ".join(existing),
    }
    if held is not None:
        env["CRYPTONITE_HELD_SERVICES"] = held
    if port_holder is not None:
        env["FAKE_PORT_HOLDER"] = str(port_holder)
    proc = subprocess.run(["bash", str(LAUNCHER)], env=env, capture_output=True, text=True, timeout=90)
    calls = log.read_text().splitlines() if log.exists() else []
    other = calls_log.read_text().splitlines() if calls_log.exists() else []
    return proc, calls, other


def up_calls(calls):
    return [c for c in calls if " up " in f" {c} "]


def start_calls(calls):
    return [c for c in calls if " start " in f" {c} "]


@pytest.fixture
def port_holder():
    """A real process in its own group standing in for a host server on :3000 (reparented, so it is
    reaped as soon as it dies and kill -0 stops seeing it)."""
    out = subprocess.run(["sh", "-c", "setsid sleep 300 >/dev/null 2>&1 & echo $!"],
                         capture_output=True, text=True, check=True)
    pid = int(out.stdout.strip())
    yield pid
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# --- A5: migrations and the hold ----------------------------------------------------------------------

def test_pending_migration_is_reported_not_applied(fake_bin, tmp_path):
    proc, calls, _ = run_launcher(fake_bin, tmp_path, alembic_current="035_cross_mandate_exposure")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not [c for c in calls if "upgrade" in c], calls
    assert any("alembic" in c and c.endswith("current") for c in calls)
    assert "schema is NOT at head" in proc.stdout


def test_schema_at_head_passes_quietly(fake_bin, tmp_path):
    proc, _, _ = run_launcher(fake_bin, tmp_path, alembic_current="036_shadow_mode (head)")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "schema at head" in proc.stdout and "NOT at head" not in proc.stdout


def test_held_services_are_never_upped_or_recreated(fake_bin, tmp_path):
    proc, calls, _ = run_launcher(fake_bin, tmp_path)
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
    assert sorted(c.split()[-1] for c in start_calls(calls)) == sorted(HELD)
    assert not [c for c in calls if "--force-recreate" in c or " restart " in f" {c} " or "--build" in c]


def test_held_service_without_a_container_is_left_alone(fake_bin, tmp_path):
    proc, calls, _ = run_launcher(fake_bin, tmp_path, existing=("scanner",))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [c.split()[-1] for c in start_calls(calls)] == ["scanner"]
    assert "held: orchestrator has no container" in proc.stdout


def test_schema_check_does_not_converge_dependencies(fake_bin, tmp_path):
    # A plain `compose run` would recreate postgres/redis if their config changed (e.g. A4 logging).
    _, calls, _ = run_launcher(fake_bin, tmp_path)
    runs = [c for c in calls if " run " in f" {c} "]
    assert runs and all("--no-deps" in c.split() for c in runs), runs


def test_clearing_the_hold_starts_everything(fake_bin, tmp_path):
    proc, calls, _ = run_launcher(fake_bin, tmp_path, held="")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert set(SERVICES) <= set(up_calls(calls)[-1].split())
    assert not start_calls(calls)


def test_a_misspelled_held_service_fails_before_anything_starts(fake_bin, tmp_path):
    proc, calls, _ = run_launcher(fake_bin, tmp_path, held="orchestrater scanner")
    assert proc.returncode == 1
    assert "'orchestrater', which is not a compose service" in proc.stderr
    assert not up_calls(calls) and not start_calls(calls)


def test_opt_in_services_running_before_the_last_stop_come_back(fake_bin, tmp_path):
    state = tmp_path / "state" / "cryptonite"
    state.mkdir(parents=True)
    (state / "running-services").write_text("api\nresearch_worker\nscanner\n")
    proc, calls, _ = run_launcher(fake_bin, tmp_path, existing=(*HELD, "research_worker"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    restored = [c for c in start_calls(calls) if c.split()[-1] == "research_worker"]
    assert len(restored) == 1 and "--profile *" in restored[0]
    assert not [c for c in start_calls(calls) if c.split()[-1] == "api"]  # already in the stack list
    assert len([c for c in start_calls(calls) if c.split()[-1] == "scanner"]) == 1  # held loop only
    assert "opt-in: research_worker restarted" in proc.stdout
    assert not (state / "running-services").exists()


def test_launcher_is_executable():  # the .desktop entry execs it directly
    assert os.access(LAUNCHER, os.X_OK)


# --- A4: the web UI is a compose service -----------------------------------------------------------------

def test_compose_web_is_waited_for_and_no_host_server_is_started(fake_bin, tmp_path):
    proc, calls, other = run_launcher(fake_bin, tmp_path, services=(*SERVICES, "web"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "web" in up_calls(calls)[-1].split()
    assert "serving on http://localhost:3000 (compose service)" in proc.stdout
    assert not [c for c in other if c.startswith(("pnpm", "setsid"))], other
    assert any(c.startswith("notify-send") and "Cryptonite is up" in c for c in other), other


def test_host_web_from_an_older_launcher_is_stopped_before_compose_binds_3000(fake_bin, tmp_path, port_holder):
    state = tmp_path / "state" / "cryptonite"
    state.mkdir(parents=True)
    (state / "web.pid").write_text(str(port_holder))
    proc, calls, _ = run_launcher(fake_bin, tmp_path, services=(*SERVICES, "web"), port_holder=port_holder)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    deadline = time.monotonic() + 5
    while _alive(port_holder) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(port_holder)
    assert not (state / "web.pid").exists()
    assert "web" in up_calls(calls)[-1].split()
    assert "stopped the host web UI" in proc.stdout


def test_a_foreign_server_on_3000_keeps_the_web_container_out(fake_bin, tmp_path, port_holder):
    proc, calls, _ = run_launcher(fake_bin, tmp_path, services=(*SERVICES, "web"), port_holder=port_holder)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _alive(port_holder)  # not ours: never killed
    assert "web" not in up_calls(calls)[-1].split()
    assert "something else serves :3000" in proc.stdout
    assert "already serving on http://localhost:3000 (not the compose service)" in proc.stdout


def test_without_a_web_service_it_says_how_to_start_one(fake_bin, tmp_path):
    proc, _, other = run_launcher(fake_bin, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "up -d --build --no-deps web" in proc.stdout
    assert not [c for c in other if c.startswith(("pnpm", "setsid"))], other
