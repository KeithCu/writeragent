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

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.contrib.smolagents.local_python_executor import ExecutionTimeoutError, InterpreterError, LocalPythonExecutor
from plugin.framework.tool import ToolBaseDummy

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.framework.errors import CalcError, ToolExecutionError, WriterAgentException, is_disposed_exception
from plugin.scripting.payload_codec import wire_str_key
from plugin.scripting.import_policy import format_inprocess_import_policy_for_prompt
from plugin.scripting.sandbox import CALC_AUTHORIZED_IMPORTS
from plugin.calc.bridge import CalcBridge
from plugin.calc.manipulator import CellManipulator
from plugin.calc.inspector import CellInspector

log = logging.getLogger("writeragent.calc.python.executor")

_SPREADSHEET_SERVICE = "com.sun.star.sheet.SpreadsheetDocument"
_CALC_HELPER_ERROR = "lp, get_range, set_range, and data_range only work in a Calc spreadsheet."


def _is_spreadsheet(doc: Any) -> bool:
    """True when *doc* is a Calc model.

    Non-bool supportsService returns False (unconfigured mocks do not count
    silently as spreadsheets).
    """
    supports = getattr(doc, "supportsService", None)
    if not callable(supports):
        return True
    try:
        result = supports(_SPREADSHEET_SERVICE)
    except Exception:
        log.debug("spreadsheet service check failed", exc_info=True)
        return False
    if not isinstance(result, bool):
        return False
    return result


class PythonExecutor:
    """Runs Python in LO's embedded interpreter with document helpers (stdlib-only imports)."""

    executor: LocalPythonExecutor

    def __init__(self) -> None:
        self.executor = LocalPythonExecutor(additional_authorized_imports=list(CALC_AUTHORIZED_IMPORTS))
        # static_tools started as None, evaluate_python_code substituted {},
        # and every builtin call raised Forbidden function evaluation.
        # send_tools is what merges BASE_PYTHON_TOOLS.
        self.executor.send_tools({})

    def inject_helpers(self, manipulator: CellManipulator, inspector: CellInspector, *, spreadsheet: bool = True) -> None:
        """Injects document interaction helpers into the environment."""

        def _require_calc(helper: str) -> None:
            if spreadsheet:
                return
            # Fail the call on a non-spreadsheet. These helpers used to be
            # injected on Writer too, and lp_helper caught the RuntimeError
            # and returned None, so the tool reported success.
            raise WriterAgentException(f"{helper}: {_CALC_HELPER_ERROR}", code="UNSUPPORTED_OPERATION")

        def lp_helper(addr: str) -> Any:
            """Read values from the spreadsheet. Generic for cells or ranges."""
            _require_calc("lp")
            try:
                data = inspector.read_range(addr)
                if ":" in addr:
                    return [[cell["value"] for cell in row] for row in data]
                if data and data[0]:
                    return data[0][0]["value"]
                return None
            except Exception as exc:
                # ExecutionTimeoutError is a BaseException and must not be wrapped:
                # catching it here disarms SIGALRM, and a loop of lp() never returns.
                # WriterAgentException (_require_calc) and a disposed document must
                # keep their type. Wrapping them as CALC_READ_ERROR hid disposal and
                # relabeled UNSUPPORTED_OPERATION.
                if isinstance(exc, WriterAgentException) or is_disposed_exception(exc):
                    raise
                log.exception("lp_helper failed for address %s", addr)
                raise WriterAgentException(
                    f"lp failed to read address '{addr}': {exc}", code="CALC_READ_ERROR"
                ) from exc

        def set_range_helper(addr: str, data: Any) -> str | dict[str, Any]:
            """Write values back to the spreadsheet."""
            _require_calc("set_range")
            return manipulator.write_formula_range(addr, data)

        self.executor.send_variables({"lp": lp_helper, "Sheet": lp_helper, "get_range": lp_helper, "set_range": set_range_helper})

    def inject_data(self, data: Any) -> None:
        """Bind the tool's ``data_range`` values as ``data`` in this executor."""
        self.executor.send_variables({"data": data})

    # Tuples become lists, and containers are deeply walked. Any leaf json.dumps cannot accept is stringified.
    def _normalize_value(self, val: Any, seen: set[int]) -> Any:
        if val is None or isinstance(val, (str, int, float, bool)):
            return val
        val_id = id(val)
        if val_id in seen:
            return f"<Cycle: {type(val).__name__}>"
        if isinstance(val, (list, tuple)):
            seen.add(val_id)
            try:
                return [self._normalize_value(item, seen) for item in val]
            finally:
                seen.remove(val_id)
        if isinstance(val, dict):
            seen.add(val_id)
            try:
                # Dict keys that are not strings: stringify the key. json.dumps rejects non-string keys.
                # {1: "a", "1": "b"} used to keep only "b" because both keys became "1".
                # wire_str_key raises instead of dropping a value.
                used: set[str] = set()
                out: dict[str, Any] = {}
                for k, v in val.items():
                    out[wire_str_key(k, used)] = self._normalize_value(v, seen)
                return out
            finally:
                seen.remove(val_id)
        # Deep-walk containers; convert tuples to lists and stringify leaves that json.dumps cannot take.
        try:
            return f"<Result: {str(val)}>"
        except Exception:
            return f"<Object: {type(val).__name__}>"

    def format_result(self, result: Any) -> Any:
        """Processes the result for storage and display, deep-walking containers."""
        if result is None:
            return None
        return self._normalize_value(result, set())

    def _captured_stdout(self) -> str:
        state = getattr(self.executor, "state", None)
        if not isinstance(state, dict):
            return ""
        return str(state.get("_print_outputs") or "")

    def last_stdout(self) -> str:
        return self._captured_stdout()

    def _execution_error(self, text: str) -> WriterAgentException:
        logs = self._captured_stdout()
        msg = f"Execution Error: {text}"
        if logs.strip():
            msg = f"{msg}\nstdout:\n{logs}"
        return WriterAgentException(msg, code="PYTHON_EXECUTION_ERROR")

    def execute_with_return(self, code_snippet: str) -> Any:
        """Execute *code_snippet*; return the last expression value (REPL-style ``_`` is not kept for the next call)."""
        try:
            code_output = self.executor(code_snippet)
            result = code_output.output
            return self.format_result(result)
        except InterpreterError as e:
            log.warning("Sandbox execution error: %s (snippet length %d)", str(e), len(code_snippet))
            raise self._execution_error(str(e)) from e
        except WriterAgentException:
            raise
        except Exception as e:
            if is_disposed_exception(e):
                raise
            log.exception("Error executing Python code (snippet length %d)", len(code_snippet))
            raise self._execution_error(str(e)) from e
        except ExecutionTimeoutError as e:
            # ExecutionTimeoutError is a BaseException, so it bypasses `except Exception`.
            # Catch it here so the timeout does not escape unhandled into LibreOffice.
            log.exception("Sandbox execution timeout: \n%s", code_snippet)
            raise self._execution_error(str(e)) from e
        except (SystemExit, KeyboardInterrupt) as e:
            # User code can raise SystemExit or KeyboardInterrupt (BaseExceptions).
            # Convert them to WriterAgentException naming the exception type; never catch GeneratorExit.
            log.exception("Sandbox execution stopped by %s: \n%s", type(e).__name__, code_snippet)
            msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            raise self._execution_error(msg) from e



