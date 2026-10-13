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


def test_control_flow_not_swallowed_by_except_exception():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})
    # return inside try/except Exception
    code = """
def f():
    try:
        return 1
    except Exception:
        return 2
result = f()
"""
    executor(code)
    assert executor.state["result"] == 1

    # break inside try/except Exception
    code = """
result = 0
for i in [1, 2, 3]:
    try:
        break
    except Exception:
        result = 99
result = 1
"""
    executor(code)
    assert executor.state["result"] == 1

    # continue inside try/except Exception
    code = """
result = 0
for i in [1, 2, 3]:
    try:
        continue
    except Exception:
        result += 10
    result += 100
"""
    executor(code)
    assert executor.state["result"] == 0


def test_with_suppressing_manager_does_not_swallow_return():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    class Suppress:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            return True

    executor.send_variables({"Suppress": Suppress})
    code = """
def f():
    with Suppress():
        return 42
    return 99
result = f()
"""
    executor(code)
    assert executor.state["result"] == 42


def test_with_suppressing_manager_suppresses_exception():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    class Suppress:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            return True

    executor.send_variables({"Suppress": Suppress})
    code = """
with Suppress():
    raise ValueError("boom")
result = "survived"
"""
    executor(code)
    assert executor.state["result"] == "survived"


def test_final_answer_inside_with_calls_exit_and_returns():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    exited = []

    class Manager:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            exited.append(True)

    executor.send_tools({"final_answer": lambda x: x})
    executor.send_variables({"Manager": Manager})
    out = executor("with Manager():\n    final_answer('done')")
    assert out.is_final_answer is True
    assert out.output == "done"
    assert exited == [True]



def test_with_second_enter_failing_exits_first_manager():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    events = []

    class First:
        def __enter__(self):
            events.append("enter1")
            return self
        def __exit__(self, *args):
            events.append("exit1")

    class Second:
        def __enter__(self):
            events.append("enter2")
            raise RuntimeError("fail enter2")
        def __exit__(self, *args):
            events.append("exit2")

    executor.send_tools({})
    executor.send_variables({"First": First, "Second": Second})
    with pytest.raises(InterpreterError) as exc_info:
        executor("with First(), Second():\n    pass")
    assert "fail enter2" in str(exc_info.value)
    assert events == ["enter1", "enter2", "exit1"]


def test_with_target_unpacking():
    executor = LocalPythonExecutor(additional_authorized_imports=[])

    class PairManager:
        def __enter__(self):
            return (10, 20)
        def __exit__(self, *args):
            pass

    executor.send_tools({})
    executor.send_variables({"PairManager": PairManager})
    executor("with PairManager() as (a, b):\n    result = a + b")
    assert executor.state["result"] == 30


def test_timeout_raises_execution_timeout_error_and_not_caught_by_user_except():
    from plugin.contrib.smolagents.local_python_executor import ExecutionTimeoutError

    executor = LocalPythonExecutor(additional_authorized_imports=["time"])
    executor.send_tools({})
    executor.timeout_seconds = 1
    code = """
import time
try:
    time.sleep(2)
except Exception:
    pass
"""
    with pytest.raises(ExecutionTimeoutError):
        executor(code)


def test_subscript_and_delete_exceptions_propagate():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})
    code = """
d = {"a": 1}
try:
    _ = d["x"]
    result1 = "failed"
except KeyError:
    result1 = "caught_key_error"

try:
    del d["x"]
    result2 = "failed"
except KeyError:
    result2 = "caught_del_key_error"

try:
    _ = [][0]
    result3 = "failed"
except IndexError:
    result3 = "caught_index_error"
"""
    executor(code)
    assert executor.state["result1"] == "caught_key_error"
    assert executor.state["result2"] == "caught_del_key_error"
    assert executor.state["result3"] == "caught_index_error"


def test_subscript_evaluates_value_before_index():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    order = []

    def get_dict():
        order.append("value")
        return {"k": 42}

    def get_key():
        order.append("index")
        return "k"

    executor.send_tools({})
    executor.send_variables({"get_dict": get_dict, "get_key": get_key})
    executor("result = get_dict()[get_key()]")
    assert executor.state["result"] == 42
    assert order == ["value", "index"]


