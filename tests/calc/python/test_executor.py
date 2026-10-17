# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# Based on code from the LibrePythonista project (Apache 2.0)
# Source: https://github.com/Amourspirit/python-libre-pythonista-ext/blob/main/tests/test_code/test_code_ast.py
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import pytest
import unittest.mock
from unittest.mock import MagicMock
from plugin.calc.python.executor import PythonExecutor, ExecutePythonScript

def test_arithmetic():
    executor = PythonExecutor()
    assert executor.execute_with_return("1 + 1") == 2
    assert executor.execute_with_return("x = 10; x * 2") == 20

def test_assignment():
    executor = PythonExecutor()
    # Last line is an assignment
    assert executor.execute_with_return("y = 100") == 100
    # Verify it's in the state
    assert executor.executor.state["y"] == 100

def test_no_persistence_across_executor_instances():
    e1 = PythonExecutor()
    e1.execute_with_return("z = 5")
    e2 = PythonExecutor()
    from plugin.framework.errors import WriterAgentException
    with pytest.raises(WriterAgentException):
        e2.execute_with_return("z + 10")

@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param("""
def add_five(n):
    return n + 5

add_five(10)
""", 15, id="test_fn_definition"),
        pytest.param("""
class Counter:
    def __init__(self):
        self.count = 0
    def inc(self):
        self.count += 1
        return self.count

c = Counter()
c.inc()
c.inc()
""", 2, id="test_class_definition"),
    ],
)
def test_fn_definition(value, expected):
    executor = PythonExecutor()
    code = value
    assert executor.execute_with_return(code) == expected


def test_syntax_error():
    executor = PythonExecutor()
    from plugin.framework.errors import WriterAgentException
    with pytest.raises(WriterAgentException) as excinfo:
        executor.execute_with_return("if x")
    assert excinfo.value.code == "PYTHON_EXECUTION_ERROR"

def test_tool_integration():
    ctx = MagicMock()
    ctx.doc.getURL.return_value = "file:///test.ods"
    ctx.doc.supportsService.return_value = True
    
    # Mocking the bridge and manipulator
    # Note: These are instantiated inside execute() so we'll need to mock them if we want to verify calls
    # but for a basic integration test we can just ensure they don't crash.
    
    tool = ExecutePythonScript()
    
    # First call
    res1 = tool.execute(ctx, code="val = 42")
    assert res1["status"] == "ok"
    assert res1["result"] == 42
    
    # Second call: fresh environment (no val from first call)
    from plugin.framework.errors import WriterAgentException
    with pytest.raises(WriterAgentException):
        tool.execute(ctx, code="val + 8")

def test_formatting():
    """format_result stringifies custom objects; class must live in executor env (same snippet)."""
    executor = PythonExecutor()
    code = """
class MyObj:
    def __str__(self): return "CUSTOM_OBJ"

MyObj()
"""
    res = executor.execute_with_return(code)
    assert "CUSTOM_OBJ" in res

def test_native_helpers():
    # This requires more complex mocking since bridge/manipulator are used
    executor = PythonExecutor()
    manipulator = MagicMock()
    inspector = MagicMock()
    
    # lp() reads every address (single cells included) through inspector.read_range.
    inspector.read_range.side_effect = lambda addr: [[{"value": "HELLO"}]] if addr == "A1" else [[{"value": 1}, {"value": 2}]]
    
    executor.inject_helpers(manipulator, inspector)
    
    # Test reading cell
    assert executor.execute_with_return("lp('A1')") == "HELLO"
    
    # Test reading range
    assert executor.execute_with_return("Sheet('A1:B1')") == [[1, 2]]
    
    # Test writing
    executor.execute_with_return("set_range('C1', 123)")
    manipulator.write_formula_range.assert_called_with('C1', 123)

