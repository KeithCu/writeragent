# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import ast
from typing import Any

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


@pytest.fixture(autouse=True)
def _reset_abandoned_timeout() -> Any:
    # Tip: reset abandoned_timeout before and after each test so thread fallback timeouts do not leak.
    import plugin.contrib.smolagents.local_python_executor as lpe

    lpe.abandoned_timeout = False
    try:
        yield
    finally:
        lpe.abandoned_timeout = False


def test_nested_generatorexp_via_evaluate_generatorexp():
    """Inner loop variable must bind when multiple generators are nested."""
    state = {"data": [[1, 2, 3], [4, 5, 6]]}
    tree = ast.parse("(v for row in data for v in row)", mode="eval")
    assert isinstance(tree.body, ast.GeneratorExp)
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
            raise ValueError("fail enter2")

        def __exit__(self, *args):
            events.append("exit2")

    executor.send_tools({})
    executor.send_variables({"First": First, "Second": Second})
    with pytest.raises(InterpreterError) as exc_info:
        executor("with First(), Second():\n    pass")
    assert "fail enter2" in str(exc_info.value)
    assert events == ["enter1", "enter2", "exit1"]

    # First.__exit__ returns true and suppresses; body does not run, exit2 absent
    events.clear()
    recorded_exc = []

    class SuppressingFirst:
        def __enter__(self):
            events.append("enter1")
            return self

        def __exit__(self, exc_type, exc_val, tb):
            events.append("exit1")
            recorded_exc.append((exc_type, exc_val))
            return True

    executor.send_variables({"SuppressingFirst": SuppressingFirst, "Second": Second, "body_assigned": False})
    executor("with SuppressingFirst(), Second():\n    body_assigned = True")
    assert events == ["enter1", "enter2", "exit1"]
    assert executor.state["body_assigned"] is False
    assert len(recorded_exc) == 1
    assert recorded_exc[0][0] is ValueError
    assert "fail enter2" in str(recorded_exc[0][1])

    # First.__exit__ raises RuntimeError; reported error is RuntimeError, not enter ValueError
    class RaisingFirst:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, tb):
            raise RuntimeError("exit error")

    executor.send_variables({"RaisingFirst": RaisingFirst, "Second": Second})
    with pytest.raises(InterpreterError) as exc_info:
        executor("with RaisingFirst(), Second():\n    pass")
    assert "exit error" in str(exc_info.value)
    assert "fail enter2" not in str(exc_info.value)


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


def test_evaluate_call_property_getter_evaluates_once():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    class Counter:
        def __init__(self):
            self.count = 0

        @property
        def method(self):
            self.count += 1
            return lambda: self.count

    executor.send_variables({"Counter": Counter})
    code = """
c = Counter()
res = c.method()
n = c.count
"""
    executor(code)
    assert executor.state["n"] == 1
    assert executor.state["res"] == 1


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
    assert executor.logs.endswith("a-b\n")

    executor('print(1, 2, end="!")')
    assert executor.logs.endswith("1 2!")

    with pytest.raises(InterpreterError) as exc_info:
        executor('print("foo", file="something")')
    assert "file=" in str(exc_info.value)

    with pytest.raises(InterpreterError) as exc_info:
        executor('print("foo", unknown=1)')
    assert "unexpected keyword argument" in str(exc_info.value)


def test_print_follows_current_execution():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    executor("""
def f():
    print("from f")
p = print
""")

    executor("""
f()
p("from p")
""")
    assert "from f" in executor.logs
    assert "from p" in executor.logs

    exec_a = LocalPythonExecutor(additional_authorized_imports=[])
    exec_a.send_tools({})
    exec_a("""
def fn_a():
    print("hello from fn_a")
""")
    fn_obj = exec_a.state["fn_a"]

    exec_b = LocalPythonExecutor(additional_authorized_imports=[])
    exec_b.send_tools({})
    exec_b.send_variables({"fn_b": fn_obj})
    exec_b("fn_b()")
    assert "hello from fn_a" in exec_b.logs
    assert exec_a.logs == ""


def test_dotted_import_binding_xml():
    executor = LocalPythonExecutor(additional_authorized_imports=["xml", "xml.*"])
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
    assert isinstance(node, (ast.Import, ast.ImportFrom))
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


