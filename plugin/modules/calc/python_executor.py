# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# Based on code from the LibrePythonista project (Apache 2.0)
# Source: https://github.com/Amourspirit/python-libre-pythonista-ext/blob/main/oxt/pythonpath/libre_pythonista_lib/doc/calc/doc/sheet/cell/code/py_module.py
# and: https://github.com/Amourspirit/python-libre-pythonista-ext/blob/main/tests/test_code/test_code_ast.py
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
"""Pure Python execution engine for Calc formulas and scripts.

Implements an AST-based 'pop-and-eval' trick to allow Python scripts to behave 
like formulas by returning the value of the last expression.
"""

import ast
import types
import logging
from typing import Any, cast

from plugin.framework.tool_base import ToolBase
from plugin.framework.errors import WriterAgentException
from plugin.modules.calc.bridge import CalcBridge
from plugin.modules.calc.manipulator import CellManipulator
from plugin.modules.calc.inspector import CellInspector

logger = logging.getLogger("writeragent.calc.python_executor")

# FIXME: Embedded Calc Python executor requires exec/eval (Bandit B102/B307). Future work:
# restricted globals/builtins, execution timeouts, or a safer subset interpreter; then drop nosec.

def get_module_init_code() -> str:
    """Returns standard imports and helper definitions for the Python environment."""
    return """
import math
import datetime
import random
import json
import re
import collections
import itertools
import statistics

# The 'lp' and 'Sheet' objects will be injected at runtime
"""


class PythonExecutor:
    """Handles the AST transformation and execution of Python code with document awareness."""

    def __init__(self, doc_url: str):
        self.doc_url = doc_url
        self.mod = types.ModuleType("CalcPythonEnv")
        self.reset()

    def reset(self):
        """Resets the environment to its initial state."""
        self.mod.__dict__.clear()
        # Pre-populate with standard imports
        self.execute_raw(get_module_init_code())

    def execute_raw(self, code: str):
        """Executes code normally using exec()."""
        exec(code, self.mod.__dict__)  # nosec B102

    def inject_helpers(self, bridge: CalcBridge, manipulator: CellManipulator, inspector: CellInspector):
        """Injects document interaction helpers into the environment."""

        def lp_helper(addr: str) -> Any:
            """Read values from the spreadsheet. Generic for cells or ranges."""
            try:
                if ":" in addr:
                    data = inspector.read_range(addr)
                    # Convert to simple list-of-lists of values
                    return [[cell["value"] for cell in row] for row in data]
                else:
                    sheet = bridge.get_active_sheet()
                    return manipulator.safe_get_cell_value(sheet, addr)
            except Exception as e:
                logger.error("lp_helper error for address %s: %s", addr, e)
                return None

        # Aliases for convenience
        self.mod.__dict__["lp"] = lp_helper
        self.mod.__dict__["Sheet"] = lp_helper
        self.mod.__dict__["get_range"] = lp_helper
        
        def set_range_helper(addr: str, data: Any):
            """Write values back to the spreadsheet."""
            return manipulator.write_formula_range(addr, data)

        self.mod.__dict__["set_range"] = set_range_helper

    def format_result(self, result: Any) -> Any:
        """Processes the result for storage and display."""
        # Handle 1D/2D lists specifically for Calc
        if isinstance(result, (list, tuple)):
            # If it's a list but not a list-of-lists, we might want to make it 2D
            if result and not isinstance(result[0], (list, tuple)):
                # Just leave it 1D for now, tool output handles it
                pass
        
        # Ensure we don't return un-serializable objects as raw references if possible
        if hasattr(result, "__dict__") and not isinstance(result, (list, tuple, dict, str, int, float, bool)):
            try:
                return f"<Result: {str(result)}>"
            except Exception:
                return f"<Object: {type(result).__name__}>"

        return result

    def execute_with_return(self, code_snippet: str) -> Any:
        """
        Compiles and executes the given code snippet.
        - If the last statement is an expression, returns its value.
        - If the last statement is an assignment, returns the assigned value.
        """
        try:
            # Parse the code as a full module
            tree = ast.parse(code_snippet, mode="exec")

            if not tree.body:
                return None

            # If the last node is an expression or assignment, remove it for separate handling
            last_expr: ast.stmt | None = None
            assign_name = ""
            last_node = tree.body[-1]

            if isinstance(last_node, ast.Expr):
                last_expr = cast("ast.Expr", tree.body.pop())
            elif isinstance(last_node, (ast.Assign, ast.AnnAssign)):
                # For assignments, we POP the node so it doesn't run in exec(),
                # then we evaluate its VALUE in eval() and manually set the name.
                last_expr = tree.body.pop()
                if isinstance(last_expr, ast.Assign):
                    if isinstance(last_expr.targets[0], ast.Name):
                        assign_name = last_expr.targets[0].id
                elif isinstance(last_expr, ast.AnnAssign):
                    if isinstance(last_expr.target, ast.Name):
                        assign_name = last_expr.target.id

            # Compile and execute the body (everything but the last expression)
            module_body = ast.fix_missing_locations(ast.Module(body=tree.body, type_ignores=[]))
            exec_code = compile(module_body, "<string>", "exec")
            
            # Execute in the shared module dictionary
            exec(exec_code, self.mod.__dict__)  # nosec B102

            result = None
            # If there was a final expression node, evaluate it
            if last_expr:
                # ast.Assign/AnnAssign have .value, ast.Expr has .value
                # We already filtered for these three types above.
                val_node = getattr(last_expr, "value", None)
                if val_node:
                    expr = ast.Expression(val_node)
                    expr = ast.fix_missing_locations(expr)
                    eval_code = compile(expr, "<string>", "eval")
                    result = eval(eval_code, self.mod.__dict__)  # nosec B307
                
                # If it was an assignment, make sure the value is stored
                if assign_name:
                    self.mod.__dict__[assign_name] = result
                
                # Store the result in '_' just like a REPL
                self.mod.__dict__["_"] = result

            return self.format_result(result)

        except SyntaxError as e:
            logger.exception("Syntax error executing Python code: \n%s", code_snippet)
            raise WriterAgentException(f"Syntax Error: {e.msg} at line {e.lineno}", code="PYTHON_SYNTAX_ERROR")
        except WriterAgentException:
            raise
        except Exception as e:
            logger.exception("Error executing Python code: \n%s", code_snippet)
            raise WriterAgentException(f"Execution Error: {str(e)}", code="PYTHON_EXECUTION_ERROR")