def test_target_range_tool():
    from plugin.calc.python.executor import ExecutePythonScript
    tool = ExecutePythonScript()
    
    ctx = MagicMock()
    ctx.doc.getURL.return_value = "file:///test.ods"
    ctx.doc.supportsService.return_value = True
    
    # We will need to patch CellManipulator to verify the write
    with unittest.mock.patch('plugin.calc.python.executor.CellManipulator') as MockManip:
        inst = MockManip.return_value
        inst.write_formula_range.return_value = "Success"
        
        res = tool.execute(ctx, code="10 + 20", target_range="D1")
        assert res["result"] == 30
        assert res["write_status"] == "Success"
        inst.write_formula_range.assert_called_with("D1", 30)

def test_data_range_injection():
    from plugin.calc.python.executor import ExecutePythonScript
    tool = ExecutePythonScript()
    
    ctx = MagicMock()
    ctx.doc.getURL.return_value = "file:///test.ods"
    ctx.doc.supportsService.return_value = True
    
    with unittest.mock.patch('plugin.calc.python.executor.CellInspector') as MockInspector:
        inst = MockInspector.return_value
        inst.read_range.return_value = [[{"value": 10}, {"value": 20}, {"value": 30}]]
        
        # Test passing data_range which is a row (gets flattened to flat list)
        code = "result = 0\nfor x in data:\n    result += x\nresult"
        res = tool.execute(ctx, code=code, data_range="A1:C1")
        assert res["result"] == 60
        inst.read_range.assert_called_with("A1:C1")


def test_lp_helper_reraises_execution_timeout():
    """SIGALRM timeout must leave lp(); swallowing it leaves the alarm disarmed."""
    from plugin.contrib.smolagents.local_python_executor import ExecutionTimeoutError

    executor = PythonExecutor()
    manipulator = MagicMock()
    inspector = MagicMock()
    inspector.read_range.side_effect = ExecutionTimeoutError("budget")
    executor.inject_helpers(manipulator, inspector)
    lp = executor.executor.state["lp"]
    with pytest.raises(ExecutionTimeoutError):
        lp("A1")
    with pytest.raises(ExecutionTimeoutError):
        lp("A1:B2")


def test_lp_helper_raises_on_cell_read_errors():
    from plugin.framework.errors import WriterAgentException

    executor = PythonExecutor()
    manipulator = MagicMock()
    inspector = MagicMock()
    inspector.read_range.side_effect = RuntimeError("missing sheet")
    executor.inject_helpers(manipulator, inspector)
    with pytest.raises(WriterAgentException) as excinfo:
        executor.executor.state["lp"]("A1")
    assert "A1" in str(excinfo.value)
    assert "missing sheet" in str(excinfo.value)


def test_lp_helper_sheet_prefix_invokes_read_range():
    executor = PythonExecutor()
    manipulator = MagicMock()
    inspector = MagicMock()
    inspector.read_range.return_value = [[{"value": 42}]]
    executor.inject_helpers(manipulator, inspector)

    val = executor.executor.state["lp"]("Sheet2.A1")
    assert val == 42
    inspector.read_range.assert_called_once_with("Sheet2.A1")
    # Reads never go through the manipulator (it only writes, via set_range).
    assert manipulator.method_calls == []