def test_slice7_syntax_and_unpacking():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    # 1. for...else: loop finishes naturally -> else runs
    code_for_else = """
res_for = 0
for i in [1, 2, 3]:
    res_for += i
else:
    for_else_ran = True
"""
    executor(code_for_else)
    assert executor.state["res_for"] == 6
    assert executor.state["for_else_ran"] is True

    # 2. for...else: break prevents else from running
    code_for_break = """
for_break_else_ran = False
for i in [1, 2, 3]:
    if i == 2:
        break
else:
    for_break_else_ran = True
"""
    executor(code_for_break)
    assert executor.state["for_break_else_ran"] is False

    # 3. while...else: loop finishes naturally -> else runs
    code_while_else = """
n = 3
while n > 0:
    n -= 1
else:
    while_else_ran = True
"""
    executor(code_while_else)
    assert executor.state["while_else_ran"] is True

    # 4. while...else: break prevents else from running
    code_while_break = """
while_break_else_ran = False
m = 3
while m > 0:
    break
else:
    while_break_else_ran = True
"""
    executor(code_while_break)
    assert executor.state["while_break_else_ran"] is False

    # 5. Bare raise inside except re-raises matched exception
    code_bare_raise = """
caught = None
try:
    try:
        raise ValueError("inner err")
    except ValueError:
        raise
except ValueError as e:
    caught = str(e)
"""
    executor(code_bare_raise)
    assert executor.state["caught"] == "inner err"

    # 6. Bare raise outside except errors
    with pytest.raises(InterpreterError) as exc_info:
        executor("raise")
    assert "Re-raise is not supported without an active exception" in str(exc_info.value)

    # 7. [a, b] = [1, 2] list assignment
    executor("[a, b] = [1, 2]")
    assert executor.state["a"] == 1
    assert executor.state["b"] == 2

    # 8. a, *b = (1, 2, 3) and a, *b, c = (1, 2, 3, 4)
    executor("x1, *x2 = (1, 2, 3)")
    assert executor.state["x1"] == 1
    assert executor.state["x2"] == [2, 3]

    executor("y1, *y2, y3 = (1, 2, 3, 4)")
    assert executor.state["y1"] == 1
    assert executor.state["y2"] == [2, 3]
    assert executor.state["y3"] == 4

    # 9. a, b = "xy" string and bytes unpack
    executor("s1, s2 = 'xy'")
    assert executor.state["s1"] == "x"
    assert executor.state["s2"] == "y"

    executor("b1, b2 = b'12'")
    assert executor.state["b1"] == ord(b"1")
    assert executor.state["b2"] == ord(b"2")

    # 10. Starred literals in list, tuple, set
    code_starred_lits = """
base_list = [2, 3]
new_list = [1, *base_list, 4]
new_tuple = (1, *base_list, 4)
new_set = {1, *base_list, 4}
"""
    executor(code_starred_lits)
    assert executor.state["new_list"] == [1, 2, 3, 4]
    assert executor.state["new_tuple"] == (1, 2, 3, 4)
    assert executor.state["new_set"] == {1, 2, 3, 4}

    # 11. Dict unpack: {**d, "b": 2}
    code_dict_unpack = """
d1 = {"a": 1, "b": 0}
d2 = {**d1, "b": 2, "c": 3}
"""
    executor(code_dict_unpack)
    assert executor.state["d2"] == {"a": 1, "b": 2, "c": 3}

    # 12. f-string conversions: f"{x!r}", f"{x!s}", f"{x!a}"
    code_fstring = """
val = "hello"
r_str = f"{val!r}"
s_str = f"{val!s}"
val_nonascii = "héllo"
a_str = f"{val_nonascii!a}"
"""
    executor(code_fstring)
    assert executor.state["r_str"] == "'hello'"
    assert executor.state["s_str"] == "hello"
    assert executor.state["a_str"] == "'h\\xe9llo'"

    # 13. x += 1 on missing x raises undefined name error
    with pytest.raises(InterpreterError) as exc_info:
        executor("missing_var += 1")
    assert "missing_var" in str(exc_info.value)
    assert "not defined" in str(exc_info.value)

    # 14. a[k()] += 1 calls k once
    code_subscript_once = """
call_log = []
def k():
    call_log.append(1)
    return "key"
arr = {"key": 10}
arr[k()] += 1
"""
    executor(code_subscript_once)
    assert len(executor.state["call_log"]) == 1
    assert executor.state["arr"]["key"] == 11


def test_slice8_operator_tables():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    # 1. Binary operators
    code_binop = """
add_res = 10 + 2
sub_res = 10 - 2
mul_res = 10 * 2
div_res = 10 / 2
floordiv_res = 10 // 3
mod_res = 10 % 3
pow_res = 2 ** 3
lshift_res = 1 << 2
rshift_res = 8 >> 2
and_res = 0b1100 & 0b1010
or_res = 0b1100 | 0b1010
xor_res = 0b1100 ^ 0b1010
"""
    executor(code_binop)
    assert executor.state["add_res"] == 12
    assert executor.state["sub_res"] == 8
    assert executor.state["mul_res"] == 20
    assert executor.state["div_res"] == 5.0
    assert executor.state["floordiv_res"] == 3
    assert executor.state["mod_res"] == 1
    assert executor.state["pow_res"] == 8
    assert executor.state["lshift_res"] == 4
    assert executor.state["rshift_res"] == 2
    assert executor.state["and_res"] == 0b1000
    assert executor.state["or_res"] == 0b1110
    assert executor.state["xor_res"] == 0b0110

    # 2. AugAssign operators
    code_aug = """
v_add = 10; v_add += 2
v_sub = 10; v_sub -= 2
v_mul = 10; v_mul *= 2
v_div = 10; v_div /= 2
v_floordiv = 10; v_floordiv //= 3
v_mod = 10; v_mod %= 3
v_pow = 2; v_pow **= 3
v_lshift = 1; v_lshift <<= 2
v_rshift = 8; v_rshift >>= 2
v_and = 0b1100; v_and &= 0b1010
v_or = 0b1100; v_or |= 0b1010
v_xor = 0b1100; v_xor ^= 0b1010
"""
    executor(code_aug)
    assert executor.state["v_add"] == 12
    assert executor.state["v_sub"] == 8
    assert executor.state["v_mul"] == 20
    assert executor.state["v_div"] == 5.0
    assert executor.state["v_floordiv"] == 3
    assert executor.state["v_mod"] == 1
    assert executor.state["v_pow"] == 8
    assert executor.state["v_lshift"] == 4
    assert executor.state["v_rshift"] == 2
    assert executor.state["v_and"] == 0b1000
    assert executor.state["v_or"] == 0b1110
    assert executor.state["v_xor"] == 0b0110

    # 3. Comparison operators (==, !=, <, <=, >, >=, is, is not, in, not in)
    code_cmp = """
c_eq = (1 == 1)
c_ne = (1 != 2)
c_lt = (1 < 2)
c_le = (2 <= 2)
c_gt = (3 > 2)
c_ge = (2 >= 2)
a_obj = [1]
c_is = (a_obj is a_obj)
c_isnot = (a_obj is not [1])
c_in = (1 in [1, 2])
c_notin = (3 not in [1, 2])
"""
    executor(code_cmp)
    assert executor.state["c_eq"] is True
    assert executor.state["c_ne"] is True
    assert executor.state["c_lt"] is True
    assert executor.state["c_le"] is True
    assert executor.state["c_gt"] is True
    assert executor.state["c_ge"] is True
    assert executor.state["c_is"] is True
    assert executor.state["c_isnot"] is True
    assert executor.state["c_in"] is True
    assert executor.state["c_notin"] is True

    # 4. Matmul @ and @= with pure Python object
    code_matmul = """
class Matrix:
    def __init__(self, val):
        self.val = val
    def __matmul__(self, other):
        return Matrix(self.val * other.val)
    def __imatmul__(self, other):
        self.val = self.val * other.val + 1
        return self

m1 = Matrix(3)
m2 = Matrix(4)
m3 = m1 @ m2
m_val = m3.val
m1 @= m2
m_aug_val = m1.val
"""
    executor(code_matmul)
    assert executor.state["m_val"] == 12
    assert executor.state["m_aug_val"] == 13

    # 5. @= on missing name raises undefined name error
    with pytest.raises(InterpreterError) as exc_info:
        executor("missing_mat @= m2")
    assert "missing_mat" in str(exc_info.value)
    assert "not defined" in str(exc_info.value)

    # 6. list += non-list raises TypeError (Python operator.iadd behavior)
    code_type_err = """
caught_type_error = False
try:
    lst = [1, 2]
    lst += 3
except TypeError:
    caught_type_error = True
"""
    executor(code_type_err)
    assert executor.state["caught_type_error"] is True

    with pytest.raises(InterpreterError) as exc_info:
        executor("lst = [1, 2]; lst += 3")
    assert "TypeError" in str(exc_info.value)
    assert "is not iterable" in str(exc_info.value)


