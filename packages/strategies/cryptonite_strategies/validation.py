"""Validation of model-written strategy code (AI-first blueprint Phase 4).

**This is defense in depth, not the security boundary.** Python cannot be sandboxed in-process: the
boundary is the isolated ``strategy_sandbox`` container (no network, no secrets, read-only root,
process/memory limits; see ``cryptonite_backtester.sandbox``). This module rejects everything the
strategy contract does not need, so most attacks never reach the sandbox at all:

- only ``from <module> import <name>`` for an explicit per-module name allow-list
  (``ALLOWED_IMPORTS``); no plain ``import``, no star or relative imports. Allowed modules re-export
  dangerous ones (``typing.sys``, ``dataclasses.inspect``, ``statistics.sys``, ``enum.bltns``), so
  names are allow-listed one by one, and at run time an imported name may never be a module
- no identifier or attribute starting with ``_`` (except ``__init__``), which also blocks private
  engine state like ``self._portfolio``
- no reflection or I/O names (``eval``, ``exec``, ``open``, ``getattr``, ...) as names *or* attributes
- no string annotations (``typing.get_type_hints`` would ``eval`` them with the real builtins)
- the order/engine bypasses the paper engine forbids (``submit_order``, ``portfolio``, ``msgbus``, ...)
- module level may only hold imports, constants, a docstring and class definitions
- exactly one ``CryptoniteStrategy`` subclass and one ``CryptoniteStrategyConfig`` subclass
"""

import ast
import builtins
import re
from dataclasses import dataclass, field
from typing import Any

MAX_SOURCE_BYTES = 50_000
_NAUTILUS_CLASS = re.compile(r"^[A-Z][A-Za-z0-9]+$")

# module -> importable names; None means "any public class name, verified to be a class at run time"
ALLOWED_IMPORTS: dict[str, frozenset[str] | None] = {
    "cryptonite_strategies.base": frozenset({"CryptoniteStrategy", "CryptoniteStrategyConfig"}),
    "nautilus_trader.indicators": None,
    "nautilus_trader.model": frozenset({"Bar", "BarType", "OrderSide", "PositionSide", "Price", "Quantity", "InstrumentId"}),
    "math": frozenset({"sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "isnan", "isfinite", "inf", "nan",
                       "pi", "e", "pow", "tanh", "atan", "copysign", "hypot"}),
    "statistics": frozenset({"mean", "fmean", "median", "stdev", "pstdev", "variance", "pvariance"}),
    "decimal": frozenset({"Decimal"}),
    "typing": frozenset({"Any", "Optional", "Union", "List", "Dict", "Tuple", "Sequence"}),
    "collections": frozenset({"deque", "defaultdict", "OrderedDict", "Counter"}),
    "enum": frozenset({"Enum", "IntEnum"}),
}

FORBIDDEN_NAMES = frozenset({
    "eval", "exec", "compile", "open", "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "breakpoint", "input", "memoryview", "help", "exit", "quit", "type", "object", "classmethod",
    "staticmethod", "property", "print", "iter", "next", "id", "hash", "dir",
})

FORBIDDEN_ATTRS = frozenset({
    # Order and engine bypasses (same intent as cryptonite_execution.ast_enforcer)
    "submit_order", "submit_order_list", "cancel_order", "modify_order", "cancel_all_orders",
    "exec_engine", "msgbus", "portfolio", "cache", "clock", "trader", "kernel",
    # str.format / format_map resolve "{0.attr}" fields at run time, invisible to the AST
    "format", "format_map",
    # Frame / code / module introspection
    "f_globals", "f_locals", "f_back", "f_builtins", "f_code", "tb_frame", "tb_next", "gi_frame", "gi_code",
    "cr_frame", "cr_code", "ag_frame", "co_code", "co_consts", "func_globals", "mro", "modules",
}) | FORBIDDEN_NAMES

ALLOWED_DUNDERS = frozenset({"__init__"})

SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance", "len",
        "list", "map", "max", "min", "range", "reversed", "round", "set", "sorted", "str", "sum",
        "tuple", "zip", "super", "ValueError", "TypeError", "KeyError", "IndexError", "ZeroDivisionError",
        "Exception", "NotImplementedError", "True", "False", "None",
    )
}


@dataclass
class ValidationResult:
    ok: bool
    violations: list[str] = field(default_factory=list)
    strategy_class: str | None = None
    config_class: str | None = None