def test_format_result_stringifies_non_json_values():
    import datetime
    import json

    executor = PythonExecutor()
    # Non-json leaf types are stringified
    samples = [executor.format_result(value) for value in ({1, 2}, b"ab", range(3), (x for x in (1, 2)))]
    for formatted in samples:
        assert isinstance(formatted, str)
        json.dumps(formatted)

    # Tuples become lists at top level and nested
    assert executor.format_result((1, 2)) == [1, 2]
    assert executor.format_result([(1, 2), (3, 4)]) == [[1, 2], [3, 4]]

    # Nested non-JSON values are deeply walked and stringified
    nested_date = executor.format_result([datetime.date(2020, 1, 2)])
    assert nested_date == ["<Result: 2020-01-02>"]
    json.dumps(nested_date)

    nested_set = executor.format_result([{"s": {1, 2}}])
    assert isinstance(nested_set[0]["s"], str)
    assert nested_set[0]["s"].startswith("<Result:")
    json.dumps(nested_set)

    # Dict with non-string keys stringifies keys
    int_keyed = executor.format_result({1: "a", 2: (3, 4)})
    assert int_keyed == {"1": "a", "2": [3, 4]}
    json.dumps(int_keyed)

    assert executor.format_result(None) is None
    assert executor.format_result([1, {"a": 2}]) == [1, {"a": 2}]
    assert executor.format_result({"a": 1}) == {"a": 1}

    # execute_with_return tests
    assert executor.execute_with_return("{1, 2}").startswith("<Result:")
    json.dumps(executor.execute_with_return('b"ab"'))
    res_tuple = executor.execute_with_return("(1, 2)")
    assert res_tuple == [1, 2]
    json.dumps(res_tuple)  # strict json.dumps without default succeeds


def test_target_range_writes_tuple_as_list():
    """target_range of return (1, 2) writes two values as a list, not '<Result:' text."""
    from plugin.calc.python.executor import ExecutePythonScript

    tool = ExecutePythonScript()
    ctx = MagicMock()
    ctx.doc.getURL.return_value = "file:///test.ods"
    ctx.doc.supportsService.return_value = True

    with unittest.mock.patch("plugin.calc.python.executor.CellManipulator") as MockManip:
        inst = MockManip.return_value
        inst.write_formula_range.return_value = "Success"

        res = tool.execute(ctx, code="(1, 2)", target_range="D1")
        assert res["result"] == [1, 2]
        assert res["write_status"] == "Success"
        inst.write_formula_range.assert_called_with("D1", [1, 2])


def test_data_range_read_failure_returns_error_result():
    """read_range failure returns an error status naming the range; execute_with_return does not run."""
    from plugin.calc.python.executor import ExecutePythonScript

    tool = ExecutePythonScript()
    ctx = MagicMock()
    ctx.doc.getURL.return_value = "file:///test.ods"
    ctx.doc.supportsService.return_value = True

    with (
        unittest.mock.patch("plugin.calc.python.executor.CellInspector") as MockInspector,
        unittest.mock.patch("plugin.calc.python.executor.PythonExecutor.execute_with_return") as mock_exec,
    ):
        inst = MockInspector.return_value
        inst.read_range.side_effect = RuntimeError("Bad range syntax")

        res = tool.execute(ctx, code="data[0]", data_range="INVALID_RANGE")
        assert res["status"] == "error"
        assert res["code"] == "CALC_READ_ERROR"
        assert "INVALID_RANGE" in res["message"]
        assert "Bad range syntax" in res["message"]
        mock_exec.assert_not_called()


def test_data_range_tool_execution_error_returns_calc_read_error() -> None:
    """read_range's ToolExecutionError must become CALC_READ_ERROR, not a raise.

    ToolExecutionError subclasses WriterAgentException. The old check re-raised
    it, so the CALC_READ_ERROR return was unreachable for the exception
    CellInspector.read_range actually raises. The script has not run, so the
    error dict has no result or stdout.
    """
    from plugin.framework.errors import ToolExecutionError

    tool = ExecutePythonScript()
    ctx = MagicMock()
    ctx.doc.getURL.return_value = "file:///test.ods"
    ctx.doc.supportsService.return_value = True

    with (
        unittest.mock.patch("plugin.calc.python.executor.CellInspector") as MockInspector,
        unittest.mock.patch("plugin.calc.python.executor.PythonExecutor.execute_with_return") as mock_exec,
    ):
        inst = MockInspector.return_value
        inst.read_range.side_effect = ToolExecutionError("Bad range syntax")

        res = tool.execute(ctx, code="data[0]", data_range="INVALID_RANGE")
        assert res["status"] == "error"
        assert res["code"] == "CALC_READ_ERROR"
        assert "INVALID_RANGE" in res["message"]
        assert "Bad range syntax" in res["message"]
        assert "details" not in res
        mock_exec.assert_not_called()


