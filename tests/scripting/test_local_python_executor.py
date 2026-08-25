# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import ast

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


def test_executor_allows_version_attribute():
    """Scientific notebooks print np.__version__; the blanket dunder deny blocked that."""
    executor = _executor_with_dummy_version()
    executor("result = np.__version__")
    assert executor.state["result"] == "1.2.3"


def test_executor_allows_version_via_getattr():
    executor = _executor_with_dummy_version()
    executor('result = getattr(np, "__version__")')
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