# Global cache: one PythonExecutor per document URL
_EXECUTOR_ENV_CACHE: dict[str, PythonExecutor] = {}


def get_executor_for_doc(doc_url: str) -> PythonExecutor:
    """Retrieves or creates a PythonExecutor for the given document."""
    if doc_url not in _EXECUTOR_ENV_CACHE:
        _EXECUTOR_ENV_CACHE[doc_url] = PythonExecutor(doc_url)
    return _EXECUTOR_ENV_CACHE[doc_url]


class ExecutePythonScript(ToolBase):
    """Executes a Python script in a persistent document-specific environment."""

    name = "execute_python_script"
    intent = "analyze"
    description = (
        "Executes a Python script within a persistent document-specific environment. "
        "The value of the last expression or assignment in the script is returned. "
        "Helpers: lp('A1:B10') reads range, set_range('C1', data) writes result. "
        "Variables defined in one call persist for subsequent calls on the same document."
    )
    parameters = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "The Python code to execute.",
            },
            "target_range": {
                "type": "string",
                "description": "Optional: Cell range (e.g. 'A1') to write the script result to.",
            },
            "reset": {
                "type": "boolean",
                "description": "If true, resets the Python environment for this document before executing.",
                "default": False
            }
        },
        "required": ["script"],
    }
    # Available in Calc and Writer
    uno_services = [
        "com.sun.star.sheet.SpreadsheetDocument",
        "com.sun.star.text.TextDocument"
    ]
    is_mutation = True # Set to True because it can use set_range!

    def execute(self, ctx, **kwargs):
        script = kwargs.get("script", "")
        reset = kwargs.get("reset", False)
        target_range = kwargs.get("target_range")
        
        doc_url = ctx.doc.getURL() or "untitled"
        executor = get_executor_for_doc(doc_url)
        
        if reset:
            executor.reset()
            
        # Inject standard Calc helpers for this call
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        inspector = CellInspector(bridge)
        executor.inject_helpers(bridge, manipulator, inspector)
            
        result = executor.execute_with_return(script)
        
        # If target_range is specified, write back
        write_status = ""
        if target_range and result is not None:
            try:
                write_status = manipulator.write_formula_range(target_range, result)
            except Exception as e:
                write_status = f"Warning: Failed to write to {target_range}: {str(e)}"
            
        return {
            "status": "ok",
            "result": result,
            "write_status": write_status
        }
