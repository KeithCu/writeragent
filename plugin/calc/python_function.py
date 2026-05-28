# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""=PYTHON() execution and return helpers (venv worker); no LLM imports."""

from __future__ import annotations

import logging
import math
import os
import tempfile
import threading
from typing import Any, cast

from plugin.calc.calc_addin_data import (
    calc_addin_args_from_split,
    check_python_data_size,
    check_python_multi_data_size,
    count_cells,
    pack_calc_data_for_wire,
    pack_calc_multi_data_for_wire,
    split_python_addin_data_args,
)
from plugin.framework.errors import format_error_payload
from plugin.framework.i18n import _
from plugin.scripting.config_limits import configured_python_max_data_cells
from plugin.scripting.payload_codec import is_image_payload, is_split_grid
from plugin.scripting.venv_worker import run_code_in_user_venv

log = logging.getLogger(__name__)

# Calc legacy add-in bridge accepts scalar double/string returns only. List results are
# emitted one scalar per formula evaluation (matrix block or repeated recalc).
MATRIX_SCALAR_SESSIONS = threading.local()


def flatten_result_values(result: Any) -> list:
    """Row-major flattening for list / nested list worker results."""
    if not isinstance(result, (list, tuple)):
        return [result]
    if not result:
        return []
    if isinstance(result[0], (list, tuple)):
        flat: list = []
        for row in result:
            flat.extend(row)
        return flat
    return list(result)


def is_scalar_index_arg(py_data: list | list[list] | None) -> bool:
    """True when arg 1 is one number (matrix index), not a data range."""
    if py_data is None:
        return False
    if count_cells(py_data) != 1:
        return False
    first = py_data[0]
    return not isinstance(first, (list, tuple))


# Tests and legacy imports
_is_scalar_index_arg = is_scalar_index_arg


def coerce_index(value: Any) -> int:
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


def to_calc_compatible(val: Any) -> float | str | bool | tuple:
    """Recursively convert Python values into LibreOffice Calc supported types.

    Calc cells only support float (UNO double), str (UNO string), and bool (UNO boolean).
    Crucially, Calc matrix formulas do NOT support integer (UNO long) types and will
    throw #VALUE! if a sequence contains integers/longs.
    """
    if val is None:
        return ""
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return float(val)
    if isinstance(val, float):
        # NaN from Python/NumPy egress must become an empty cell, not a raw double (#NUM! / #VALUE!).
        if math.isnan(val):
            return ""
        return val
    if isinstance(val, str):
        return val
    if isinstance(val, (list, tuple)):
        inner = val[0] if val else None
        if isinstance(inner, (list, tuple)):
            return tuple(tuple(to_calc_compatible(cell) for cell in row) for row in val)
        return tuple(to_calc_compatible(item) for item in val)
    return str(val)


def session_key(ctx: Any, code: str) -> tuple:
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


class WorkerResultSession:
    """Caches one worker list result across multiple =PYTHON() calls in a recalc pass."""

    __slots__ = ("raw", "flat", "next_index")

    def __init__(self, raw: Any, flat: list) -> None:
        self.raw = raw
        self.flat = tuple(flat)
        self.next_index = 0


# Legacy alias for tests
_WorkerResultSession = WorkerResultSession


def scalar_for_list_result(ctx: Any, code: str, result: Any, *, worker_data: Any = None) -> float | str | bool:
    """Return one Calc scalar per invocation when the worker produced a list."""
    flat: list = [to_calc_compatible(v) for v in flatten_result_values(result)]
    if not flat:
        return ""
    key = (session_key(ctx, code), repr(worker_data))
    sessions = getattr(MATRIX_SCALAR_SESSIONS, "sessions", None)
    if sessions is None:
        sessions = {}
        MATRIX_SCALAR_SESSIONS.sessions = sessions
    state = sessions.get(key)
    if not isinstance(state, WorkerResultSession) or state.flat != tuple(flat):
        state = WorkerResultSession(result, flat)
        sessions[key] = state
    idx = state.next_index
    state.next_index = idx + 1
    if state.next_index >= len(state.flat):
        sessions.pop(key, None)
    if 0 <= idx < len(state.flat):
        return state.flat[idx]
    return state.flat[-1] if state.flat else ""


