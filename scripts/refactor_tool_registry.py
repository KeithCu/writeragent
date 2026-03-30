#!/usr/bin/env python3
"""Utilities for Literal / string-literal audits.

- Default: read-only AST audit of send-handler FSM string usage (safe).
- ``--audit-uieffect``: collect ``kind=`` / positional kind strings for UI channel effects.
- ``--audit-chat-fsm``: send-handler audit plus UI-effect kind audit.
- ``--tool-registry``: legacy refactor of JSON schema lines in tool_registry.py only.

Do not use the tool-registry rewrite on arbitrary Python files.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

SEND_HANDLER_AUDIT_PATHS = (
    "plugin/modules/chatbot/state_machine.py",
    "plugin/modules/chatbot/send_handlers.py",
    "plugin/tests/test_state_machine.py",
)

TOOL_REGISTRY_PATH = "plugin/framework/tool_registry.py"

UI_EFFECT_KIND_PATHS = (
    "plugin/modules/chatbot/tool_loop_state.py",
    "plugin/modules/chatbot/state_machine.py",
)

# Expected set for ``UIEffectKind`` in plugin/framework/types.py
EXPECTED_UI_EFFECT_KINDS = frozenset({"append", "status", "debug", "info"})


def _str_constant(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class SendHandlerLiteralAudit(ast.NodeVisitor):
    """Collect string literals tied to SendHandlerState / CompleteJobEffect / handler_type / status."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handler_type_literals: set[str] = set()
        self.status_literals: set[str] = set()
        self.complete_job_literals: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        name: str | None = None
        if isinstance(fn, ast.Name):
            name = fn.id
        elif isinstance(fn, ast.Attribute):
            name = fn.attr

        if name == "SendHandlerState":
            for kw in node.keywords:
                if kw.arg == "handler_type":
                    s = _str_constant(kw.value)
                    if s is not None:
                        self.handler_type_literals.add(s)
                elif kw.arg == "status":
                    s = _str_constant(kw.value)
                    if s is not None:
                        self.status_literals.add(s)
        elif name == "CompleteJobEffect":
            for arg in node.args:
                s = _str_constant(arg)
                if s is not None:
                    self.complete_job_literals.add(s)
        elif name == "replace" and isinstance(fn, ast.Attribute):
            # dataclasses.replace(state, status='error', ...)
            for kw in node.keywords:
                if kw.arg == "status":
                    s = _str_constant(kw.value)
                    if s is not None:
                        self.status_literals.add(s)

        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        # state.handler_type == 'audio', x in ('a','b')
        self._compare_chain(node)
        self.generic_visit(node)

    def _compare_chain(self, node: ast.Compare) -> None:
        left = node.left
        for i, op in enumerate(node.ops):
            right = node.comparators[i]
            if isinstance(op, ast.Eq):
                self._maybe_record_attr_eq(left, right)
                self._maybe_record_attr_eq(right, left)
            elif isinstance(op, ast.In):
                if isinstance(left, ast.Attribute) and left.attr == "handler_type":
                    self._strings_from_tuple_or_set(right, self.handler_type_literals)
            left = right

    def _maybe_record_attr_eq(self, a: ast.AST, b: ast.AST) -> None:
        if not isinstance(a, ast.Attribute):
            return
        s = _str_constant(b)
        if s is None:
            return
        if a.attr == "handler_type":
            self.handler_type_literals.add(s)
        elif a.attr == "status":
            self.status_literals.add(s)

    def _strings_from_tuple_or_set(self, node: ast.AST, out: set[str]) -> None:
        if isinstance(node, (ast.Tuple, ast.Set)):
            for elt in node.elts:
                s = _str_constant(elt)
                if s is not None:
                    out.add(s)


