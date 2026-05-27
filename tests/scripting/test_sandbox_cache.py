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
from plugin.scripting.worker_harness import _execute_request
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


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
    r1 = _execute_request("x = 41\nresult = x + 1", None)
    assert r1["status"] == "ok"
    r2 = _execute_request("result = x + 1", None)
    assert r2["status"] == "error"


def test_validate_sandbox_ast_rejects_global() -> None:
    module = ast.parse("global x\nresult = 1")
    err = validate_sandbox_ast(module, list(BASE_BUILTIN_MODULES))
    assert err is not None
    assert "Global" in err
