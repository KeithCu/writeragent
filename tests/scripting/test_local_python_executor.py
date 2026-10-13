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


def test_setattr_forbids_class_dunder():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})
    code = """
class A:
    pass
class B:
    pass
a = A()
setattr(a, "__class__", B)
"""
    with pytest.raises(InterpreterError) as exc_info:
        executor(code)
    assert "Forbidden access to dunder attribute" in str(exc_info.value)
    assert "__class__" in str(exc_info.value)


def test_nodunder_getattr_sentinel_two_and_three_args():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})
    # Two-arg getattr on missing attribute raises AttributeError
    with pytest.raises(InterpreterError) as exc_info:
        executor("class A: pass\ngetattr(A(), 'missing')")
    assert "AttributeError" in str(exc_info.value)

    # Three-arg getattr returns default
    executor("class A: pass\nresult = getattr(A(), 'missing', 99)")
    assert executor.state["result"] == 99


def test_class_body_mutation_guard_rejects_module_attribute_assignment():
    import math

    executor = LocalPythonExecutor(additional_authorized_imports=["math"])
    executor.send_tools({})
    code = """
import math
class B:
    t = math
    t.marker = 1
"""
    with pytest.raises(InterpreterError) as exc_info:
        executor(code)
    assert "Cannot assign attributes on a module or a type" in str(exc_info.value)
    assert not hasattr(math, "marker")


def test_evaluate_call_attribute_dunder_rejected_at_runtime():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})
    with pytest.raises(InterpreterError) as exc_info:
        executor("class A: pass\nA().__class__()")
    assert "Forbidden" in str(exc_info.value)


def test_print_hijack_method_and_user_def():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    # obj.print calls method
    code = """
class A:
    def print(self, x):
        return f"printed:{x}"
result = A().print(123)
"""
    executor(code)
    assert executor.state["result"] == "printed:123"

    # User def print is called
    code = """
def print(*args, sep=" "):
    return f"user:{sep.join(map(str, args))}"
result = print("a", "b", sep="-")
"""
    executor(code)
    assert executor.state["result"] == "user:a-b"


def test_injected_print_sep_end_file_unknown():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    executor('print("a", "b", sep="-")')
    assert str(executor.state["_print_outputs"]).endswith("a-b\n")

    executor('print(1, 2, end="!")')
    assert str(executor.state["_print_outputs"]).endswith("1 2!")

    with pytest.raises(InterpreterError) as exc_info:
        executor('print("foo", file="something")')
    assert "file=" in str(exc_info.value)

    with pytest.raises(InterpreterError) as exc_info:
        executor('print("foo", unknown=1)')
    assert "unexpected keyword argument" in str(exc_info.value)


def test_dotted_import_binding_xml():
    executor = LocalPythonExecutor(additional_authorized_imports=["xml", "xml.etree.ElementTree"])
    executor.send_tools({})

    # import xml.etree.ElementTree binds "xml"
    executor("import xml.etree.ElementTree\nel = xml.etree.ElementTree.Element('tag')\nresult = el.tag")
    assert executor.state["result"] == "tag"
    assert "xml.etree.ElementTree" not in executor.state
    assert "xml" in executor.state

    # import xml.etree.ElementTree as ET binds "ET" to leaf
    executor("import xml.etree.ElementTree as ET\nel2 = ET.Element('child')\nresult2 = el2.tag")
    assert executor.state["result2"] == "child"
    assert "ET" in executor.state


def test_import_tool_guards():
    executor = LocalPythonExecutor(additional_authorized_imports=["math"])
    executor.send_tools({})

    # from math import sqrt works (overridable)
    executor("from math import sqrt\nresult = sqrt(4)")
    assert executor.state["result"] == 2.0

    # from math import sin as len is rejected
    with pytest.raises(InterpreterError) as exc_info:
        executor("from math import sin as len")
    assert "Cannot assign to name 'len': doing this would erase the existing tool!" in str(exc_info.value)

    # import math as print is rejected
    with pytest.raises(InterpreterError) as exc_info:
        executor("import math as print")
    assert "Cannot assign to name 'print': doing this would erase the existing tool!" in str(exc_info.value)

    # from math import * does not replace pow
    executor("from math import *\nresult_pow = pow(2, 3, 5)")
    assert executor.state["result_pow"] == 3


def test_relative_import_rejected():
    from plugin.contrib.smolagents.local_python_executor import evaluate_import

    # Calling evaluate_import on relative node raises InterpreterError, not AttributeError
    node = ast.parse("from . import x").body[0]
    with pytest.raises(InterpreterError) as exc_info:
        evaluate_import(node, {}, [])
    assert "Relative imports are not allowed." in str(exc_info.value)