def test_slice9_timeout_validation_and_unwinding():
    import time
    from concurrent.futures import ThreadPoolExecutor as TPE
    from plugin.contrib.smolagents.local_python_executor import (
        ExecutionTimeoutError,
        evaluate_python_code,
        timeout,
    )

    # 1. Validation guard
    with pytest.raises(TypeError) as exc_info:
        timeout(0.5)
    assert "whole number of seconds >= 1" in str(exc_info.value)

    with pytest.raises(TypeError):
        timeout(True)

    with pytest.raises(TypeError):
        timeout(0)

    with pytest.raises(TypeError):
        timeout(-2)

    # 2. Thread pool fallback does not block on shutdown(wait=False)
    # Run from a worker thread where signal.signal raises ValueError
    def worker():
        @timeout(1)
        def slow():
            time.sleep(2.5)
            return "done"

        t0 = time.monotonic()
        with pytest.raises(ExecutionTimeoutError):
            slow()
        return time.monotonic() - t0

    import plugin.contrib.smolagents.local_python_executor as lpe
    try:
        with TPE(max_workers=1) as pool:
            future = pool.submit(worker)
            elapsed = future.result()
        assert elapsed < 2.0  # Returned well under 2.5s!
    finally:
        lpe.abandoned_timeout = False

    # 3. Outer timeout(3) with inner timeout(1) finishing immediately, outer sleeps
    @timeout(3)
    def outer_run():
        evaluate_python_code("x = 1", timeout_seconds=1)
        time.sleep(4.0)

    t0 = time.monotonic()
    with pytest.raises(ExecutionTimeoutError):
        outer_run()
    elapsed = time.monotonic() - t0
    assert 2.5 <= elapsed <= 3.8

    # 4. Inverse: outer timeout(2) with inner timeout(5) finishing immediately, outer sleeps
    @timeout(2)
    def outer_inverse():
        evaluate_python_code("y = 1", timeout_seconds=5)
        time.sleep(3.0)

    t0 = time.monotonic()
    with pytest.raises(ExecutionTimeoutError):
        outer_inverse()
    elapsed = time.monotonic() - t0
    assert 1.5 <= elapsed <= 2.8

    # 5. Non-main thread timeout stops pure-Python loop via _Ops deadline
    from plugin.contrib.smolagents.local_python_executor import LocalPythonExecutor

    def loop_worker():
        executor = LocalPythonExecutor(additional_authorized_imports=[], timeout_seconds=1)
        executor.send_variables({"counter": 0})
        code = "while True:\n    counter += 1"
        with pytest.raises(ExecutionTimeoutError):
            executor(code)
        val = executor.state["counter"]
        time.sleep(0.15)
        return val, executor.state["counter"]

    with TPE(max_workers=1) as pool:
        future = pool.submit(loop_worker)
        val_at_timeout, val_after_wait = future.result()
    assert val_at_timeout > 0
    assert val_at_timeout == val_after_wait


def test_slice10_import_authorized_drift():
    from plugin.contrib.smolagents.local_python_executor import check_import_authorized
    from plugin.scripting.sandbox import import_authorized

    # Empty string returns False
    assert check_import_authorized("", ["matplotlib"]) is False

    for check_fn in (import_authorized, check_import_authorized):
        # 1. writeragent allowed when list contains only plugin.scripting.writeragent_api
        assert check_fn("writeragent", ["plugin.scripting.writeragent_api"]) is True
        assert check_fn("writeragent", ["other_pkg"]) is False

        # 2. writeragent.vision allowed when list contains plugin.vision or explicit alias
        assert check_fn("writeragent.vision", ["plugin.vision"]) is True
        assert check_fn("writeragent.vision", ["writeragent.vision"]) is True
        assert check_fn("writeragent.vision", ["other_pkg"]) is False

        # 3. matplotlib not allowed when list contains only matplotlib.pyplot
        assert check_fn("matplotlib", ["matplotlib.pyplot"]) is False

        # 4. matplotlib.pyplot not allowed when list contains only matplotlib (no star)
        assert check_fn("matplotlib.pyplot", ["matplotlib"]) is False
        # With star wildcard it is allowed
        assert check_fn("matplotlib.pyplot", ["matplotlib.*"]) is True
        assert check_fn("os", ["*"]) is False
        assert check_fn("plugin.framework.config", ["*"]) is False
        assert check_fn("math", ["*", "math"]) is True


def test_slice10_dotted_import_with_allowlist():
    executor = LocalPythonExecutor(additional_authorized_imports=["xml", "xml.*"])
    executor.send_tools({})
    executor("import xml.etree.ElementTree\nx = hasattr(xml, 'etree')")
    assert executor.state["x"] is True


