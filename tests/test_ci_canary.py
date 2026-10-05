"""Temporary C1 canary: proves CI goes red on a failing test and a ruff finding. Reverted in the next commit."""
import os


def test_ci_canary_fails():
    assert 1 == 2, "C1 canary: CI must block this"
