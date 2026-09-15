"""Test Strategy AST enforcement rules for defense-in-depth."""

import pytest
from cryptonite_execution.ast_enforcer import StrategyAstEnforcer

COMPLIANT_STRATEGY = """
class MyPaperStrategy:
    def __init__(self, fast_period: int = 10, slow_period: int = 20):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.history = []

    def on_bar(self, bar):
        self.history.append(bar.close)
        if len(self.history) >= self.slow_period:
            # Emit intent via high-level helper or dispatcher
            pass
"""

VIOLATING_SUBMIT_ORDER = """
class RogueStrategy:
    def on_bar(self, bar):
        # Direct bypass call to Nautilus engine
        self.submit_order(bar)
"""

VIOLATING_EXEC_ENGINE_ACCESS = """
class SneakyStrategy:
    def on_bar(self, bar):
        engine = self.exec_engine
        engine.cancel_all()
"""

VIOLATING_TRADER_ACCESS = """
class DirectTraderStrategy:
    def on_bar(self, bar):
        trader = self._trader
        print(trader)
"""

SYNTAX_ERROR_STRATEGY = """
class BrokenStrategy:
    def on_bar(self, bar)
        missing_colon = True
"""


def test_ast_enforcer_accepts_compliant_strategy():
    valid, violations = StrategyAstEnforcer.enforce(COMPLIANT_STRATEGY)
    assert valid is True
    assert len(violations) == 0


def test_ast_enforcer_flags_direct_order_submission():
    valid, violations = StrategyAstEnforcer.enforce(VIOLATING_SUBMIT_ORDER)
    assert valid is False
    assert len(violations) == 1
    assert "Direct call to 'submit_order' is forbidden" in violations[0]


def test_ast_enforcer_flags_engine_and_trader_access():
    valid, violations = StrategyAstEnforcer.enforce(VIOLATING_EXEC_ENGINE_ACCESS)
    assert valid is False
    assert any("exec_engine" in v for v in violations)

    valid_trader, violations_trader = StrategyAstEnforcer.enforce(VIOLATING_TRADER_ACCESS)
    assert valid_trader is False
    assert any("_trader" in v for v in violations_trader)


def test_ast_enforcer_catches_syntax_error():
    valid, violations = StrategyAstEnforcer.enforce(SYNTAX_ERROR_STRATEGY)
    assert valid is False
    assert any("SyntaxError" in v for v in violations)