def test_b3_parent_module_rejected_when_only_child_listed():
    """B3: with an allowlist naming only plugin.scripting.viz, importing plugin

    and plugin.scripting must be REJECTED by the runtime import checker
    (check_import_authorized -> import_authorized), while plugin.scripting.viz is ACCEPTED.
    """
    from plugin.contrib.smolagents.local_python_executor import check_import_authorized
    from plugin.scripting.sandbox import import_authorized

    allowlist = ["plugin.scripting.viz"]

    # 1. Direct checks on both import_authorized and check_import_authorized
    for check_fn in (import_authorized, check_import_authorized):
        assert check_fn("plugin", allowlist) is False
        assert check_fn("plugin.scripting", allowlist) is False
        assert check_fn("plugin.scripting.viz", allowlist) is True

    # 2. Executing imports through LocalPythonExecutor
    executor = LocalPythonExecutor(additional_authorized_imports=allowlist)
    executor.send_tools({})

    with pytest.raises(InterpreterError) as exc_info:
        executor("import plugin")
    assert "not allowed" in str(exc_info.value).lower()

    with pytest.raises(InterpreterError) as exc_info:
        executor("import plugin.scripting")
    assert "not allowed" in str(exc_info.value).lower()

    # Importing the allowed child module succeeds
    executor("import plugin.scripting.viz as viz\nloaded = str(viz)")
    assert "plugin.scripting.viz" in executor.state["loaded"]


def test_future_annotations_and_collections_abc_execute():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})
    code_future = (
        "from __future__ import annotations\n"
        "def f(x: int) -> int:\n"
        "    return x\n"
        "result = f(1)\n"
    )
    executor(code_future)
    assert executor.state["result"] == 1

    executor("from collections.abc import Mapping\nresult = issubclass(dict, Mapping)\n")
    assert executor.state["result"] is True

    executor("import collections.abc as cabc\nresult = issubclass(list, cabc.Sequence)\n")
    assert executor.state["result"] is True

    with pytest.raises(InterpreterError) as exc_info:
        executor("import os")
    assert str(exc_info.value) == "Import of os is not allowed."


def test_import_from_forbidden_dunder_attributes():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    for name in ("__loader__", "__spec__", "__dict__", "__builtins__", "__getattribute__"):
        with pytest.raises(InterpreterError) as exc_info:
            executor(f"from re import {name}")
        assert "Forbidden access to dunder attribute" in str(exc_info.value)

    executor("from re import __version__\nres_ver = __version__")
    assert isinstance(executor.state["res_ver"], str)

    executor("from __future__ import annotations\nres_ann = 1")
    assert executor.state["res_ann"] == 1

    executor("from collections.abc import Mapping\nres_map = issubclass(dict, Mapping)")
    assert executor.state["res_map"] is True


def test_operator_loader_and_spec_checks():
    executor = LocalPythonExecutor(additional_authorized_imports=["operator", "json"])
    executor.send_tools({})

    with pytest.raises(InterpreterError) as exc_info:
        executor("import operator, re\nf = operator.attrgetter('__loader__')\nres = f(re)")
    assert "Forbidden access to dunder attribute" in str(exc_info.value)

    with pytest.raises(InterpreterError) as exc_info:
        executor("import operator, re\nf = operator.methodcaller('__getattribute__', '__loader__')\nres = f(re)")
    assert "Forbidden access to dunder attribute" in str(exc_info.value)

    executor('import json\nres1 = json.loads(\'{"__spec__": 1}\')\nres2 = json.loads(\'{"__spec__": 1, "__name__": "os"}\')')
    assert executor.state["res1"] == {"__spec__": 1}
    assert executor.state["res2"] == {"__spec__": 1, "__name__": "os"}

    with pytest.raises(InterpreterError) as exc_info:
        executor("import random\nx = random._os")
    assert "Forbidden access to module" in str(exc_info.value)

    with pytest.raises(InterpreterError) as exc_info:
        executor("import operator, random\noperator.attrgetter('_os.listdir')(random)")
    assert "Forbidden access to module" in str(exc_info.value)

    with pytest.raises(InterpreterError) as exc_info:
        executor("import operator, random\noperator.attrgetter('_os.remove')(random)")
    assert "Forbidden access to module" in str(exc_info.value)

    with pytest.raises(InterpreterError) as exc_info:
        executor("import operator, collections\noperator.attrgetter('_collections_abc.sys._getframe')(collections)")
    assert "Forbidden access to module" in str(exc_info.value)

    import os
    executor.send_variables({"f": os.listdir})
    with pytest.raises(InterpreterError) as exc_info:
        executor('f(".")')
    assert "Forbidden" in str(exc_info.value)

    positive_code = """
class Inner:
    def __init__(self, val):
        self.b = val

class Outer:
    def __init__(self):
        self.a = Inner(10)
        self.b = 20

import operator
obj = Outer()
res_nested = operator.attrgetter('a.b')(obj)
res_tuple = operator.attrgetter('a', 'b')(obj)
"""
    executor(positive_code)
    assert executor.state["res_nested"] == 10
    assert executor.state["res_tuple"] == (executor.state["obj"].a, 20)


def test_string_based_attribute_dispatch_dunder_rejection():
    executor = LocalPythonExecutor(additional_authorized_imports=["operator", "string", "itertools", "functools"])
    executor.send_tools({})

    with pytest.raises(InterpreterError) as exc:
        executor("import operator\nbase = operator.attrgetter('__bases__')(int)[0]")
    assert "Forbidden access to dunder attribute" in str(exc.value)

    executor.send_variables({"base": int.__bases__[0]})
    with pytest.raises(InterpreterError) as exc:
        executor("import operator\noperator.methodcaller('__subclasses__')(base)")
    assert "Forbidden access to dunder attribute" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("import operator, itertools\nlist(itertools.starmap(operator.attrgetter, [('__subclasses__',)]))")
    assert "Forbidden access to dunder attribute" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("import operator\nlist(map(operator.methodcaller, ['__subclasses__']))")
    assert "Forbidden access to dunder attribute" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("import operator, functools\nfunctools.partial(operator.methodcaller, '__subclasses__')()")
    assert "Forbidden access to dunder attribute" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("from operator import attrgetter as a, methodcaller as m\na('__subclasses__')")
    assert "Forbidden access to dunder attribute" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("from operator import attrgetter as a, methodcaller as m\nm('__subclasses__')")
    assert "Forbidden access to dunder attribute" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("import string, itertools\nf = string.Formatter()\nlist(itertools.starmap(f.get_field, [('0.__subclasses__', [int], {})]))")
    assert "Forbidden access to dunder attribute" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("import string\nstring.Formatter().format('{0.__subclasses__}', int)")
    assert "Forbidden access to dunder attribute" in str(exc.value)

    code_allowed = """
import operator, string, re

res_add = operator.add(2, 3)
res_item = operator.itemgetter(1)([10, 20, 30])
res_upper = operator.methodcaller("upper")("hello")
res_str = operator.methodcaller("__str__")(99)
res_real = operator.attrgetter("real")(complex(3, 4))
res_ver = operator.attrgetter("__version__")(re)

class Item:
    def __init__(self, name):
        self.name = name

item = Item("widget")
res_fmt = string.Formatter().format("{0} {1.name}", "item:", item)
"""
    executor(code_allowed)
    assert executor.state["res_add"] == 5
    assert executor.state["res_item"] == 20
    assert executor.state["res_upper"] == "HELLO"
    assert executor.state["res_str"] == "99"
    assert executor.state["res_real"] == 3.0
    assert isinstance(executor.state["res_ver"], str)
    assert executor.state["res_fmt"] == "item: widget"


