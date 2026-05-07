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

import logging
from typing import Any

from plugin.contrib.smolagents.local_python_executor import LocalPythonExecutor, InterpreterError
from plugin.framework.tool import ToolBaseDummy
from plugin.framework.errors import WriterAgentException
from plugin.modules.calc.bridge import CalcBridge
from plugin.modules.calc.manipulator import CellManipulator
from plugin.modules.calc.inspector import CellInspector

logger = logging.getLogger("writeragent.calc.python_executor")

CALC_AUTHORIZED_IMPORTS = ["math", "datetime", "random", "json", "re", "collections", "itertools", "statistics"]


class PythonExecutor:
    """Handles the execution of Python code with document awareness locally in a secure sandbox."""

    def __init__(self, doc_url: str):
        self.doc_url = doc_url
        self.executor = LocalPythonExecutor(additional_authorized_imports=CALC_AUTHORIZED_IMPORTS)

    def reset(self):
        """Resets the environment to its initial state."""
        self.executor = LocalPythonExecutor(additional_authorized_imports=CALC_AUTHORIZED_IMPORTS)

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
        def set_range_helper(addr: str, data: Any):
            """Write values back to the spreadsheet."""
            return manipulator.write_formula_range(addr, data)

        self.executor.send_variables({"lp": lp_helper, "Sheet": lp_helper, "get_range": lp_helper, "set_range": set_range_helper})

    def format_result(self, result: Any) -> Any:
        """Processes the result for storage and display."""
        # Handle 1D/2D lists specifically for Calc
        if isinstance(result, (list, tuple)):
            if result and not isinstance(result[0], (list, tuple)):
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
        Executes the given code snippet using the AST sandbox wrapper.
        The executor natively passes back the value of the final evaluated expression.
        """
        try:
            code_output = self.executor(code_snippet)
            result = code_output.output

            # Store the result in '_' just like a REPL
            if result is not None:
                self.executor.send_variables({"_": result})

            return self.format_result(result)

        except InterpreterError as e:
            logger.exception("Sandbox execution error: \n%s", code_snippet)
            raise WriterAgentException(f"Execution Error: {str(e)}", code="PYTHON_EXECUTION_ERROR")
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


class ExecutePythonScript(ToolBaseDummy):
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
            "script": {"type": "string", "description": "The Python code to execute."},
            "target_range": {"type": "string", "description": "Optional: Cell range (e.g. 'A1') to write the script result to."},
            "reset": {"type": "boolean", "description": "If true, resets the Python environment for this document before executing.", "default": False},
        },
        "required": ["script"],
    }
    # Available in Calc and Writer
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument", "com.sun.star.text.TextDocument"]
    is_mutation = True  # Set to True because it can use set_range!

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

        return {"status": "ok", "result": result, "write_status": write_status}