class ExecutePythonScript(ToolBaseDummy):
    """Executes Python in LibreOffice's sandbox (no numpy); fresh environment every call."""

    name: str | None = "execute_python_script"
    intent: str | None = "analyze"
    description: str = format_inprocess_import_policy_for_prompt() + " The value of the last expression is returned. Each call starts with a clean environment."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "The Python code to execute."},
            "data_range": {"type": "string", "description": "Optional cell range; values are injected as data (flat list for one row or column)."},
            "target_range": {"type": "string", "description": "Optional: Cell range (e.g. 'A1') to write the script result to."},
        },
        "required": ["code"],
    }
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument", "com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        code = kwargs.get("code", "")
        data_range = kwargs.get("data_range")
        target_range = kwargs.get("target_range")

        spreadsheet = _is_spreadsheet(ctx.doc)
        if not spreadsheet and (data_range or target_range):
            raise WriterAgentException(_CALC_HELPER_ERROR, code="UNSUPPORTED_OPERATION")
        executor = PythonExecutor()

        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        inspector = CellInspector(bridge)
        executor.inject_helpers(manipulator, inspector, spreadsheet=spreadsheet)

        # Inject data if data_range is provided
        if data_range:
            try:
                raw_data = inspector.read_range(data_range)
                py_data = [[cell["value"] for cell in row] for row in raw_data]
                if len(py_data) == 1:
                    py_data = py_data[0]
                elif all(len(row) == 1 for row in py_data):
                    py_data = [row[0] for row in py_data]
                executor.inject_data(py_data)
            except Exception as exc:
                # CellInspector.read_range wraps a failed read as ToolExecutionError.
                # That class subclasses WriterAgentException, so this check used to
                # re-raise it and the CALC_READ_ERROR return below never ran for the
                # exception production reads actually raise. CalcError is the same
                # kind of typed failure. Map both onto the tool error. A disposed
                # document and any other WriterAgentException still propagate.
                if is_disposed_exception(exc) or (
                    isinstance(exc, WriterAgentException) and not isinstance(exc, (CalcError, ToolExecutionError))
                ):
                    raise
                log.exception("Failed to inject data_range %s", data_range)
                return self._tool_error(f"Failed to read data_range {data_range}: {exc}", code="CALC_READ_ERROR")

        result = executor.execute_with_return(code)
        stdout = executor.last_stdout()

        # write_formula_range returns a summary string or an array-formula dict.
        write_status: str | dict[str, Any] = ""
        if target_range and result is not None:
            try:
                write_status = manipulator.write_formula_range(target_range, result)
            except Exception as exc:
                # CellManipulator.write_formula_range re-raises CalcError and wraps
                # other write failures as CalcError. CalcError subclasses
                # WriterAgentException, so this check used to re-raise it and the
                # CALC_WRITE_ERROR return below never ran. The script result and
                # stdout, already computed above, were dropped with the exception.
                # Map CalcError and ToolExecutionError onto that return so the
                # error result keeps them. A disposed document and any other
                # WriterAgentException still propagate.
                if is_disposed_exception(exc) or (
                    isinstance(exc, WriterAgentException) and not isinstance(exc, (CalcError, ToolExecutionError))
                ):
                    raise
                log.exception("Failed to write to %s", target_range)
                return self._tool_error(
                    f"Failed to write to {target_range}: {exc}",
                    code="CALC_WRITE_ERROR",
                    result=result,
                    stdout=stdout,
                )

        return {"status": "ok", "result": result, "stdout": stdout, "write_status": write_status}