def test_allowed_dunder_methods_attribute_access():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    code_init = """
class Base:
    def __init__(self):
        self.val = 10

class Derived(Base):
    def __init__(self):
        super().__init__()

d = Derived()
res_init = d.val
"""
    executor(code_init)
    assert executor.state["res_init"] == 10

    code_str_repr = """
class A:
    def __str__(self):
        return "custom_str"
    def __repr__(self):
        return "custom_repr"

a = A()
s = a.__str__()
r = a.__repr__()
"""
    executor(code_str_repr)
    assert executor.state["s"] == "custom_str"
    assert executor.state["r"] == "custom_repr"

    with pytest.raises(InterpreterError) as exc_info:
        executor("(1).__class__")
    assert "Forbidden access to dunder attribute" in str(exc_info.value)

    with pytest.raises(InterpreterError) as exc_info:
        executor("class A: pass\nA().__dict__")
    assert "Forbidden access to dunder attribute" in str(exc_info.value)


def test_print_alias_and_map_and_user_super():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    executor('p = print\np("alias_output")')
    assert "alias_output" in executor.logs

    executor('list(map(print, ["map_out1", "map_out2"]))')
    assert "map_out1" in executor.logs
    assert "map_out2" in executor.logs

    code_super = """
def super():
    return 42
res = super()
"""
    executor(code_super)
    assert executor.state["res"] == 42


def test_top_level_signals_and_with_cleanup():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    for sig_code in ("return 5", "break", "continue", "yield 1"):
        with pytest.raises(InterpreterError) as exc_info:
            executor(sig_code)
        assert "SyntaxError" in str(exc_info.value) or "outside" in str(exc_info.value)

    executor("from __future__ import annotations\nx = 100")
    assert executor.state["x"] == 100

    class TrackingCM:
        def __init__(self, log_list):
            self.log_list = log_list

        def __enter__(self):
            self.log_list.append("enter")
            return self

        def __exit__(self, exc_type, exc_val, tb):
            self.log_list.append("exit")
            return True

    from plugin.contrib.smolagents.local_python_executor import evaluate_python_code

    for exc_cls in (KeyboardInterrupt, SystemExit, GeneratorExit):
        trace = []
        with pytest.raises(exc_cls):
            evaluate_python_code(
                f"with cm: raise {exc_cls.__name__}()",
                state={"cm": TrackingCM(trace)},
            )
        assert trace == ["enter", "exit"]


def test_executor_bookkeeping_isolation_and_ops_budget():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    code_mutations = """
_operations_count = {"counter": -999999999}
_print_outputs = 123
del _print_outputs
_active_exceptions = 456
print("still intact")
"""
    executor(code_mutations)
    assert "still intact" in executor.logs

    from unittest.mock import patch

    with patch("plugin.contrib.smolagents.local_python_executor.MAX_OPERATIONS", 50):
        with pytest.raises(InterpreterError) as exc_info:
            executor("for i in range(100): pass")
        assert "Reached the max number of operations" in str(exc_info.value)

    code_class_cell = """
class Base:
    def greet(self):
        return "hello"

class Child(Base):
    def greet(self):
        __class_cell__ = "corrupted"
        return super().greet() + " child"

c = Child()
res = c.greet()
"""
    executor(code_class_cell)
    assert executor.state["res"] == "hello child"


def test_class_body_subscript_assignment():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    code_subscript = """
class A:
    d = {}
    d["x"] = 1
"""
    executor(code_subscript)
    assert getattr(executor.state["A"], "d") == {"x": 1}

    code_attr = """
class Target:
    val = 0
t = Target()
class B:
    t.val = 42
"""
    executor(code_attr)
    assert getattr(executor.state["t"], "val") == 42

    with pytest.raises(InterpreterError) as exc_info:
        executor("class C:\n    a, b = 1, 2\n")
    assert "Unsupported Assign target in class body" in str(exc_info.value)


def test_keyword_only_none_defaults():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    executor("""
def f(*, x=None):
    return x
res_def = f()
res_def_val = f(x=42)

lam = lambda *, x=None: x
res_lam = lam()
res_lam_val = lam(x=99)
""")
    assert executor.state["res_def"] is None
    assert executor.state["res_def_val"] == 42
    assert executor.state["res_lam"] is None
    assert executor.state["res_lam_val"] == 99

    with pytest.raises(InterpreterError) as exc_info:
        executor("def req(*, x): return x\nreq()")
    assert "TypeError" in str(exc_info.value) or "missing" in str(exc_info.value)


def test_chained_comparisons_truthiness_and_short_circuit():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    executor("""
res1 = 1 < 2 < 3
res2 = 1 < 2 > 3
""")
    assert executor.state["res1"] is True
    assert executor.state["res2"] is False

    code_short_circuit = """
trail = []
class ZeroLT:
    def __lt__(self, other):
        return 0

def bad_call():
    trail.append("ran")
    return 10

a = ZeroLT()
res_zero = a < 5 < bad_call()
"""
    executor(code_short_circuit)
    assert executor.state["res_zero"] == 0
    assert executor.state["trail"] == []


