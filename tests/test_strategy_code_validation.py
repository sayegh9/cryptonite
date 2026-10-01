"""Allow-list validation + restricted execution of model-written strategy code (blueprint Phase 4)."""

from pathlib import Path

import pytest
from cryptonite_strategies.validation import safe_exec, validate_strategy_source

STRATEGIES = Path(__file__).resolve().parents[1] / "packages" / "strategies" / "cryptonite_strategies"

GOOD = '''
"""A tiny valid strategy."""
from typing import Any
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model import Bar
from cryptonite_strategies.base import CryptoniteStrategy, CryptoniteStrategyConfig

PERIOD = 20


class MyConfig(CryptoniteStrategyConfig):
    def __init__(self, instrument_id: str, bar_type: str, period: int = PERIOD, trade_size: float = 0.01,
                 evaluation_start_ns: int = 0, **kwargs: Any) -> None:
        super().__init__(instrument_id=instrument_id, bar_type=bar_type, trade_size=trade_size,
                         evaluation_start_ns=evaluation_start_ns, **kwargs)
        self.period = int(period)


class MyStrategy(CryptoniteStrategy):
    def __init__(self, config: MyConfig) -> None:
        super().__init__(config)
        self.ema = ExponentialMovingAverage(config.period)

    def on_start(self) -> None:
        super().on_start()
        self.register_indicator_for_bars(self.bar_type, self.ema)

    def on_bar(self, bar: Bar) -> None:
        if not self.ema.initialized:
            return
        if self.is_flat() and float(bar.close) > self.ema.value:
            self.buy_market()
        elif self.is_long() and float(bar.close) < self.ema.value:
            self.close_all()
'''


def test_a_well_formed_strategy_passes_and_loads():
    result = validate_strategy_source(GOOD)
    assert result.ok, result.violations
    assert (result.strategy_class, result.config_class) == ("MyStrategy", "MyConfig")
    strategy_cls, config_cls = safe_exec(GOOD)
    assert strategy_cls.__name__ == "MyStrategy" and config_cls.__name__ == "MyConfig"


@pytest.mark.parametrize("snippet", [
    "import os",
    "import subprocess",
    "from socket import socket",
    "from . import base",
    "import nautilus_trader.live",
    "x = open('/etc/passwd')",
    "x = __import__('os')",
    "x = getattr(object, 'x')",
    "x = ().__class__.__bases__[0].__subclasses__()",
    "x = '{0.__class__}'.format(1)",
    "x = eval('1')",
    "x = globals()",
    "print(1)",  # module-level statements beyond constants/classes
    # Security audit 2026-09-26: verified escapes, now regression tests
    "from typing import sys",
    "from dataclasses import inspect",
    "from statistics import sys as s",
    "from enum import bltns",
    "from typing import get_type_hints",
    "from typing import ForwardRef",
    "from functools import partial",
    "from collections import namedtuple",
    "class H:\n    x: \"__import__('subprocess').check_output(['id'])\"",
    "X = 1\nclass Q:\n    y = X.real._private",
    # re-audit: match class patterns read attributes by string; unchecked parameter kinds; PEP 695
    "class M:\n    def f(self, v):\n        match v:\n            case str(__class__=c):\n                return c",
    "class P:\n    def f(self, _x, /):\n        return 1",
    "class P:\n    def f(self, *_a):\n        return 1",
    "class P:\n    def f(self, **kw: 'x'):\n        return 1",
    "type X = int",
    "class G[T]:\n    pass",
])
def test_escapes_and_side_effects_are_rejected(snippet):
    assert not validate_strategy_source(GOOD + "\n" + snippet + "\n").ok


def test_private_attributes_reflection_attributes_and_string_annotations_are_rejected():
    for bad in ("x = self._portfolio", "x = self.log.eval", "x = self.log.modules"):
        assert not validate_strategy_source(GOOD.replace("self.buy_market()", bad)).ok, bad
    annotated = GOOD.replace("def on_bar(self, bar: Bar) -> None:", "def on_bar(self, bar: \"Bar\") -> None:")
    assert not validate_strategy_source(annotated).ok


def test_runtime_import_hook_refuses_modules_and_unlisted_names():
    from cryptonite_strategies import validation

    for module, names in (("typing", ("sys",)), ("statistics", ("sys",)), ("enum", ("bltns",)), ("typing", ("get_type_hints",))):
        with pytest.raises(ImportError):
            validation._guarded_import(module, fromlist=names)
    with pytest.raises(ImportError):
        validation._guarded_import("typing")  # plain import


def test_order_and_engine_bypasses_are_rejected():
    bypass = GOOD.replace("self.buy_market()", "self.submit_order(None)")
    assert not validate_strategy_source(bypass).ok
    assert not validate_strategy_source(GOOD.replace("self.buy_market()", "x = self.portfolio")).ok


def test_exactly_one_strategy_and_config_class():
    assert not validate_strategy_source(GOOD.replace("class MyStrategy(CryptoniteStrategy)", "class MyStrategy(object)")).ok


def test_runtime_import_hook_blocks_what_the_ast_cannot_see():
    # Even if validation were bypassed, the restricted builtins have no open/eval and imports are guarded
    from cryptonite_strategies import validation

    with pytest.raises(ImportError):
        validation._guarded_import("os", fromlist=("path",))
    for name in ("open", "eval", "print", "type", "object", "getattr"):
        assert name not in validation.SAFE_BUILTINS


def test_imports_must_resolve_in_this_nautilus_version():
    from cryptonite_strategies.validation import resolve_imports

    assert resolve_imports(GOOD) == []
    stale = GOOD.replace("from nautilus_trader.indicators import ExponentialMovingAverage",
                         "from nautilus_trader.indicators.ema import ExponentialMovingAverage")
    assert not validate_strategy_source(stale).ok  # submodule paths are not on the allow-list
    wrong_name = GOOD.replace("import ExponentialMovingAverage", "import ExponentialMovingAverage, NotAnIndicator")
    assert any("NotAnIndicator" in p for p in resolve_imports(wrong_name))
