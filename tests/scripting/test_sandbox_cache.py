# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for sandbox_cache (parse + static validation LRU)."""

from __future__ import annotations

import ast
from unittest.mock import patch

import pytest

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Generator

from plugin.contrib.smolagents.local_python_executor import BASE_BUILTIN_MODULES
from plugin.scripting import sandbox_cache as hot_cache
from plugin.scripting.sandbox_cache import clear_python_code_hot_cache, get_hot_entry, validate_sandbox_ast
from plugin.scripting.venv.venv_sandbox import run_sandboxed_code


@pytest.fixture(autouse=True)
def _clear_hot_cache() -> Generator[None, None, None]:
    clear_python_code_hot_cache()
    yield
    clear_python_code_hot_cache()


def test_get_hot_entry_returns_same_module_object() -> None:
    imports = list(BASE_BUILTIN_MODULES)
    e1 = get_hot_entry("result = 1 + 2", imports)
    e2 = get_hot_entry("result = 1 + 2", imports)
    assert e1.error is None
    assert e2.error is None
    assert e1.module is e2.module


def test_different_imports_fingerprint_separate_entries() -> None:
    code = "result = 1"
    a = get_hot_entry(code, ["math"])
    b = get_hot_entry(code, ["json"])
    assert a.module is not b.module


def test_blocked_import_cached_error() -> None:
    imports = list(BASE_BUILTIN_MODULES)
    code = "import os\nresult = 1"
    e1 = get_hot_entry(code, imports)
    assert e1.error is not None
    assert "os" in e1.error
    with patch("plugin.scripting.sandbox_cache.validate_sandbox_ast") as mock_validate:
        e2 = get_hot_entry(code, imports)
        mock_validate.assert_not_called()
    assert e2.error == e1.error


def test_syntax_error_cached() -> None:
    imports = list(BASE_BUILTIN_MODULES)
    code = "result = ("
    e1 = get_hot_entry(code, imports)
    assert e1.error is not None
    assert e1.module is None
    with patch("ast.parse", wraps=ast.parse) as mock_parse:
        e2 = get_hot_entry(code, imports)
        mock_parse.assert_not_called()
    assert e2.error == e1.error


def test_lru_eviction() -> None:
    clear_python_code_hot_cache()
    original_max = hot_cache._max_entries
    hot_cache._max_entries = 2
    try:
        imports = list(BASE_BUILTIN_MODULES)
        key_old = hot_cache._cache_key("result = 1", imports)
        get_hot_entry("result = 1", imports)
        get_hot_entry("result = 2", imports)
        get_hot_entry("result = 3", imports)
        assert key_old not in hot_cache._cache
        assert len(hot_cache._cache) == 2
    finally:
        hot_cache._max_entries = original_max


def test_ast_parse_skipped_on_cache_hit() -> None:
    imports = list(BASE_BUILTIN_MODULES)
    code = "result = 40 + 2"
    with patch("plugin.scripting.sandbox_cache.ast.parse", wraps=ast.parse) as mock_parse:
        get_hot_entry(code, imports)
        get_hot_entry(code, imports)
    assert mock_parse.call_count == 1


def test_fresh_namespace_after_hot_cache_hit() -> None:
    r1 = run_sandboxed_code("x = 41\nresult = x + 1", None)
    assert r1["status"] == "ok"
    r2 = run_sandboxed_code("result = x + 1", None)
    assert r2["status"] == "error"


def test_validate_sandbox_ast_rejects_global() -> None:
    module = ast.parse("global x\nresult = 1")
    err = validate_sandbox_ast(module, list(BASE_BUILTIN_MODULES))
    assert err is not None
    assert "Global" in err