def test_hasattr_and_ast_assignment_dunder_guard():
    import ast
    from plugin.contrib.smolagents.local_python_executor import evaluate_ast

    executor = LocalPythonExecutor(additional_authorized_imports=["math"])
    executor.send_tools({})

    with pytest.raises(InterpreterError) as exc_info:
        executor("hasattr(int, '__bases__')")
    assert "Forbidden access to dunder attribute" in str(exc_info.value)

    executor("import math\nres_has = hasattr(math, 'marker')")
    assert executor.state["res_has"] is False

    class Dummy:
        pass

    d = Dummy()
    node_assign = ast.Assign(
        targets=[ast.Attribute(value=ast.Name(id="obj", ctx=ast.Load()), attr="__dict__", ctx=ast.Store())],
        value=ast.Constant(value={}),
    )
    with pytest.raises(InterpreterError) as exc_info:
        evaluate_ast(node_assign, {"obj": d}, {}, {}, [])
    assert "Forbidden access to dunder attribute" in str(exc_info.value)

    node_aug = ast.AugAssign(
        target=ast.Attribute(value=ast.Name(id="obj", ctx=ast.Load()), attr="__dict__", ctx=ast.Store()),
        op=ast.BitOr(),
        value=ast.Constant(value={}),
    )
    with pytest.raises(InterpreterError) as exc_info:
        evaluate_ast(node_aug, {"obj": d}, {}, {}, [])
    assert "Forbidden access to dunder attribute" in str(exc_info.value)

    with pytest.raises(InterpreterError) as exc_info:
        executor("class A: pass\nA().__dict__")
    assert "Forbidden access to dunder attribute" in str(exc_info.value)


def test_except_handler_name_cleanup():
    # Tip: CPython deletes the except ... as target variable on block exit.
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})
    code = """
try:
    raise ValueError("test")
except ValueError as e:
    pass
"""
    executor(code)
    assert "e" not in executor.state

    code_rebind = """
try:
    raise ValueError("test2")
except ValueError as e:
    e = 1
"""
    executor(code_rebind)
    assert "e" not in executor.state


def test_executor_default_static_tools_has_sum():
    # Tip: LocalPythonExecutor initializes with BASE_PYTHON_TOOLS without requiring send_tools.
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    res = executor("result = sum([1, 2, 3])")
    assert res.output == 6
    assert executor.state["result"] == 6


def test_except_handler_as_result_egress():
    # Tip: except Exception as result does not leave the exception object as the cell result.
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

    r = run_sandboxed_code(
        """
try:
    1 / 0
except Exception as result:
    pass
""",
        None,
    )
    assert r["status"] == "ok"
    assert r["result"] is None


def test_timeout_fallback_sets_abandoned_timeout(monkeypatch: pytest.MonkeyPatch):
    # Tip: thread-pool fallback timeout sets abandoned_timeout so the worker process will exit.
    import signal
    import time
    from typing import Any
    import plugin.contrib.smolagents.local_python_executor as lpe
    from plugin.contrib.smolagents.local_python_executor import ExecutionTimeoutError

    lpe.abandoned_timeout = False

    def refuse_sigalrm(signum: Any, handler: Any) -> Any:
        if signum == signal.SIGALRM:
            raise ValueError("SIGALRM unavailable")
        return signal.SIG_DFL

    monkeypatch.setattr(signal, "signal", refuse_sigalrm)

    executor = LocalPythonExecutor(additional_authorized_imports=["time"], timeout_seconds=1)
    executor.send_tools({})
    executor("x = 0")

    def slow_call() -> int:
        time.sleep(1.2)
        return 1

    try:
        executor.send_variables({"slow_call": slow_call})
        with pytest.raises(ExecutionTimeoutError):
            executor("x = slow_call()")

        assert lpe.abandoned_timeout is True
        time.sleep(0.4)
        assert executor.state["x"] == 1
    finally:
        lpe.abandoned_timeout = False


def test_sigalrm_timeout_leaves_abandoned_flag_clear():
    # Tip: normal SIGALRM path unwinds on the same thread without setting abandoned_timeout.
    import time
    import plugin.contrib.smolagents.local_python_executor as lpe
    from plugin.contrib.smolagents.local_python_executor import ExecutionTimeoutError

    lpe.abandoned_timeout = False
    executor = LocalPythonExecutor(additional_authorized_imports=[], timeout_seconds=1)
    executor.send_tools({})
    executor.send_variables({"sleep": time.sleep})

    with pytest.raises(ExecutionTimeoutError):
        executor("sleep(2.5)")

    assert lpe.abandoned_timeout is False


def test_snippets_match_exec():
    # Tip: compare LocalPythonExecutor evaluation with standard exec across language constructs.
    from typing import Any

    snippets = [
        # Arithmetic & operators
        "a = (2 + 3) * 4 - 5 // 2",
        "b = 2 ** 10 % 7",
        "c = -(-5)",
        "d = True and False or True",
        "e = 1 < 2 <= 3 > 0 != 5",
        # Unpacking & literals
        "x, y, *z = [1, 2, 3, 4, 5]",
        "t = (1, *[2, 3], 4)",
        "d_lit = {'a': 1, **{'b': 2, 'c': 3}}",
        "s_lit = {1, 2, *{3, 4}}",
        # Comprehensions
        "lc = [i * 2 for i in range(5) if i % 2 == 0]",
        "dc = {k: v for k, v in [('x', 1), ('y', 2)]}",
        "sc = {x % 3 for x in range(10)}",
        "gc = list(x + 1 for x in [1, 2, 3])",
        # Function defaults & args
        """
def f(a, b=10, *args, **kwargs):
    return a + b + sum(args) + kwargs.get('extra', 0)
f_res = f(1, 2, 3, 4, extra=5)
""",
        # Classes & super
        """
class Base:
    def val(self):
        return 10
class Sub(Base):
    def val(self):
        return super().val() + 5
cls_res = Sub().val()
""",
        # Context manager (with)
        """
class CM:
    def __enter__(self):
        return 42
    def __exit__(self, *args):
        pass
with CM() as ctx_val:
    with_res = ctx_val + 1
""",
        # Formatted string
        "name = 'world'; num = 42; f_res = f'hello {name} {num:04d}'",
        # Ternary conditional
        "cond_res = 'yes' if 10 > 5 else 'no'",
        # Try / except ... as
        """
try:
    int('not_a_number')
except ValueError as err:
    caught = str(err)
""",
        # Loop with break and continue
        """
accum = []
for i in range(10):
    if i == 2:
        continue
    if i == 6:
        break
    accum.append(i)
""",
    ]

    for snippet in snippets:
        exec_scope: dict[str, Any] = {}
        exec(snippet, {}, exec_scope)

        executor = LocalPythonExecutor(additional_authorized_imports=[])
        executor.send_tools({})
        executor(snippet)

        for var, expected_val in exec_scope.items():
            if var.startswith("__"):
                continue
            assert var in executor.state, f"Missing var {var} in snippet: {snippet}"
            if callable(expected_val):
                assert callable(executor.state[var])
            else:
                assert executor.state[var] == expected_val, (
                    f"Mismatch for {var}: executor got {executor.state[var]!r}, exec got {expected_val!r} in snippet: {snippet}"
                )


