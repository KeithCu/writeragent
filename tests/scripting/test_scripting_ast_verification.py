# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis security verification for sandbox_cache AST validation."""

from __future__ import annotations

import ast

from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.contrib.smolagents.local_python_executor import ALLOWED_DUNDER_ATTRIBUTES
from plugin.scripting.sandbox_cache import validate_sandbox_ast, get_hot_entry
import pytest


def test_validate_sandbox_ast_safe_code() -> None:
    code = "import math\nx = math.sin(1.0)"
    module = ast.parse(code)
    error = validate_sandbox_ast(module, ["math"])
    assert error is None


@pytest.mark.parametrize(
    "value, value_2",
    [
        pytest.param("x = obj.__code__", "Forbidden access to dunder attribute", id="test_validate_sandbox_ast_forbidden_dunder"),
        pytest.param("import subprocess", "Import of subprocess is not allowed", id="test_validate_sandbox_ast_unauthorized_import"),
    ],
)
def test_validate_sandbox_ast_forbidden_dunder(value, value_2) -> None:
    code = value
    module = ast.parse(code)
    error = validate_sandbox_ast(module, ["math"])
    assert error is not None
    assert value_2 in error

_ATTR_SAMPLES = (
    "__code__",
    "__globals__",
    "__subclasses__",
    "__builtins__",
    "__class__",
    "__dict__",
    "__doc__",
    *sorted(ALLOWED_DUNDER_ATTRIBUTES),
    "foo",
    "value",
)


@given(attr=st.sampled_from(_ATTR_SAMPLES))
@settings(max_examples=50)
def test_validate_sandbox_ast_dunder_attribute_security(attr: str) -> None:
    code = f"x = obj.{attr}"
    module = ast.parse(code)
    error = validate_sandbox_ast(module, ["math"])
    if attr.startswith("__") and attr.endswith("__") and attr not in ALLOWED_DUNDER_ATTRIBUTES:
        assert error is not None
        assert "Forbidden access to dunder attribute" in error
    else:
        assert error is None


def test_get_hot_entry_caching() -> None:
    code = "result = 42"
    entry1 = get_hot_entry(code, ["math"])
    assert entry1.error is None
    assert isinstance(entry1.module, ast.Module)

    entry2 = get_hot_entry(code, ["math"])
    assert entry1 is entry2