def _name_allowed(module: str, name: str) -> bool:
    allowed = ALLOWED_IMPORTS.get(module, frozenset())
    if allowed is None:
        return bool(_NAUTILUS_CLASS.match(name))
    return name in allowed


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[str] = []

    def _bad(self, node: ast.AST, message: str) -> None:
        self.violations.append(f"Line {getattr(node, 'lineno', '?')}: {message}")

    def visit_Import(self, node: ast.Import) -> None:
        self._bad(node, "plain 'import' is not allowed; use 'from <module> import <name>'")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module or node.module not in ALLOWED_IMPORTS:
            self._bad(node, f"import from '{node.module}' is not allowed")
            return
        for alias in node.names:
            if alias.name == "*" or not _name_allowed(node.module, alias.name):
                self._bad(node, f"'{alias.name}' may not be imported from '{node.module}'")
            if alias.asname and alias.asname.startswith("_"):
                self._bad(node, f"alias '{alias.asname}' is not allowed")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_") and node.attr not in ALLOWED_DUNDERS:
            self._bad(node, f"access to '{node.attr}' is not allowed")
        elif node.attr in FORBIDDEN_ATTRS:
            self._bad(node, f"access to '{node.attr}' is not allowed")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_NAMES or node.id.startswith("_"):
            self._bad(node, f"use of '{node.id}' is not allowed")
        self.generic_visit(node)

    def _check_names(self, node: ast.AST, *names: str | None) -> None:
        for name in names:
            if name and name.startswith("_") and name not in ALLOWED_DUNDERS:
                self._bad(node, f"name '{name}' is not allowed")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        a = node.args
        params = [*a.posonlyargs, *a.args, *a.kwonlyargs, *(p for p in (a.vararg, a.kwarg) if p)]
        self._check_names(node, node.name, *(p.arg for p in params))
        self._check_annotations(node.returns, *(p.annotation for p in params))
        self._no_type_params(node)
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        a = node.args
        self._check_names(node, *(p.arg for p in [*a.posonlyargs, *a.args, *a.kwonlyargs, *(p for p in (a.vararg, a.kwarg) if p)]))
        self.generic_visit(node)

    def visit_Match(self, node: ast.AST) -> None:
        # class patterns read attributes by plain string (MatchClass.kwd_attrs), invisible to visit_Attribute
        self._bad(node, "'match' statements are not allowed")

    def visit_TypeAlias(self, node: ast.AST) -> None:
        self._bad(node, "'type' aliases are not allowed")

    def _no_type_params(self, node: ast.AST) -> None:
        if getattr(node, "type_params", None):
            self._bad(node, "type parameters are not allowed")

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._check_names(node, node.name)
        if node.keywords:
            self._bad(node, "class keywords (e.g. metaclass=) are not allowed")
        if node.decorator_list:
            self._bad(node, "class decorators are not allowed")
        self._no_type_params(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_annotations(node.annotation)
        self.generic_visit(node)

    def _check_annotations(self, *annotations: ast.AST | None) -> None:
        for ann in annotations:
            if ann is not None and any(isinstance(n, ast.Constant) and isinstance(n.value, str) for n in ast.walk(ann)):
                self._bad(ann, "string annotations are not allowed")

    def visit_Global(self, node: ast.Global) -> None:
        self._bad(node, "'global' is not allowed")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self._bad(node, "'nonlocal' is not allowed")


def validate_strategy_source(source: str) -> ValidationResult:
    if len(source.encode()) > MAX_SOURCE_BYTES:
        return ValidationResult(False, [f"source larger than {MAX_SOURCE_BYTES} bytes"])
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return ValidationResult(False, [f"SyntaxError: {e}"])

    visitor = _Visitor()
    visitor.visit(tree)
    violations = list(visitor.violations)

    strategies, configs = [], []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # docstring / constant expression
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.ClassDef):
            bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", None) for b in node.bases}
            if "CryptoniteStrategy" in bases:
                strategies.append(node.name)
            elif "CryptoniteStrategyConfig" in bases:
                configs.append(node.name)
            continue
        if not isinstance(node, ast.Import):  # plain imports are already reported
            violations.append(f"Line {node.lineno}: only imports, constants and class definitions are allowed at module level")

    if len(strategies) != 1:
        violations.append(f"expected exactly one CryptoniteStrategy subclass, found {len(strategies)}")
    if len(configs) != 1:
        violations.append(f"expected exactly one CryptoniteStrategyConfig subclass, found {len(configs)}")
    return ValidationResult(
        not violations, violations,
        strategies[0] if len(strategies) == 1 else None,
        configs[0] if len(configs) == 1 else None,
    )


def _resolve(module_name: str, name: str) -> Any:
    """The imported object, refusing modules (re-exported modules are the escape hatch)."""
    import importlib
    import types

    module = importlib.import_module(module_name)
    obj = getattr(module, name)
    if isinstance(obj, types.ModuleType):
        raise ImportError(f"'{name}' from '{module_name}' is a module")
    if ALLOWED_IMPORTS[module_name] is None and not isinstance(obj, type):
        raise ImportError(f"'{name}' from '{module_name}' is not a class")
    return obj


def resolve_imports(source: str) -> list[str]:
    """Check that every import resolves in this runtime to an allowed, non-module object (e.g. that
    a name really is a class exported by ``nautilus_trader.indicators``). Run after
    ``validate_strategy_source`` passed; imports only allow-listed modules."""
    problems = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module in ALLOWED_IMPORTS:
            for alias in node.names:
                try:
                    _resolve(node.module, alias.name)
                except ImportError as e:
                    problems.append(f"Line {node.lineno}: {e}")
                except AttributeError:
                    problems.append(f"Line {node.lineno}: '{alias.name}' is not exported by '{node.module}'")
    return problems


def _guarded_import(name: str, globals_: Any = None, locals_: Any = None, fromlist: Any = (), level: int = 0):
    if level or name not in ALLOWED_IMPORTS or not fromlist:
        raise ImportError(f"import of '{name}' is not allowed in strategy code")
    for item in fromlist:
        if not _name_allowed(name, item):
            raise ImportError(f"'{item}' may not be imported from '{name}'")
        _resolve(name, item)
    return builtins.__import__(name, globals_, locals_, fromlist, level)


def safe_exec(source: str, module_name: str = "ai_strategy") -> tuple[type, type]:
    """Validate and execute strategy source with restricted builtins; return (strategy_cls, config_cls).
    Only ever called inside the isolated sandbox container."""
    result = validate_strategy_source(source)
    if not result.ok:
        raise ValueError("strategy code rejected: " + "; ".join(result.violations))
    namespace: dict[str, Any] = {
        "__builtins__": {**SAFE_BUILTINS, "__import__": _guarded_import, "__build_class__": builtins.__build_class__},
        "__name__": module_name,
    }
    exec(compile(source, f"<{module_name}>", "exec"), namespace)  # noqa: S102 - validated, restricted builtins
    return namespace[result.strategy_class], namespace[result.config_class]
