"""Autonomous debate execution stays frozen until mandates exist (AI-first blueprint D11, A0.6)."""

import importlib.util
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parents[1] / "services" / "debate_worker" / "main.py"


@pytest.fixture(scope="module")
def debate_main():
    spec = importlib.util.spec_from_file_location("debate_worker_main", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("configured", ["autonomous", "interactive", None, "anything"])
def test_every_setting_resolves_to_interactive(debate_main, configured):
    assert debate_main.resolve_execution_mode(configured) == "interactive"


def test_worker_reads_the_mode_only_through_the_freeze():
    source = MAIN.read_text()
    assert source.count("resolve_execution_mode(mode_row") == 2
    assert 'mode_row["value"] if mode_row else "interactive"' not in source