def test_validate_sandbox_ast_writeragent_star_does_not_allow_secrets() -> None:
    """A passed-in writeragent.* must not authorize config or LlmClient."""
    allowed = ["writeragent.*", "plugin.scripting.analysis", "duckdb", "duckdb.*"]
    config_err = validate_sandbox_ast(ast.parse("import writeragent.framework.config"), allowed)
    assert config_err is not None
    assert "writeragent.framework.config" in config_err
    llm_err = validate_sandbox_ast(
        ast.parse("from writeragent.framework.client.llm_client import LlmClient"),
        allowed,
    )
    assert llm_err is not None
    assert "llm_client" in llm_err
    assert validate_sandbox_ast(
        ast.parse("from writeragent.scripting.analysis import run_analysis"),
        allowed,
    ) is None
    assert validate_sandbox_ast(ast.parse("import duckdb"), allowed) is None
    assert validate_sandbox_ast(ast.parse("import duckdb.functional"), allowed) is None


def test_validate_sandbox_ast_allows_future_and_collections_abc() -> None:
    imports = list(BASE_BUILTIN_MODULES)
    assert validate_sandbox_ast(ast.parse("from __future__ import annotations\nresult = 1\n"), imports) is None
    assert validate_sandbox_ast(ast.parse("import collections.abc\n"), imports) is None
    assert validate_sandbox_ast(ast.parse("from collections.abc import Mapping\n"), imports) is None
    assert validate_sandbox_ast(ast.parse("import os\n"), ["*"]) == "Import of os is not allowed."


def test_recursion_error_is_parse_error() -> None:
    imports = list(BASE_BUILTIN_MODULES)
    with patch("ast.parse", side_effect=RecursionError("too deep")) as mock_parse:
        e1 = get_hot_entry("1+1", imports)
        assert e1.module is None
        assert e1.error is not None
        assert "deeply nested" in e1.error

        e2 = get_hot_entry("1+1", imports)
        assert mock_parse.call_count == 1
        assert e2.error == e1.error


def test_long_source_is_not_cached() -> None:
    imports = list(BASE_BUILTIN_MODULES)
    code = "result = 1\n" + " " * 70000
    with patch("ast.parse", wraps=ast.parse) as mock_parse:
        e1 = get_hot_entry(code, imports)
        assert e1.error is None
        key = hot_cache._cache_key(code, imports)
        assert key not in hot_cache._cache

        e2 = get_hot_entry(code, imports)
        assert mock_parse.call_count == 2
        assert e2.error is None


def test_match_forbidden_when_present() -> None:
    from plugin.contrib.smolagents.local_python_executor import _HANDLERS
    from plugin.scripting.sandbox_cache import HotEntry

    if hasattr(ast, "Match"):
        assert ast.Match not in _HANDLERS
    assert getattr(HotEntry, "__dataclass_params__").slots is False


def test_value_error_from_parse_is_cached_parse_error() -> None:
    imports = list(BASE_BUILTIN_MODULES)
    with patch(
        "ast.parse",
        side_effect=ValueError("source code string cannot contain null bytes"),
    ) as mock_parse:
        e1 = get_hot_entry("result = 1", imports)
        assert e1.module is None
        assert (
            e1.error
            == "Code parsing failed due to: ValueError: source code string cannot contain null bytes"
        )

        e2 = get_hot_entry("result = 1", imports)
        assert mock_parse.call_count == 1
        assert e2.error == e1.error


def test_syntax_error_caret_is_under_offset() -> None:
    entry = get_hot_entry("def f(:", list(BASE_BUILTIN_MODULES))
    assert entry.error is not None
    assert entry.error.startswith("Code parsing failed on line 1 due to: SyntaxError:")
    assert entry.error.endswith("def f(:\n" + (" " * 6) + "^")
    assert "line None" not in entry.error


def test_null_byte_source_is_parse_error() -> None:
    build = getattr(hot_cache._build_entry, "__wrapped__", hot_cache._build_entry)
    source = "result = 1\0"
    try:
        ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        expected = f"Code parsing failed due to: {type(exc).__name__}: {exc}"
    else:
        raise AssertionError("ast.parse accepted a null byte")
    entry = build(source, list(BASE_BUILTIN_MODULES))
    assert entry.module is None
    assert entry.error == expected
    assert "line None" not in entry.error
    assert "\n" not in entry.error