def test_function_argument_binding_and_errors():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    # Positional-only
    code = """
def f(a, /, b):
    return a + b
result_pos = f(1, 2)
"""
    executor(code)
    assert executor.state["result_pos"] == 3

    with pytest.raises(InterpreterError) as exc_info:
        executor("f(a=1, b=2)")
    assert "positional-only" in str(exc_info.value)

    # Keyword-only with default
    code = """
def g(a, *, b=1):
    return a + b
r1 = g(10)
r2 = g(10, b=5)
"""
    executor(code)
    assert executor.state["r1"] == 11
    assert executor.state["r2"] == 15

    with pytest.raises(InterpreterError) as exc_info:
        executor("g(10, 20)")
    assert "positional argument" in str(exc_info.value)

    # Missing required argument raises TypeError
    with pytest.raises(InterpreterError) as exc_info:
        executor("def h(x): return x\nh()")
    assert "missing a required argument" in str(exc_info.value)

    # Extra positional argument
    with pytest.raises(InterpreterError) as exc_info:
        executor("def h(x): return x\nh(1, 2)")
    assert "too many positional arguments" in str(exc_info.value)

    # Unexpected keyword argument
    with pytest.raises(InterpreterError) as exc_info:
        executor("def h(x): return x\nh(x=1, y=2)")
    assert "unexpected keyword argument" in str(exc_info.value)

    # *args and **kwargs contents
    code = """
def var(a, *args, **kwargs):
    return (a, args, kwargs)
result_var = var(1, 2, 3, b=4)
"""
    executor(code)
    assert executor.state["result_var"] == (1, (2, 3), {"b": 4})


def test_default_versus_global_and_definition_time_eval():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    # Global does not override default
    code = """
x = 10
def f(x=1):
    return x
result = f()
"""
    executor(code)
    assert executor.state["result"] == 1

    # Definition-time default evaluation and mutable default sharing
    code = """
def f(x=[]):
    x.append(1)
    return x
a = f()
b = f()
result = (a, b)
"""
    executor(code)
    a, b = executor.state["result"]
    assert a == [1, 1]
    assert b is a


def test_lambda_defaults_and_rest():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    code = """
f = lambda x=1: x
r1 = f()
r2 = f(42)
g = lambda *rest: rest
r3 = g(1, 2)
"""
    executor(code)
    assert executor.state["r1"] == 1
    assert executor.state["r2"] == 42
    assert executor.state["r3"] == (1, 2)


def test_nested_def_and_class_methods_do_not_leak():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    # Nested def does not leak to state or custom_tools
    code = """
def outer():
    def inner():
        return 1
    return inner()
res = outer()
"""
    executor(code)
    assert executor.state["res"] == 1
    assert "inner" not in executor.state
    assert "inner" not in executor.custom_tools

    # Calling inner outside raises undefined error
    with pytest.raises(InterpreterError) as exc_info:
        executor("inner()")
    assert "inner" in str(exc_info.value)

    # Class methods do not leak into executor.state or executor.custom_tools
    code = """
class A:
    def foo(self):
        return 4
a_res = A().foo()
"""
    executor(code)
    assert executor.state["a_res"] == 4
    assert "foo" not in executor.state
    assert "foo" not in executor.custom_tools


def test_decorators_raise_interpreter_error():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    def dummy_dec(fn):
        return fn

    executor.send_variables({"dec": dummy_dec})

    # Function decorator
    with pytest.raises(InterpreterError) as exc_info:
        executor("@dec\ndef f(): pass")
    assert "Decorators are not supported in the sandbox" in str(exc_info.value)

    # Class decorator
    with pytest.raises(InterpreterError) as exc_info:
        executor("@dec\nclass C: pass")
    assert "Decorators are not supported in the sandbox" in str(exc_info.value)


def test_slice6_class_scope_metaclass_super():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    # 1. Class-body assignment and scope: y = x + 1 works
    code1 = """
class A:
    x = 10
    y = x + 1
res_x = A.x
res_y = A.y
"""
    executor(code1)
    assert executor.state["res_x"] == 10
    assert executor.state["res_y"] == 11

    # 2. A method that reads a class-body local name fails (name error / undefined)
    code2 = """
class B:
    x = 10
    def get_x(self):
        return x
b = B()
b.get_x()
"""
    with pytest.raises(InterpreterError) as exc_info:
        executor(code2)
    assert "x" in str(exc_info.value)

    # 3. class A(metaclass=M) has type M
    code3 = """
class Meta(type):
    pass
class MClass(metaclass=Meta):
    pass
t = type(MClass)
"""
    executor(code3)
    assert executor.state["t"] is executor.state["Meta"]

    # 4. class A(x=1) raises
    code4 = """
class BadClass(x=1):
    pass
"""
    with pytest.raises(InterpreterError) as exc_info:
        executor(code4)
    assert "Unsupported class keyword argument 'x'" in str(exc_info.value)

    # 5. Three classes each calling super().greet() do not recurse and return base string
    code5 = """
class Base:
    def greet(self):
        return "A"

class Mid(Base):
    def greet(self):
        return super().greet() + "B"

class Sub(Mid):
    def greet(self):
        return super().greet() + "C"

sub = Sub()
greeting = sub.greet()
"""
    executor(code5)
    assert executor.state["greeting"] == "ABC"

    # 6. obj.super() is not redirected
    code6 = """
class CustomSuper:
    def super(self):
        return "custom super"

cs = CustomSuper()
cs_res = cs.super()
"""
    executor(code6)
    assert executor.state["cs_res"] == "custom super"

    # 7. Zero-arg super() outside a method raises error
    with pytest.raises(InterpreterError) as exc_info:
        executor("super()")
    assert "super() needs at least one argument" in str(exc_info.value)

    # 8. Explicit super(Cls, obj) works
    code8 = """
class X:
    def f(self):
        return "X"
class Y(X):
    def f(self):
        return super(Y, self).f() + "Y"
y_res = Y().f()
"""
    executor(code8)
    assert executor.state["y_res"] == "XY"






