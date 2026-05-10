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
from plugin.modules.calc.python_executor import PythonExecutor, ExecutePythonScript

def test_arithmetic():
    executor = PythonExecutor("test_url")
    assert executor.execute_with_return("1 + 1") == 2
    assert executor.execute_with_return("x = 10; x * 2") == 20

def test_assignment():
    executor = PythonExecutor("test_url")
    # Last line is an assignment
    assert executor.execute_with_return("y = 100") == 100
    # Verify it's in the state
    assert executor.executor.state["y"] == 100

def test_persistence():
    executor = PythonExecutor("test_url")
    executor.execute_with_return("z = 5")
    assert executor.execute_with_return("z + 10") == 15

def test_fn_definition():
    executor = PythonExecutor("test_url")
    code = """
def add_five(n):
    return n + 5

add_five(10)
"""
    assert executor.execute_with_return(code) == 15

def test_class_definition():
    executor = PythonExecutor("test_url")
    code = """
class Counter:
    def __init__(self):
        self.count = 0
    def inc(self):
        self.count += 1
        return self.count

c = Counter()
c.inc()
c.inc()
"""
    assert executor.execute_with_return(code) == 2

def test_reset():
    executor = PythonExecutor("test_url")
    executor.execute_with_return("a = 1")
    assert executor.executor.state["a"] == 1
    executor.reset()
    assert "a" not in executor.executor.state

def test_syntax_error():
    executor = PythonExecutor("test_url")
    from plugin.framework.errors import WriterAgentException
    with pytest.raises(WriterAgentException) as excinfo:
        executor.execute_with_return("if x")
    assert excinfo.value.code == "PYTHON_EXECUTION_ERROR"

def test_tool_integration():
    ctx = MagicMock()
    ctx.doc.getURL.return_value = "file:///test.ods"
    
    # Mocking the bridge and manipulator
    # Note: These are instantiated inside execute() so we'll need to mock them if we want to verify calls
    # but for a basic integration test we can just ensure they don't crash.
    
    tool = ExecutePythonScript()
    
    # First call
    res1 = tool.execute(ctx, script="val = 42")
    assert res1["status"] == "ok"
    assert res1["result"] == 42
    
    # Second call (persistence check)
    res2 = tool.execute(ctx, script="val + 8")
    assert res2["status"] == "ok"
    assert res2["result"] == 50

def test_formatting():
    """format_result stringifies custom objects; class must live in executor env (same snippet)."""
    executor = PythonExecutor("test")
    code = """
class MyObj:
    def __str__(self): return "CUSTOM_OBJ"

MyObj()
"""
    res = executor.execute_with_return(code)
    assert "CUSTOM_OBJ" in res

def test_native_helpers():
    # This requires more complex mocking since bridge/manipulator are used
    executor = PythonExecutor("test")
    bridge = MagicMock()
    manipulator = MagicMock()
    inspector = MagicMock()
    
    manipulator.safe_get_cell_value.return_value = "HELLO"
    inspector.read_range.return_value = [[{"value": 1}, {"value": 2}]]
    
    executor.inject_helpers(bridge, manipulator, inspector)
    
    # Test reading cell
    assert executor.execute_with_return("lp('A1')") == "HELLO"
    
    # Test reading range
    assert executor.execute_with_return("Sheet('A1:B1')") == [[1, 2]]
    
    # Test writing
    executor.execute_with_return("set_range('C1', 123)")
    manipulator.write_formula_range.assert_called_with('C1', 123)

def test_target_range_tool():
    from plugin.modules.calc.python_executor import ExecutePythonScript
    tool = ExecutePythonScript()
    
    ctx = MagicMock()
    ctx.doc.getURL.return_value = "file:///test.ods"
    
    # We will need to patch CellManipulator to verify the write
    with unittest.mock.patch('plugin.modules.calc.python_executor.CellManipulator') as MockManip:
        inst = MockManip.return_value
        inst.write_formula_range.return_value = "Success"
        
        res = tool.execute(ctx, script="10 + 20", target_range="D1")
        assert res["result"] == 30
        assert res["write_status"] == "Success"
        inst.write_formula_range.assert_called_with("D1", 30)