def test_string_format_safe_formatter_and_wrapper_privates():
    executor = LocalPythonExecutor(additional_authorized_imports=["operator", "string", "itertools", "functools"])
    executor.send_tools({})

    # Dunder attribute access via format variations is rejected.
    dunder_calls = [
        '"{0.__class__}".format(5)',
        '"{0.__class__}".format_map({0: 5})',
        'type("").format("{0.__class__}", 5)',
        'str.format("{0.__class__}", 5)',
        'getattr("{0.__class__}", "format")(5)',
        'import functools\nfunctools.partial(getattr("{0.__class__}", "format"), 5)()',
        'import functools\nfunctools.partial(type("").format, "{0.__class__}")(5)',
        'list(map(getattr("{0.__class__}", "format"), [5]))',
        'import operator\noperator.attrgetter("format")("{0.__class__}")(5)',
        'import operator\noperator.methodcaller("format", 5)("{0.__class__}")',
    ]
    for expr in dunder_calls:
        with pytest.raises(InterpreterError) as exc:
            executor(expr)
        assert "Forbidden access to dunder attribute" in str(exc.value)

    # Wrapper private attributes are hidden.
    for expr in [
        "import operator\nx = operator._real_module",
        "import string\nx = string._real_module",
        "import string\nx = string._authorized_imports",
        "import string, operator\nx = string.Formatter().get_field('0._real_module', [operator], {})",
    ]:
        with pytest.raises((InterpreterError, AttributeError)):
            executor(expr)

    # Formatting __globals__ on a function defined in the cell raises.
    code_globals = """
def my_func():
    pass
res = '{0.__globals__}'.format(my_func)
"""
    with pytest.raises(InterpreterError) as exc:
        executor(code_globals)
    assert "Forbidden access to dunder attribute" in str(exc.value)

    # Standard format forms stay allowed.
    allowed_code = """
import operator, string

class Item:
    def __init__(self, name):
        self.name = name

item = Item("gadget")
r1 = "{:03d}".format(5)
r2 = "{0.real}".format(3+4j)
r3 = string.Formatter().format("{0.real}", 3+4j)
r4 = "{0.name}".format(item)
r5 = operator.methodcaller("upper")("hello")
"""
    executor(allowed_code)
    assert executor.state["r1"] == "005"
    assert executor.state["r2"] == "3.0"
    assert executor.state["r3"] == "3.0"
    assert executor.state["r4"] == "gadget"
    assert executor.state["r5"] == "HELLO"

    # Tip: cache one wrapper per raw module to avoid unbounded growth across executors.
    from plugin.contrib.smolagents.local_python_executor import get_safe_module
    import operator
    list_a = ["operator"]
    list_b = ["operator"]
    mod_a1 = get_safe_module(operator, list_a)
    mod_a2 = get_safe_module(operator, list_a)
    mod_b = get_safe_module(operator, list_b)
    assert mod_a1 is mod_a2
    assert mod_a1 is mod_b


def test_operator_and_string_star_imports():
    # Tip: star-import delegates __all__ and __dir__ so exported names import cleanly.
    executor = LocalPythonExecutor(additional_authorized_imports=["operator", "string"])
    result = executor("from operator import *\nres_add = add(1, 2)\nf = attrgetter('real')\nres_attr = f(3+4j)")
    assert result.output == 3.0
    assert executor.state["res_add"] == 3
    assert executor.state["res_attr"] == 3.0

    result_str = executor("from string import *\nres_letters = ascii_letters[:4]\nfmt = Formatter().format('{0}', 'hi')")
    assert result_str.output == "hi"
    assert executor.state["res_letters"] == "abcd"
    assert executor.state["fmt"] == "hi"



