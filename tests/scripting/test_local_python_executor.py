# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import ast

import pytest

from plugin.contrib.smolagents.local_python_executor import (
    BASE_BUILTIN_MODULES,
    InterpreterError,
    LocalPythonExecutor,
    evaluate_generatorexp,
)

_MIXED_GRID = [
    [1.0, "label", 10.0],
    [2.0, "x", 20.0],
    [3.0, "y", 30.0],
    [4.0, "z", 40.0],
]


def test_nested_generatorexp_via_evaluate_generatorexp():
    """Inner loop variable must bind when multiple generators are nested."""
    state = {"data": [[1, 2, 3], [4, 5, 6]]}
    tree = ast.parse("(v for row in data for v in row)", mode="eval")
    gen = evaluate_generatorexp(
        tree.body,
        state,
        static_tools={},
        custom_tools={},
        authorized_imports=BASE_BUILTIN_MODULES,
    )
    assert list(gen) == [1, 2, 3, 4, 5, 6]


def test_nested_generatorexp_via_local_python_executor():
    """=PYTHON() path: sum(nested genexp) after send_tools merges builtins."""
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})
    executor.send_variables({"data": _MIXED_GRID})
    executor(
        "result = float(sum(v for row in data for v in row if isinstance(v, (int, float))))",
    )
    assert executor.state["result"] == 110.0


def test_single_generatorexp_still_works():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})
    executor("result = sum(x for x in (1, 2, 3))")
    assert executor.state["result"] == 6


def _executor_with_dummy_version():
    class _Dummy:
        __version__ = "1.2.3"

    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})
    executor.send_variables({"np": _Dummy()})
    return executor


@pytest.mark.parametrize(
    "value",
    [
        # Scientific notebooks print np.__version__; the blanket dunder deny blocked that.
        pytest.param("result = np.__version__", id="test_executor_allows_version_attribute"),
        pytest.param('result = getattr(np, "__version__")', id="test_executor_allows_version_via_getattr"),
    ],
)
def test_executor_allows_version_attribute(value):
    executor = _executor_with_dummy_version()
    executor(value)
    assert executor.state["result"] == "1.2.3"

def test_executor_still_forbids_class_dunder():
    executor = _executor_with_dummy_version()
    try:
        executor("result = np.__class__")
    except InterpreterError as err:
        assert "Forbidden access to dunder attribute" in str(err)
        assert "__class__" in str(err)
    else:
        raise AssertionError("np.__class__ must remain forbidden")


def test_local_python_executor_does_not_import_tools():
    """LibrePy ships LPE without the smolagents Tool chain."""
    from pathlib import Path

    import plugin.contrib.smolagents.local_python_executor as lpe

    tree = ast.parse(Path(lpe.__file__).read_text(encoding="utf-8"))
    mods: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
    assert ".tools" not in mods
    assert "tools" not in mods


def test_direct_import_os_still_forbidden():
    executor = LocalPythonExecutor(additional_authorized_imports=["platform"])
    executor.send_tools({})
    try:
        executor("import os")
    except InterpreterError as err:
        assert "os" in str(err).lower() or "not allowed" in str(err).lower()
    else:
        raise AssertionError("import os must stay unauthorized")


@pytest.mark.parametrize(
    "value, value_2, value_3",
    [
        # Allowed platform must not re-export raw os (get_safe_module used to return os as-is).
        pytest.param("platform", "import platform\nresult = platform.os", "platform.os must not resolve to a live os module", id="test_platform_os_is_not_the_os_module"),
        pytest.param("writeragent", "import writeragent\nresult = writeragent.sys", "writeragent.sys must not resolve to a live sys module", id="test_writeragent_sys_is_not_the_sys_module"),
    ],
)
def test_platform_os_is_not_the_os_module(value, value_2, value_3):
    executor = LocalPythonExecutor(additional_authorized_imports=[value])
    executor.send_tools({})
    try:
        executor(value_2)
    except (InterpreterError, AttributeError):
        return
    raise AssertionError(value_3)

def test_evaluate_import_does_not_dump_allowlist():
    """Executor import failure must not dump the allowlist or expose duckdb."""
    import pytest

    executor = LocalPythonExecutor(additional_authorized_imports=["duckdb"])
    executor.send_tools({})
    with pytest.raises(InterpreterError) as exc_info:
        executor("import os")
    msg = str(exc_info.value)
    assert msg == "Import of os is not allowed."
    assert "duckdb" not in msg
    assert "Authorized imports" not in msg
