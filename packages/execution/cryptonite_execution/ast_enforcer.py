"""AST enforcement for strategy source validation (defense-in-depth)."""

import ast
from typing import Any

FORBIDDEN_CALL_ATTRS = {
    "submit_order",
    "submit_order_list",
    "cancel_order",
    "modify_order",
    "cancel_all_orders",
}

FORBIDDEN_PROPERTY_ATTRS = {
    "exec_engine",
    "_exec_engine",
    "_trader",
    "_kernel",
    "msgbus",
    "_msgbus",
    "portfolio",
}


class StrategySecurityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        # Check method calls on self or engines
        if isinstance(node.func, ast.Attribute):
            attr_name = node.func.attr
            if attr_name in FORBIDDEN_CALL_ATTRS:
                self.violations.append(
                    f"Line {node.lineno}: Direct call to '{attr_name}' is forbidden. "
                    "Strategies must yield or pass order intents to GuardedOrderDispatcher."
                )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Check forbidden internal engine attribute accesses
        if node.attr in FORBIDDEN_PROPERTY_ATTRS:
            self.violations.append(
                f"Line {node.lineno}: Access to internal attribute '{node.attr}' is forbidden."
            )
        self.generic_visit(node)


class StrategyAstEnforcer:
    """Validates that a strategy does not attempt direct execution engine or order bypasses."""

    @staticmethod
    def enforce(source_code: str) -> tuple[bool, list[str]]:
        try:
            tree = ast.parse(source_code)
        except SyntaxError as e:
            return False, [f"SyntaxError parsing strategy code: {e}"]

        visitor = StrategySecurityVisitor()
        visitor.visit(tree)
        return len(visitor.violations) == 0, visitor.violations
