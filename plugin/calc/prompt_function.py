# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
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
import os
import sys
import threading

# Ensure the extension's install directory is on sys.path
# so that "plugin.xxx" imports work correctly.
# We must do this before any imports from "plugin.xyz"
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_ext_root = os.path.dirname(os.path.dirname(_plugin_dir))
if _ext_root not in sys.path:
    sys.path.insert(0, _ext_root)
if os.path.dirname(_plugin_dir) not in sys.path:
    sys.path.insert(0, os.path.dirname(_plugin_dir))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)


import unohelper


# from com.sun.star.lang import XServiceInfo
# from com.sun.star.sheet import XAddIn
try:
    from org.extension.writeragent.PromptFunction import (  # type: ignore
        XPromptFunction as _XPromptFunctionBase,
    )
except ImportError:

    class _XPromptFunctionStub(unohelper.Base):
        pass

    _XPromptFunctionBase = _XPromptFunctionStub

from plugin.framework.config import get_config, get_api_config, get_config_int
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.async_stream import run_blocking_in_thread
from plugin.framework.client.errors import format_error_for_display
from plugin.calc.calc_addin_data import calc_addin_data_to_python, check_python_data_size, count_cells
from plugin.scripting.run_venv_code import run_code_in_user_venv

import logging

log = logging.getLogger(__name__)

# Calc legacy add-in bridge accepts scalar double/string returns only. List results are
# emitted one scalar per formula evaluation (matrix block or repeated recalc).
_MATRIX_SCALAR_SESSIONS = threading.local()


def _flatten_result_values(result):
    """Row-major flattening for list / nested list worker results."""
    if not isinstance(result, (list, tuple)):
        return [result]
    if not result:
        return []
    if isinstance(result[0], (list, tuple)):
        flat = []
        for row in result:
            flat.extend(row)
        return flat
    return list(result)


def _is_scalar_index_arg(py_data: list | list[list] | None) -> bool:
    """True when arg 1 is one number (matrix index), not a data range."""
    if py_data is None:
        return False
    if count_cells(py_data) != 1:
        return False
    first = py_data[0]
    return not isinstance(first, (list, tuple))


def _coerce_index(value) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        return int(float(value))
    raise ValueError(f"index must be numeric, got {value!r}")


def to_calc_compatible(val):
    """Recursively convert Python values into LibreOffice Calc supported types.

    Calc cells only support float (UNO double), str (UNO string), and bool (UNO boolean).
    Crucially, Calc matrix formulas do NOT support integer (UNO long) types and will
    throw #VALUE! if a sequence contains integers/longs.
    """
    if val is None:
        return ""
    if isinstance(val, bool):
        return val
    # Check bool before int, as isinstance(True, int) is True
    if isinstance(val, int):
        return float(val)
    if isinstance(val, float):
        return val
    if isinstance(val, str):
        return val
    if isinstance(val, (list, tuple)):
        inner = val[0] if val else None
        if isinstance(inner, (list, tuple)):
            return tuple(tuple(to_calc_compatible(cell) for cell in row) for row in val)
        return tuple(to_calc_compatible(item) for item in val)
    return str(val)


def _session_key(ctx, code: str):
    doc_url = ""
    sheet_name = ""
    try:
        smgr = ctx.ServiceManager
        desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        doc = desktop.getCurrentComponent()
        if doc is not None:
            doc_url = getattr(doc, "getURL", lambda: "")() or ""
            ctrl = getattr(doc, "getCurrentController", lambda: None)()
            if ctrl is not None:
                sheet = ctrl.getActiveSheet()
                if sheet is not None:
                    sheet_name = sheet.getName()
    except Exception:
        pass
    return (doc_url, sheet_name, code)


class _WorkerResultSession:
    """Caches one worker list result across multiple =PYTHON() calls in a recalc pass."""

    __slots__ = ("raw", "flat", "next_index")

    def __init__(self, raw, flat: list):
        self.raw = raw
        self.flat = tuple(flat)
        self.next_index = 0