def finalize_python_return(
    ctx: Any,
    code: str,
    result: Any,
    *,
    index_arg: Any = None,
    worker_data: Any = None,
) -> float | str | bool | tuple:
    """Map worker result to a single value Calc's add-in bridge accepts."""
    if isinstance(result, (list, tuple)):
        if index_arg is not None:
            flat = flatten_result_values(result)
            idx = coerce_index(index_arg)
            if idx < 0 or idx >= len(flat):
                return f"Error: index {idx} out of range (result length {len(flat)})"
            return to_calc_compatible(flat[idx])
        return scalar_for_list_result(ctx, code, result, worker_data=worker_data)
    return to_calc_compatible(result)


def _insert_image_result_on_sheet(ctx: Any, payload: dict[str, Any]) -> None:
    """Write image payload bytes to a temp file and insert as a cell-anchored shape on the active sheet."""
    import uno
    from com.sun.star.awt import Size

    img_bytes = payload["data"]
    fmt = payload.get("format", "png")
    suffix = ".svg" if fmt == "svg" else ".png"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(img_bytes)
        tmp_path = tmp.name

    file_url = uno.systemPathToFileUrl(os.path.abspath(tmp_path))
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    doc = desktop.getCurrentComponent()
    ctrl = doc.getCurrentController()
    sheet = ctrl.getActiveSheet()
    draw_page = sheet.DrawPage

    shape = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
    shape.setSize(Size(15000, 10000))
    draw_page.add(shape)
    shape.setPropertyValue("GraphicURL", file_url)

    # Anchor the image to the active cell so it moves/scales with the grid.
    try:
        from plugin.calc.calc_utils import get_cell_geometry
        selection = ctrl.getSelection()
        if selection is not None:
            addr = selection.getRangeAddress()
            cell = sheet.getCellByPosition(addr.StartColumn, addr.StartRow)
            # Bugfix: merged cells report sub-cell geometry via raw cell.Position/Size.
            # Use calc_utils merged-aware geometry so overlays land on the full merged area.
            cell_pos, cell_size = get_cell_geometry(sheet, cell)
            shape.setPropertyValue("Anchor", cell)
            shape.setPropertyValue("ResizeWithCell", True)
            if hasattr(shape, "setPosition"):
                shape.setPosition(cell_pos)
            if hasattr(shape, "setSize"):
                shape.setSize(cell_size)
    except Exception:
        log.debug("_insert_image_result_on_sheet: could not anchor to cell", exc_info=True)


def _format_error_for_display(exc: BaseException) -> str:
    """Cell-safe error text without importing ``plugin.framework.client`` (loads LLM stack)."""
    err: Exception = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
    payload = format_error_payload(err)
    return _("Error: {0}").format(payload.get("message", str(exc)))


def execute_python_addin(
    ctx: Any,
    code: str,
    data: Any = None,
    true_strings: set[str] | None = None,
    false_strings: set[str] | None = None,
) -> Any:
    """Run *code* in the user venv and return a Calc-compatible scalar (or error string)."""
    log.debug("=== PYTHON(%r, data=%r) ===", code, data)
    try:
        args = split_python_addin_data_args(data)
        py_data = calc_addin_args_from_split(args, true_strings, false_strings)
        log.debug("PYTHON parsed py_data: %r", py_data)
        is_multi = len(args) > 1
        index_arg = None
        if py_data is not None and not is_multi and is_scalar_index_arg(py_data) and not is_split_grid(py_data):
            index_arg = py_data[0]
        max_cells = configured_python_max_data_cells(ctx)
        if py_data is not None:
            if is_multi:
                size_err = check_python_multi_data_size(py_data, max_cells=max_cells)
            else:
                size_err = check_python_data_size(py_data, max_cells=max_cells)
            if size_err:
                ret = f"Error: {size_err}"
                log.debug("PYTHON returning size error: %r", ret)
                return ret
            worker_data = pack_calc_multi_data_for_wire(py_data) if is_multi else pack_calc_data_for_wire(py_data)
        else:
            worker_data = None
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
            result = res.get("result")
            log.debug("PYTHON raw result: %r (type: %s)", result, type(result).__name__)
            if is_image_payload(result):
                _insert_image_result_on_sheet(ctx, cast("dict[str, Any]", result))
                return _("Image inserted")
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