def test_numpy_pickle_policy_enforcement(tmp_path):
    executor = LocalPythonExecutor(additional_authorized_imports=["numpy", "pandas", "functools"])
    executor.send_tools({})

    calls_recorded: list[tuple[str, str, tuple[Any, ...], dict[str, Any]]] = []

    def make_fake(mod: str, name: str):
        def fake(*args: Any, **kwargs: Any) -> str:
            calls_recorded.append((mod, name, args, kwargs))
            return "ok"
        fake.__module__ = mod
        fake.__name__ = name
        return fake

    fake_save = make_fake("numpy", "save")
    fake_savez = make_fake("numpy", "savez")
    fake_write_array = make_fake("numpy", "write_array")
    fake_load = make_fake("numpy", "load")
    fake_read_array = make_fake("numpy", "read_array")

    class FakeNp:
        save = fake_save
        savez = fake_savez
        write_array = fake_write_array
        load = fake_load
        read_array = fake_read_array
    FakeNp.__module__ = "numpy"

    np_obj = FakeNp()
    executor.send_variables({"np": np_obj})

    # Omitted allow_pickle forces False on savers without raising.
    calls_recorded.clear()
    executor("np.save('file', [1, 2])")
    assert calls_recorded[-1][3]["allow_pickle"] is False

    calls_recorded.clear()
    executor("np.savez('file', [1, 2])")
    assert calls_recorded[-1][3]["allow_pickle"] is False

    calls_recorded.clear()
    executor("np.write_array('fp', [1, 2], 1)")
    assert calls_recorded[-1][3]["allow_pickle"] is False

    # Explicit True raises for savers and loaders.
    with pytest.raises(InterpreterError) as exc:
        executor("np.save('file', [1, 2], True)")
    assert "allow_pickle=True" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("np.save('file', [1, 2], allow_pickle=True)")
    assert "allow_pickle=True" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("np.savez('file', allow_pickle=True)")
    assert "allow_pickle=True" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("np.write_array('fp', [1, 2], 1, True)")
    assert "allow_pickle=True" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("np.load('file', None, True)")
    assert "allow_pickle=True" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("np.load('file', allow_pickle=True)")
    assert "allow_pickle=True" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("np.read_array('fp', True)")
    assert "allow_pickle=True" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("np.read_array('fp', allow_pickle=True)")
    assert "allow_pickle=True" in str(exc.value)

    # functools.partial with wrapped fake cannot pass allow_pickle=True.
    executor("import functools\np = functools.partial(np.save, 'file', [1, 2])")
    with pytest.raises(InterpreterError) as exc:
        executor("p(allow_pickle=True)")
    assert "allow_pickle=True" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("p(True)")
    assert "allow_pickle=True" in str(exc.value)

    # from numpy import save as s with fake.
    import sys
    import types
    orig_np = sys.modules.get("numpy")
    fake_mod = types.ModuleType("numpy")
    setattr(fake_mod, "save", fake_save)
    sys.modules["numpy"] = fake_mod
    try:
        executor_import = LocalPythonExecutor(additional_authorized_imports=["numpy"])
        with pytest.raises(InterpreterError) as exc:
            executor_import("from numpy import save as s\ns('file', [1, 2], allow_pickle=True)")
        assert "allow_pickle=True" in str(exc.value)
    finally:
        if orig_np is not None:
            sys.modules["numpy"] = orig_np
        else:
            sys.modules.pop("numpy", None)

    # If NumPy imports, numeric np.save succeeds and object-array save raises.
    try:
        import importlib.util
        if importlib.util.find_spec("numpy") is None:
            return
        executor_real = LocalPythonExecutor(additional_authorized_imports=["numpy"])
        executor_real.send_variables({"path_num": str(tmp_path / "num.npy"), "path_obj": str(tmp_path / "obj.npy")})
        real_code = """
import numpy as np
arr_num = np.array([1, 2, 3])
np.save(path_num, arr_num)
"""
        executor_real(real_code)

        real_obj_code = """
import numpy as np
class Dummy:
    pass
arr_obj = np.array([Dummy()])
np.save(path_obj, arr_obj)
"""
        with pytest.raises(InterpreterError) as exc:
            executor_real(real_obj_code)
        assert "cannot be saved when allow_pickle=False" in str(exc.value)
    except ImportError:
        pass


def test_bounded_print_container_during_execution():
    executor = LocalPythonExecutor(additional_authorized_imports=[], max_print_outputs_length=80)
    executor.send_tools({})

    code = """
for i in range(100):
    print(f"iter={i}")
"""
    executor(code)
    assert len(executor.logs) < 200
    assert "truncated to stay below 80 characters" in executor.logs
    assert "iter=99" in executor.logs


def test_cpython_divergences():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    # 1. f-strings call __format__
    code_fmt = """
class Formatted:
    def __str__(self):
        return "STR"
    def __format__(self, spec):
        return f"FMT{spec}"

obj = Formatted()
width = "03d"
res = f"{obj!s}|{obj:spec}|{obj}|{obj:{width}}"
"""
    executor(code_fmt)
    assert executor.state["res"] == "STR|FMTspec|FMT|FMT03d"

    code_bad_fmt = """
class BadFmt:
    def __format__(self, spec):
        return 123

f"{BadFmt()}"
"""
    with pytest.raises(InterpreterError) as exc:
        executor(code_bad_fmt)
    assert "TypeError" in str(exc.value)

    # 2. __init__ may not return a value
    code_init_return = """
class BadInit:
    def __init__(self):
        return 5

b = BadInit()
"""
    with pytest.raises(InterpreterError) as exc:
        executor(code_init_return)
    assert "TypeError" in str(exc.value)
    assert "__init__() should return None" in str(exc.value)

    code_init_trailing = """
class GoodInit:
    def __init__(self):
        self.x = 42
        1 + 1

g = GoodInit()
res_x = g.x
"""
    executor(code_init_trailing)
    assert executor.state["res_x"] == 42

    # 3. del obj.attr
    code_del = """
class Obj:
    def __init__(self):
        self.attr = "hello"

o = Obj()
del o.attr
has_attr = hasattr(o, "attr")
"""
    executor(code_del)
    assert executor.state["has_attr"] is False

    with pytest.raises(InterpreterError) as exc:
        executor("o = Obj()\ndel o.__class__")
    assert "Forbidden access to dunder attribute" in str(exc.value)

    with pytest.raises(InterpreterError) as exc:
        executor("class C: x = 1\ndel C.x")
    assert "Cannot delete attributes on a module or a type" in str(exc.value)

    # 4. Expression callees
    code_expr_callee = """
def add1(x): return x + 1
def add2(x): return x + 2

r1 = (add1 if True else add2)(10)
r2 = (add1 if False else add2)(10)
r3 = (add1 or add2)(20)
"""
    executor(code_expr_callee)
    assert executor.state["r1"] == 11
    assert executor.state["r2"] == 12
    assert executor.state["r3"] == 21

    with pytest.raises(InterpreterError) as exc:
        executor("(123 if True else 456)()")
    assert "This is not a correct function" in str(exc.value)


def test_stable_tool_wrappers_and_safety_checks():
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({})

    # 1. (max is max) is True in cell
    executor("same_max = (max is max)")
    assert executor.state["same_max"] is True

    # 2. Custom tool returning dangerous value is rejected
    import os
    def evil_tool():
        return os

    executor.send_tools({"get_evil": evil_tool})
    with pytest.raises(InterpreterError) as exc:
        executor("get_evil()")
    assert "Forbidden access to module" in str(exc.value)

    # 3. Comprehension results unchanged with fast path
    executor("res_comp = [i * 2 for i in range(10)]")
    assert executor.state["res_comp"] == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18]