class UIEffectKindAudit(ast.NodeVisitor):
    """Collect ``kind`` for ToolLoopUIEffect(kind=...) and SendHandlerUIEffect(kind, ...)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.kind_literals: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        name: str | None = None
        if isinstance(fn, ast.Name):
            name = fn.id
        elif isinstance(fn, ast.Attribute):
            name = fn.attr

        if name == "ToolLoopUIEffect":
            for kw in node.keywords:
                if kw.arg == "kind":
                    s = _str_constant(kw.value)
                    if s is not None:
                        self.kind_literals.add(s)
        elif name == "SendHandlerUIEffect" and node.args:
            s = _str_constant(node.args[0])
            if s is not None:
                self.kind_literals.add(s)

        self.generic_visit(node)


def audit_uieffect_kinds(paths: tuple[str, ...] | list[str] | None = None) -> int:
    """Print UI effect ``kind`` literals; warn if outside EXPECTED_UI_EFFECT_KINDS."""
    rels = list(paths) if paths is not None else list(UI_EFFECT_KIND_PATHS)
    all_kinds: set[str] = set()
    for rel in rels:
        p = REPO_ROOT / rel
        if not p.is_file():
            print(f"Missing: {p}", file=sys.stderr)
            return 1
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        v = UIEffectKindAudit(p)
        v.visit(tree)
        print(f"\n== UIEffect kind audit: {rel} ==")
        print("  kind literals:", sorted(v.kind_literals))
        all_kinds |= v.kind_literals
    print("\n== Union (UI channel kind strings) ==")
    print("  kind:", sorted(all_kinds))
    unexpected = all_kinds - EXPECTED_UI_EFFECT_KINDS
    missing_expected = EXPECTED_UI_EFFECT_KINDS - all_kinds
    if unexpected:
        print("  WARNING: kinds not in UIEffectKind union:", sorted(unexpected))
    if missing_expected:
        print("  NOTE: union allows but codebase did not emit:", sorted(missing_expected))
    return 0


def audit_send_handler_fsms(paths: tuple[str, ...] | list[str]) -> int:
    """Print collected literals; exit 1 if a file is missing."""
    all_ht: set[str] = set()
    all_st: set[str] = set()
    all_cj: set[str] = set()
    for rel in paths:
        p = REPO_ROOT / rel
        if not p.is_file():
            print(f"Missing: {p}", file=sys.stderr)
            return 1
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        v = SendHandlerLiteralAudit(p)
        v.visit(tree)
        print(f"\n== {rel} ==")
        print("  handler_type literals:", sorted(v.handler_type_literals))
        print("  status literals:", sorted(v.status_literals))
        print("  CompleteJobEffect literals:", sorted(v.complete_job_literals))
        all_ht |= v.handler_type_literals
        all_st |= v.status_literals
        all_cj |= v.complete_job_literals
    print("\n== Union (all files) ==")
    print("  handler_type:", sorted(all_ht))
    print("  status:", sorted(all_st))
    print("  CompleteJobEffect:", sorted(all_cj))
    return 0


def refactor_tool_registry_status_literals() -> int:
    """Legacy: add Literal to typing import and rewrite schema status lines in tool_registry."""
    file_path = REPO_ROOT / TOOL_REGISTRY_PATH
    if not file_path.is_file():
        print(f"Missing: {file_path}", file=sys.stderr)
        return 1
    content = file_path.read_text(encoding="utf-8")
    if "from typing import" in content and "Literal" not in content:
        content = content.replace(
            "from typing import",
            "from typing import Literal, ",
        )
        print(f"Added Literal to typing import in {TOOL_REGISTRY_PATH}")
    else:
        print(f"Literal already imported or no typing import in {TOOL_REGISTRY_PATH}")
    content = content.replace('"status": "ok"', '"status": Literal["ok"]')
    content = content.replace('"status": "error"', '"status": Literal["error"]')
    file_path.write_text(content, encoding="utf-8")
    print(f"Refactored status fields in {TOOL_REGISTRY_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit-send-handler",
        action="store_true",
        help="AST scan of send-handler FSM files only",
    )
    parser.add_argument(
        "--audit-uieffect",
        action="store_true",
        help="AST scan of ToolLoopUIEffect / SendHandlerUIEffect kind strings",
    )
    parser.add_argument(
        "--audit-chat-fsm",
        action="store_true",
        help="Run send-handler audit then UI-effect kind audit",
    )
    parser.add_argument(
        "--tool-registry",
        action="store_true",
        help="Rewrite tool_registry.py schema status strings (destructive)",
    )
    args = parser.parse_args(argv)

    if args.tool_registry:
        return refactor_tool_registry_status_literals()
    if args.audit_chat_fsm:
        rc = audit_send_handler_fsms(SEND_HANDLER_AUDIT_PATHS)
        if rc != 0:
            return rc
        return audit_uieffect_kinds()
    if args.audit_uieffect:
        return audit_uieffect_kinds()
    if args.audit_send_handler:
        return audit_send_handler_fsms(SEND_HANDLER_AUDIT_PATHS)
    return audit_send_handler_fsms(SEND_HANDLER_AUDIT_PATHS)


if __name__ == "__main__":
    raise SystemExit(main())
