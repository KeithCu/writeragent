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
"""In-process Python execution for Calc/Writer (LocalPythonExecutor sandbox).

Each tool call uses a fresh executor instance so variables do not leak across invocations.
"""

import logging
from typing import Any

from plugin.contrib.smolagents.local_python_executor import LocalPythonExecutor, InterpreterError
from plugin.framework.tool import ToolBaseDummy
from plugin.framework.errors import WriterAgentException
from plugin.calc.bridge import CalcBridge
from plugin.calc.manipulator import CellManipulator
from plugin.calc.inspector import CellInspector

logger = logging.getLogger("writeragent.calc.python_executor")

CALC_AUTHORIZED_IMPORTS = ["math", "datetime", "random", "json", "re", "collections", "itertools", "statistics"]


class PythonExecutor:
    """Runs Python in LO's embedded interpreter with document helpers (stdlib-only imports)."""

    def __init__(self, doc_url: str):
        self.doc_url = doc_url
        self.executor = LocalPythonExecutor(additional_authorized_imports=CALC_AUTHORIZED_IMPORTS)

    def inject_helpers(self, bridge: CalcBridge, manipulator: CellManipulator, inspector: CellInspector):
        """Injects document interaction helpers into the environment."""

        def lp_helper(addr: str) -> Any:
            """Read values from the spreadsheet. Generic for cells or ranges."""
            try:
                if ":" in addr:
                    data = inspector.read_range(addr)
                    return [[cell["value"] for cell in row] for row in data]
                sheet = bridge.get_active_sheet()
                return manipulator.safe_get_cell_value(sheet, addr)
            except Exception as e:
                logger.error("lp_helper error for address %s: %s", addr, e)
                return None

        def set_range_helper(addr: str, data: Any):
            """Write values back to the spreadsheet."""
            return manipulator.write_formula_range(addr, data)

        self.executor.send_variables({"lp": lp_helper, "Sheet": lp_helper, "get_range": lp_helper, "set_range": set_range_helper})

    def format_result(self, result: Any) -> Any:
        """Processes the result for storage and display."""
        if hasattr(result, "__dict__") and not isinstance(result, (list, tuple, dict, str, int, float, bool)):
            try:
                return f"<Result: {str(result)}>"
            except Exception:
                return f"<Object: {type(result).__name__}>"
        return result

    def execute_with_return(self, code_snippet: str) -> Any:
        """Execute *code_snippet*; return the last expression value (REPL-style ``_`` is not kept for the next call)."""
        try:
            code_output = self.executor(code_snippet)
            result = code_output.output
            return self.format_result(result)
        except InterpreterError as e:
            logger.exception("Sandbox execution error: \n%s", code_snippet)
            raise WriterAgentException(f"Execution Error: {str(e)}", code="PYTHON_EXECUTION_ERROR") from e
        except WriterAgentException:
            raise
        except Exception as e:
            logger.exception("Error executing Python code: \n%s", code_snippet)
            raise WriterAgentException(f"Execution Error: {str(e)}", code="PYTHON_EXECUTION_ERROR") from e


class ExecutePythonScript(ToolBaseDummy):
    """Executes Python in LibreOffice's sandbox (no numpy); fresh environment every call."""

    name = "execute_python_script"
    intent = "analyze"
    description = (
        "Executes a Python script in LibreOffice's embedded interpreter (stdlib sandbox, not the user venv). "
        "The value of the last expression is returned. Each call starts with a clean environment. "
        "Helpers: lp('A1:B10') reads range, set_range('C1', data) writes result."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "The Python code to execute."},
            "data_range": {"type": "string", "description": "Optional cell range; values are injected as data (flat list for one row or column)."},
            "target_range": {"type": "string", "description": "Optional: Cell range (e.g. 'A1') to write the script result to."},
        },
        "required": ["code"],
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument", "com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        code = kwargs.get("code", "")
        data_range = kwargs.get("data_range")
        target_range = kwargs.get("target_range")

        doc_url = ctx.doc.getURL() or "untitled"
        executor = PythonExecutor(doc_url)

        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        inspector = CellInspector(bridge)
        executor.inject_helpers(bridge, manipulator, inspector)

        # Inject data if data_range is provided
        if data_range:
            try:
                raw_data = inspector.read_range(data_range)
                py_data = [[cell["value"] for cell in row] for row in raw_data]
                if len(py_data) == 1:
                    py_data = py_data[0]
                elif all(len(row) == 1 for row in py_data):
                    py_data = [row[0] for row in py_data]
                executor.executor.send_variables({"data": py_data})
            except Exception as e:
                logger.error("Failed to inject data_range %s: %s", data_range, e)

        result = executor.execute_with_return(code)

        write_status = ""
        if target_range and result is not None:
            try:
                write_status = manipulator.write_formula_range(target_range, result)
            except Exception as e:
                write_status = f"Warning: Failed to write to {target_range}: {str(e)}"

        return {"status": "ok", "result": result, "write_status": write_status}