def test_calc_helpers_fail_on_writer_document():
    from plugin.framework.errors import WriterAgentException

    tool = ExecutePythonScript()
    ctx = MagicMock()
    ctx.doc.getURL.return_value = "file:///note.odt"
    ctx.doc.supportsService.side_effect = lambda name: name == "com.sun.star.text.TextDocument"

    res = tool.execute(ctx, code="1 + 1")
    assert res["status"] == "ok"
    assert res["result"] == 2

    with pytest.raises(WriterAgentException) as excinfo:
        tool.execute(ctx, code="lp('A1')")
    assert excinfo.value.code == "PYTHON_EXECUTION_ERROR"
    assert "Calc" in str(excinfo.value)

    with pytest.raises(WriterAgentException) as range_exc:
        tool.execute(ctx, code="1", data_range="A1:A2")
    assert range_exc.value.code == "UNSUPPORTED_OPERATION"


def test_execute_with_return_catches_timeout_and_exit_signals():
    from unittest.mock import MagicMock
    from plugin.calc.python.executor import PythonExecutor, WriterAgentException
    from plugin.contrib.smolagents.local_python_executor import ExecutionTimeoutError

    executor = PythonExecutor()

    # ExecutionTimeoutError
    executor.executor = MagicMock()
    executor.executor.side_effect = ExecutionTimeoutError("timed out")
    with pytest.raises(WriterAgentException) as exc_info:
        executor.execute_with_return("pass")
    assert "timed out" in str(exc_info.value)
    assert exc_info.value.code == "PYTHON_EXECUTION_ERROR"

    # SystemExit
    executor.executor.side_effect = SystemExit(4)
    with pytest.raises(WriterAgentException) as exc_info:
        executor.execute_with_return("pass")
    assert "SystemExit" in str(exc_info.value)
    assert exc_info.value.code == "PYTHON_EXECUTION_ERROR"

    # KeyboardInterrupt
    executor.executor.side_effect = KeyboardInterrupt()
    with pytest.raises(WriterAgentException) as exc_info:
        executor.execute_with_return("pass")
    assert "KeyboardInterrupt" in str(exc_info.value)
    assert exc_info.value.code == "PYTHON_EXECUTION_ERROR"


def test_interpreter_error_logs_warning_without_source(caplog: pytest.LogCaptureFixture) -> None:
    import logging
    from plugin.calc.python.executor import PythonExecutor
    from plugin.framework.errors import WriterAgentException

    executor = PythonExecutor()
    bad_code = "secret_token = 'SUPER_SECRET_KEY'\nundefined_symbol_xyz()"

    with caplog.at_level(logging.WARNING, logger="plugin.calc.python.executor"):
        with pytest.raises(WriterAgentException):
            executor.execute_with_return(bad_code)

    assert bad_code not in caplog.text
    assert "SUPER_SECRET_KEY" not in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert any("snippet length" in r.message for r in caplog.records)


def test_builtins_and_print_stdout() -> None:
    executor = PythonExecutor()
    assert executor.execute_with_return("len([1, 2, 3])") == 3
    assert executor.execute_with_return("sum([1, 2])") == 3
    assert executor.execute_with_return("sorted([3, 1])") == [1, 3]
    assert executor.execute_with_return("print('hi')\n5") == 5
    assert executor.last_stdout() == "hi\n"


def test_execute_python_script_returns_stdout() -> None:
    tool = ExecutePythonScript()
    ctx = MagicMock()
    ctx.doc.getURL.return_value = "file:///test.ods"
    ctx.doc.supportsService.return_value = True

    res = tool.execute(ctx, code="print(1)\n2")
    assert res["status"] == "ok"
    assert res["result"] == 2
    assert res["stdout"] == "1\n"