def _scalar_for_list_result(ctx, code: str, result, *, worker_data=None) -> float | str | bool:
    """Return one Calc scalar per invocation when the worker produced a list."""
    flat: list = [to_calc_compatible(v) for v in _flatten_result_values(result)]
    if not flat:
        return ""
    key = (_session_key(ctx, code), repr(worker_data))
    sessions = getattr(_MATRIX_SCALAR_SESSIONS, "sessions", None)
    if sessions is None:
        sessions = {}
        _MATRIX_SCALAR_SESSIONS.sessions = sessions
    state = sessions.get(key)
    if not isinstance(state, _WorkerResultSession) or state.flat != tuple(flat):
        state = _WorkerResultSession(result, flat)
        sessions[key] = state
    idx = state.next_index
    state.next_index = idx + 1
    if state.next_index >= len(state.flat):
        sessions.pop(key, None)
    if 0 <= idx < len(state.flat):
        return state.flat[idx]
    return state.flat[-1] if state.flat else ""


def finalize_python_return(ctx, code: str, result, *, index_arg=None, worker_data=None):
    """Map worker result to a single value Calc's add-in bridge accepts."""
    if isinstance(result, (list, tuple)):
        if index_arg is not None:
            flat = _flatten_result_values(result)
            idx = _coerce_index(index_arg)
            if idx < 0 or idx >= len(flat):
                return f"Error: index {idx} out of range (result length {len(flat)})"
            return to_calc_compatible(flat[idx])
        return _scalar_for_list_result(ctx, code, result, worker_data=worker_data)
    return to_calc_compatible(result)


