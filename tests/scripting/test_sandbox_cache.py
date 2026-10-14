# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for sandbox_cache (parse + static validation LRU)."""

from __future__ import annotations

import ast
from unittest.mock import patch

import pytest

from plugin.contrib.smolagents.local_python_executor import BASE_BUILTIN_MODULES
from plugin.scripting import sandbox_cache as hot_cache
from plugin.scripting.sandbox_cache import clear_python_code_hot_cache, get_hot_entry, validate_sandbox_ast
from plugin.scripting.venv.venv_sandbox import run_sandboxed_code


@pytest.fixture(autouse=True)
def _clear_hot_cache() -> None:
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
    from plugin.scripting.sandbox_cache import _FORBIDDEN_NODE_TYPES, HotEntry

    if hasattr(ast, "Match"):
        assert ast.Match in _FORBIDDEN_NODE_TYPES
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
    from plugin.scripting.sandbox import CALC_AUTHORIZED_IMPORTS, VENV_AUTHORIZED_IMPORTS

    for seq in (SMOL_BASE_BUILTIN_MODULES, VENV_AUTHORIZED_IMPORTS, CALC_AUTHORIZED_IMPORTS):
        for name in seq:
            assert "\0" not in name
            assert "\n" not in name
            assert len(name) <= DEAL_MAX_TOKEN

