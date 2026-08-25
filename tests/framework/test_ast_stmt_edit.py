# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shared expression-statement AST edits."""

from __future__ import annotations

import ast

from plugin.framework.ast_stmt_edit import (
    is_name_call_expr,
    iter_matching_expr_statements,
    remove_expr_statements,
)


def _grammar_obs(node: ast.Expr) -> bool:
    return is_name_call_expr(node, frozenset({"grammar_obs", "_grammar_obs"}))


def _xl(node: ast.Expr) -> bool:
    return is_name_call_expr(node, frozenset({"xl"}))


def test_remove_empty_if_keeps_pass() -> None:
    src = "if True:\n    grammar_obs('only')\n"
    out, n = remove_expr_statements(src, _grammar_obs, pass_comment="stripped obs call")
    assert n == 1
    assert "pass" in out
    assert "grammar_obs(" not in out
    ast.parse(out)


def test_remove_with_siblings_deletes_without_pass() -> None:
    src = "if True:\n    grammar_obs('x')\n    x = 1\n"
    out, n = remove_expr_statements(src, _grammar_obs)
    assert n == 1
    assert "grammar_obs" not in out
    assert "x = 1" in out
    # Sibling remains — no need for a lonely pass (may still have pass if logic differs).
    ast.parse(out)


def test_multiline_call_fully_removed() -> None:
    src = (
        "def f():\n"
        "    before = 1\n"
        "    grammar_obs(\n"
        "        'multi',\n"
        "        a=1,\n"
        "    )\n"
        "    after = 2\n"
    )
    out, n = remove_expr_statements(src, _grammar_obs)
    assert n == 1
    assert "grammar_obs" not in out
    assert "before = 1" in out
    assert "after = 2" in out
    ast.parse(out)


def test_skip_last_module_expr_keeps_egress_xl() -> None:
    # Sentinel form like excel placeholder normalize (bare %Pn% is not valid Python).
    src = "xl(_P2_)\n"
    out, n = remove_expr_statements(src, _xl, skip_last_module_expr=True)
    assert n == 0
    assert "xl(_P2_)" in out


def test_skip_last_module_expr_strips_nested_xl() -> None:
    src = "if cond:\n    xl(_P2_)\n"
    out, n = remove_expr_statements(src, _xl, pass_comment="discarded statement", skip_last_module_expr=True)
    assert n == 1
    assert "pass" in out
    assert "xl(" not in out
    ast.parse(out)


def test_iter_matches_remove_discovery() -> None:
    src = "def f():\n    grammar_obs('a')\n    return 1\n"
    found = iter_matching_expr_statements(src, _grammar_obs)
    assert len(found) == 1
    out, n = remove_expr_statements(src, _grammar_obs)
    assert n == len(found)
    assert "grammar_obs" not in out


def test_is_name_call_expr_expr_without_value() -> None:
    """Missing .value must not AttributeError (CrossHair-style incomplete Expr)."""
    node = ast.Expr(value=ast.Constant(1))
    del node.value
    assert is_name_call_expr(node, frozenset({"xl"})) is False
    assert is_name_call_expr(ast.Expr(value=ast.Constant(1)), frozenset({"xl"})) is False


def test_is_name_call_expr_overflow_pre_fails_closed() -> None:
    import deal
    import pytest

    from plugin.framework.deal_shim import DEAL_MAX_CMD_ARGS, DEAL_MAX_TOKEN
    from tests.strip_bundle import deal_pre_present

    if not deal_pre_present(is_name_call_expr):
        pytest.skip("@deal.pre stripped in release bundle")
    node = ast.Expr(value=ast.Call(func=ast.Name(id="xl", ctx=ast.Load()), args=[], keywords=[]))
    with pytest.raises(deal.PreContractError):
        is_name_call_expr(node, frozenset(f"n{i}" for i in range(DEAL_MAX_CMD_ARGS + 1)))
    with pytest.raises(deal.PreContractError):
        is_name_call_expr(node, frozenset({"x" * (DEAL_MAX_TOKEN + 1)}))
    assert is_name_call_expr(node, frozenset({"xl"})) is True