class PromptFunction(unohelper.Base, _XPromptFunctionBase):  # pyright: ignore[reportGeneralTypeIssues] — runtime IDL base from LO  # pyrefly: ignore[invalid-inheritance]
    def __init__(self, ctx):
        log.debug("=== PromptFunction.__init__ called ===")
        self.ctx = ctx
        self.client = None

    def getProgrammaticFunctionName(self, aDisplayName):
        log.debug(f"=== getProgrammaticFunctionName called with: '{aDisplayName}' ===")
        if aDisplayName == "PROMPT":
            return "prompt"
        if aDisplayName == "PYTHON":
            return "python"
        return ""

    def getDisplayFunctionName(self, aProgrammaticName):
        log.debug(f"=== getDisplayFunctionName called with: '{aProgrammaticName}' ===")
        if aProgrammaticName == "prompt":
            return "PROMPT"
        if aProgrammaticName == "python":
            return "PYTHON"
        return ""

    def getFunctionDescription(self, aProgrammaticName):
        if aProgrammaticName == "prompt":
            return "Generates text using an LLM."
        if aProgrammaticName == "python":
            return "Executes Python code in the configured venv and returns the result."
        return ""

    def getArgumentDescription(self, aProgrammaticName, nArgument):
        if aProgrammaticName == "prompt":
            if nArgument == 0:
                return "The prompt to send to the LLM."
            elif nArgument == 1:
                return "The system prompt to use."
            elif nArgument == 2:
                return "The model to use."
            elif nArgument == 3:
                return "The maximum number of tokens to generate."
        if aProgrammaticName == "python":
            if nArgument == 0:
                return "The Python code to execute. Assign output to 'result'."
            elif nArgument == 1:
                return (
                    "Optional range injected as data, or a single-cell index for matrix "
                    "formulas (e.g. ROW(A1)-ROW($A$1))."
                )
        return ""

    def getArgumentName(self, aProgrammaticName, nArgument):
        if aProgrammaticName == "prompt":
            if nArgument == 0:
                return "message"
            elif nArgument == 1:
                return "system_prompt"
            elif nArgument == 2:
                return "model"
            elif nArgument == 3:
                return "max_tokens"
        if aProgrammaticName == "python":
            if nArgument == 0:
                return "code"
            elif nArgument == 1:
                return "data"
        return ""

    def hasFunctionWizard(self, aProgrammaticName):
        return True

    def getArgumentCount(self, aProgrammaticName):
        if aProgrammaticName == "prompt":
            return 4
        if aProgrammaticName == "python":
            return 2
        return 0

    def getArgumentIsOptional(self, aProgrammaticName, nArgument):
        if aProgrammaticName == "prompt":
            return nArgument > 0
        if aProgrammaticName == "python":
            return nArgument == 1
        return False

    def getProgrammaticCategoryName(self, aProgrammaticName):
        return "Add-In"

    def getDisplayCategoryName(self, aProgrammaticName):
        return "Add-In"

    def getLocale(self):
        return self.ctx.ServiceManager.createInstance("com.sun.star.lang.Locale", ("en", "US", ""))

    def setLocale(self, locale):
        pass

    def load(self, xSomething):
        pass

    def unload(self):
        pass

    def prompt(self, message, systemPrompt, model, maxTokens):
        log.debug(f"=== PromptFunction.PROMPT({message}) called ===")
        try:
            system_prompt = systemPrompt if systemPrompt is not None else get_config(self.ctx, "extend_selection_system_prompt")
            model_name = model if model is not None else (get_config(self.ctx, "text_model") or get_config(self.ctx, "model") or "")
            if maxTokens is not None:
                try:
                    max_tokens = int(maxTokens)
                except (TypeError, ValueError):
                    max_tokens = 70
            else:
                max_tokens = get_config_int(self.ctx, "calc_prompt_max_tokens")

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": message})

            config = get_api_config(self.ctx)
            if model is not None:
                config = dict(config, model=str(model_name))

            if not self.client:
                self.client = LlmClient(config, self.ctx)
            else:
                self.client.config = config

            return run_blocking_in_thread(self.ctx, self.client.chat_completion_sync, messages, max_tokens=max_tokens)
        except Exception as e:
            log.error("PROMPT error: %s" % str(e))
            return format_error_for_display(e)

    def python(self, code, data=None):
        log.debug("=== PromptFunction.PYTHON(%r, data=%r) called ===", code, data)
        try:
            py_data = calc_addin_data_to_python(data)
            log.debug("PYTHON parsed py_data: %r", py_data)
            index_arg = None
            worker_data = py_data
            if py_data is not None and _is_scalar_index_arg(py_data):
                index_arg = py_data[0]
                # Keep worker_data = py_data instead of None so single cells are still passed as 'data' list
            elif py_data is not None:
                size_err = check_python_data_size(py_data)
                if size_err:
                    ret = f"Error: {size_err}"
                    log.debug("PYTHON returning size error: %r", ret)
                    return ret
            # Synchronous: =PYTHON() runs during Calc recalc; UI event pumping from
            # run_blocking_in_thread can re-enter the formula engine and yield #VALUE!.
            sessions = getattr(_MATRIX_SCALAR_SESSIONS, "sessions", None)
            if sessions is None:
                sessions = {}
                _MATRIX_SCALAR_SESSIONS.sessions = sessions
            cache_key = (_session_key(self.ctx, code), repr(worker_data))
            cached = sessions.get(cache_key)
            if isinstance(cached, _WorkerResultSession) and cached.next_index < len(cached.flat):
                res = {"status": "ok", "result": cached.raw}
            else:
                res = run_code_in_user_venv(self.ctx, code, data=worker_data)
            log.debug("PYTHON res from worker: %r", res)
            if res.get("status") == "ok":
                result = res.get("result")
                log.debug("PYTHON raw result: %r (type: %s)", result, type(result).__name__)
                final_ret = finalize_python_return(self.ctx, code, result, index_arg=index_arg, worker_data=worker_data)
                log.debug("PYTHON returning scalar: %r (type: %s)", final_ret, type(final_ret).__name__)
                return final_ret
            else:
                err_msg = f"Error: {res.get('message') or res.get('error')}"
                log.debug("PYTHON returning worker error: %r", err_msg)
                return err_msg
        except Exception as e:
            log.exception("PYTHON unexpected error during execution")
            err_msg = format_error_for_display(e)
            log.debug("PYTHON returning exception wrapper: %r", err_msg)
            return err_msg

    # XServiceInfo implementation
    def getImplementationName(self):
        return "org.extension.writeragent.PromptFunction"

    def supportsService(self, name):
        return name in self.getSupportedServiceNames()

    def getSupportedServiceNames(self):
        return ("com.sun.star.sheet.AddIn",)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(PromptFunction, "org.extension.writeragent.PromptFunction", ("com.sun.star.sheet.AddIn",))
