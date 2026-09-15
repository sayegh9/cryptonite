"""Structural Isolation Test.

Verifies via AST static analysis that `packages/scanner`, `services/scanner`,
`services/outbox`, and `services/api` contain NO imports of execution, trading,
order routing, or broker execution engines.
"""

import ast
from pathlib import Path

FORBIDDEN_PREFIXES = (
    "nautilus_trader.execution",
    "nautilus_trader.trading",
    "nautilus_trader.orders",
    "nautilus_trader.portfolio",
    "nautilus_trader.risk",
    "ccxt",
    "binance.client",
    "binance.spot",
)

TARGET_DIRS = [
    Path("packages/scanner"),
    Path("services/scanner"),
    Path("services/outbox"),
    Path("services/api"),
]


def test_no_execution_imports():
    root_dir = Path(__file__).parent.parent
    violations = []

    for target_dir in TARGET_DIRS:
        full_dir = root_dir / target_dir
        assert full_dir.exists(), f"Directory {target_dir} must exist"

        for py_file in full_dir.rglob("*.py"):
            with open(py_file, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=str(py_file))

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for forbidden in FORBIDDEN_PREFIXES:
                            if alias.name == forbidden or alias.name.startswith(forbidden + "."):
                                violations.append(
                                    f"{py_file}:{node.lineno} imports forbidden module '{alias.name}'"
                                )
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for forbidden in FORBIDDEN_PREFIXES:
                        if module == forbidden or module.startswith(forbidden + "."):
                            violations.append(
                                f"{py_file}:{node.lineno} imports from forbidden module '{module}'"
                            )

    assert not violations, f"Structural isolation violated:\n" + "\n".join(violations)