def test_allowlist_entries_fit_cache_key() -> None:
    from plugin.contrib.smolagents.utils import BASE_BUILTIN_MODULES as SMOL_BASE_BUILTIN_MODULES
    from plugin.framework.deal_shim import DEAL_MAX_TOKEN
    from plugin.scripting.sandbox import VENV_AUTHORIZED_IMPORTS

    for seq in (SMOL_BASE_BUILTIN_MODULES, VENV_AUTHORIZED_IMPORTS):
        for name in seq:
            assert "\0" not in name
            assert "\n" not in name
            assert len(name) <= DEAL_MAX_TOKEN


def test_validate_sandbox_ast_rejects_importfrom_forbidden_dunder() -> None:
    imports = list(BASE_BUILTIN_MODULES)
    assert validate_sandbox_ast(ast.parse("from re import __loader__"), imports) == "Forbidden access to dunder attribute: __loader__"
    assert validate_sandbox_ast(ast.parse("from re import __spec__"), imports) == "Forbidden access to dunder attribute: __spec__"
    assert validate_sandbox_ast(ast.parse("from re import __dict__"), imports) == "Forbidden access to dunder attribute: __dict__"
    assert validate_sandbox_ast(ast.parse("from re import __version__"), imports) is None
    assert validate_sandbox_ast(ast.parse("from collections.abc import Mapping"), imports) is None


def test_get_hot_entry_top_level_signals_cache_syntax_error() -> None:
    imports = list(BASE_BUILTIN_MODULES)
    for code in ("return 5", "break", "continue", "yield 1"):
        entry = get_hot_entry(code, imports)
        assert entry.module is None
        assert entry.error is not None
        assert "SyntaxError" in entry.error
    assert get_hot_entry("from __future__ import annotations\nx = 1", imports).error is None


def test_sandbox_ast_validation_denylist_table() -> None:
    imports = list(BASE_BUILTIN_MODULES)
    denied = [
        "def f(): yield 1",
        "def f():\n    yield from [1]",
        "@dec\ndef f(): pass",
        "@dec\nclass C: pass",
    ]
    if hasattr(ast, "TryStar"):
        denied.append("try:\n    pass\nexcept* Exception:\n    pass")

    for code in denied:
        entry = get_hot_entry(code, imports)
        assert entry.error is not None

    allowed = [
        "def f(): return 1",
        "class C:\n    pass",
        "try:\n    pass\nexcept Exception:\n    pass",
    ]
    for code in allowed:
        entry = get_hot_entry(code, imports)
        assert entry.error is None
        assert entry.module is not None


def test_all_ast_stmt_and_expr_subclasses_covered() -> None:
    from plugin.contrib.smolagents.local_python_executor import _HANDLERS
    from plugin.scripting.sandbox_cache import _STRUCTURAL_NODES, validate_sandbox_ast

    def get_subclasses(cls: type[ast.AST]) -> set[type[ast.AST]]:
        subclasses: set[type[ast.AST]] = set()
        for sub in cls.__subclasses__():
            subclasses.add(sub)
            subclasses.update(get_subclasses(sub))
        return subclasses

    all_nodes = get_subclasses(ast.stmt) | get_subclasses(ast.expr)
    imports = list(BASE_BUILTIN_MODULES)

    for node_cls in sorted(all_nodes, key=lambda c: c.__name__):
        if node_cls in _HANDLERS or node_cls in _STRUCTURAL_NODES or issubclass(node_cls, ast.Constant):
            continue
        instance = node_cls.__new__(node_cls)
        mod = ast.Module(body=[cast("ast.stmt", instance)], type_ignores=[])
        err = validate_sandbox_ast(mod, imports)
        assert err is not None and f"{node_cls.__name__} is not supported" in err



