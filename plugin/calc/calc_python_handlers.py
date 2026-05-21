# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""=PYTHON() execution handler (venv worker); no LLM imports."""

from __future__ import annotations

import logging
from typing import Any

from plugin.calc.calc_addin_data import calc_addin_data_to_python, check_python_data_size, pack_calc_data_for_wire
from plugin.calc.calc_python_helpers import (
    MATRIX_SCALAR_SESSIONS,
    WorkerResultSession,
    finalize_python_return,
    is_scalar_index_arg,
    session_key,
    worker_result_for_calc,
)
from plugin.framework.errors import format_error_payload
from plugin.framework.i18n import _
from plugin.scripting.payload_codec import is_split_grid
from plugin.scripting.run_venv_code import run_code_in_user_venv

log = logging.getLogger(__name__)


def _format_error_for_display(exc: BaseException) -> str:
    """Cell-safe error text without importing ``plugin.framework.client`` (loads LLM stack)."""
    payload = format_error_payload(exc)
    return _("Error: {0}").format(payload.get("message", str(exc)))


def execute_python_addin(ctx: Any, code: str, data: Any = None) -> Any:
    """Run *code* in the user venv and return a Calc-compatible scalar (or error string)."""
    log.debug("=== PYTHON(%r, data=%r) ===", code, data)
    try:
        py_data = calc_addin_data_to_python(data)
        log.debug("PYTHON parsed py_data: %r", py_data)
        index_arg = None
        if py_data is not None and is_scalar_index_arg(py_data) and not is_split_grid(py_data):
            index_arg = py_data[0]
        if py_data is not None:
            size_err = check_python_data_size(py_data)
            if size_err:
                ret = f"Error: {size_err}"
                log.debug("PYTHON returning size error: %r", ret)
                return ret
            py_data = pack_calc_data_for_wire(py_data)
        worker_data = py_data
        # Synchronous: =PYTHON() runs during Calc recalc; UI event pumping from
        # run_blocking_in_thread can re-enter the formula engine and yield #VALUE!.
        sessions = getattr(MATRIX_SCALAR_SESSIONS, "sessions", None)
        if sessions is None:
            sessions = {}
            MATRIX_SCALAR_SESSIONS.sessions = sessions
        cache_key = (session_key(ctx, code), repr(worker_data))
        cached = sessions.get(cache_key)
        if isinstance(cached, WorkerResultSession) and cached.next_index < len(cached.flat):
            res = {"status": "ok", "result": cached.raw}
        else:
            res = run_code_in_user_venv(ctx, code, data=worker_data)
        log.debug("PYTHON res from worker: %r", res)
        if res.get("status") == "ok":
            result = worker_result_for_calc(res.get("result"))
            log.debug("PYTHON raw result: %r (type: %s)", result, type(result).__name__)
            final_ret = finalize_python_return(ctx, code, result, index_arg=index_arg, worker_data=worker_data)
            log.debug("PYTHON returning scalar: %r (type: %s)", final_ret, type(final_ret).__name__)
            return final_ret
        err_msg = f"Error: {res.get('message') or res.get('error')}"
        log.debug("PYTHON returning worker error: %r", err_msg)
        return err_msg
    except Exception as e:
        log.exception("PYTHON unexpected error during execution")
        err_msg = _format_error_for_display(e)
        log.debug("PYTHON returning exception wrapper: %r", err_msg)
        return err_msg
