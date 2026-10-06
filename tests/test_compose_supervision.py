"""A4 (F-M1, F-M10): every long-running compose service is supervised, health-checked and log-bounded,
and the web UI is one of them. Static checks on infra/docker-compose.yml; nothing is started."""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((REPO / "infra" / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]
ONE_SHOT = {"test", "test_volumes_init"}  # `run --rm` / init jobs, not long-running
LONG_RUNNING = sorted(set(SERVICES) - ONE_SHOT)


def test_one_shot_set_is_current():
    for name in ONE_SHOT:
        assert "test" in SERVICES[name].get("profiles", []), name


@pytest.mark.parametrize("name", LONG_RUNNING)
def test_long_running_service_is_supervised(name):
    svc = SERVICES[name]
    assert svc.get("restart") == "unless-stopped", name
    hc = svc.get("healthcheck")
    assert hc and hc.get("test"), f"{name} has no healthcheck"
    assert hc["test"][0] in ("CMD", "CMD-SHELL"), name
    assert hc.get("interval") and hc.get("timeout") and hc.get("retries"), name


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_every_service_rotates_its_logs(name):
    assert SERVICES[name].get("logging") == {"driver": "json-file",
                                              "options": {"max-size": "50m", "max-file": "5"}}, name


def test_web_runs_next_start_on_loopback():
    web = SERVICES["web"]
    assert web["build"]["dockerfile"] == "infra/docker/web.Dockerfile"
    assert web["ports"] == ["127.0.0.1:3000:3000"]
    assert web["environment"]["API_ORIGIN"] == web["build"]["args"]["API_ORIGIN"] == "http://api:8000"
    assert "env_file" not in web  # the UI needs no secrets
    dockerfile = (REPO / "infra" / "docker" / "web.Dockerfile").read_text()
    assert '"next", "start"' in dockerfile.replace("node_modules/.bin/", "")


def test_ports_stay_on_loopback():
    for name, svc in SERVICES.items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)


def test_python_healthchecks_do_not_need_curl():
    # Only the ingest image installs curl; every other Python image must probe with python itself.
    for name, svc in SERVICES.items():
        test = svc.get("healthcheck", {}).get("test", [])
        if any("curl" in part for part in test):  # list form and CMD-SHELL strings
            assert name == "ingest", name


def test_web_runs_unprivileged():
    web = SERVICES["web"]
    assert web["cap_drop"] == ["ALL"] and "no-new-privileges:true" in web["security_opt"] and web["init"] is True
    dockerfile = (REPO / "infra" / "docker" / "web.Dockerfile").read_text()
    assert dockerfile.count("@sha256:") == 2  # both stages pinned by digest
    assert "rm -rf .next/cache" in dockerfile
    ignore = (REPO / "infra" / "docker" / "web.Dockerfile.dockerignore").read_text().split()
    assert "**/.env*" in ignore and "**/node_modules" in ignore


def test_liveness_checks_do_not_depend_on_outside_services():
    # The sandbox is judged by its own serve-loop heartbeat file, the derivatives feed by its own
    # heartbeat key (not by Binance reachability via perp:mark:*).
    sandbox = SERVICES["strategy_sandbox"]
    beat = sandbox["command"][sandbox["command"].index("--heartbeat") + 1]
    assert beat.startswith("/tmp/") and beat in " ".join(sandbox["healthcheck"]["test"])
    feed = " ".join(SERVICES["derivatives_feed"]["healthcheck"]["test"])
    assert "derivatives_feed:heartbeat" in feed and "perp:mark" not in feed


def test_slow_endpoints_get_headroom():
    scanner = SERVICES["scanner"]["healthcheck"]
    assert "timeout=20" in " ".join(scanner["test"]) and scanner["timeout"] == "25s"
    sentiment = " ".join(SERVICES["sentiment"]["healthcheck"]["test"])
    assert "timeout=8" in sentiment and SERVICES["sentiment"]["healthcheck"]["timeout"] == "10s"