def test_normalize_value_refuses_colliding_keys() -> None:
    from plugin.framework.errors import WriterAgentException

    executor = PythonExecutor()
    with pytest.raises(ValueError) as excinfo:
        executor.format_result({1: "a", "1": "b"})
    assert "collide" in str(excinfo.value)

    with pytest.raises(WriterAgentException) as exc_info:
        executor.execute_with_return('{1: "a", "1": "b"}')
    assert exc_info.value.code == "PYTHON_EXECUTION_ERROR"
    assert "collide" in str(exc_info.value)

    assert executor.format_result({1: "a", 2: "b"}) == {"1": "a", "2": "b"}


def test_lp_helper_reraises_disposed_and_writeragent() -> None:
    class DummyDisposedException(Exception):
        pass

    executor = PythonExecutor()
    manipulator = MagicMock()
    inspector = MagicMock()
    inspector.read_range.side_effect = DummyDisposedException("document was closed")
    executor.inject_helpers(manipulator, inspector)

    with pytest.raises(DummyDisposedException):
        executor.executor.state["lp"]("A1")

    from plugin.framework.errors import WriterAgentException

    inspector.read_range.side_effect = WriterAgentException("custom failure", code="UNSUPPORTED_OPERATION")
    with pytest.raises(WriterAgentException) as excinfo:
        executor.executor.state["lp"]("A1")
    assert excinfo.value.code == "UNSUPPORTED_OPERATION"


def test_target_range_write_failure_is_tool_error() -> None:
    tool = ExecutePythonScript()
    ctx = MagicMock()
    ctx.doc.getURL.return_value = "file:///test.ods"
    ctx.doc.supportsService.return_value = True

    with unittest.mock.patch("plugin.calc.python.executor.CellManipulator") as MockManip:
        inst = MockManip.return_value
        inst.write_formula_range.side_effect = RuntimeError("disk full")

        res = tool.execute(ctx, code="print('logging')\n42", target_range="D1")
        assert res["status"] == "error"
        assert res["code"] == "CALC_WRITE_ERROR"
        assert "disk full" in res["message"]
        assert "D1" in res["message"]
        assert res["details"]["result"] == 42
        assert isinstance(res["details"]["stdout"], str)
        assert "logging" in res["details"]["stdout"]


def test_target_range_calc_error_preserves_result_and_stdout() -> None:
    """write_formula_range's CalcError must be CALC_WRITE_ERROR and keep result and stdout.

    CalcError subclasses WriterAgentException. The old check re-raised it, so
    the CALC_WRITE_ERROR return was unreachable and the computed result and
    stdout were dropped.
    """
    from plugin.framework.errors import CalcError

    tool = ExecutePythonScript()
    ctx = MagicMock()
    ctx.doc.getURL.return_value = "file:///test.ods"
    ctx.doc.supportsService.return_value = True

    with unittest.mock.patch("plugin.calc.python.executor.CellManipulator") as MockManip:
        inst = MockManip.return_value
        inst.write_formula_range.side_effect = CalcError("disk full")

        res = tool.execute(ctx, code="print('logging')\n42", target_range="D1")
        assert res["status"] == "error"
        assert res["code"] == "CALC_WRITE_ERROR"
        assert "disk full" in res["message"]
        assert "D1" in res["message"]
        assert res["details"]["result"] == 42
        assert isinstance(res["details"]["stdout"], str)
        assert "logging" in res["details"]["stdout"]


def test_data_range_disposal_propagates() -> None:
    class DummyDisposedException(Exception):
        pass

    tool = ExecutePythonScript()
    ctx = MagicMock()
    ctx.doc.getURL.return_value = "file:///test.ods"
    ctx.doc.supportsService.return_value = True

    with unittest.mock.patch("plugin.calc.python.executor.CellInspector") as MockInspector:
        inst = MockInspector.return_value
        inst.read_range.side_effect = DummyDisposedException("disposed doc")

        with pytest.raises(DummyDisposedException):
            tool.execute(ctx, code="data[0]", data_range="A1:A2")

