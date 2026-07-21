# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""=PY() execution and return helpers (venv worker); no LLM imports."""

from __future__ import annotations

import logging
import math
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
from plugin.calc.python.image_egress import insert_image_result_on_sheet
from plugin.framework.errors import format_error_payload
from plugin.framework.i18n import _
from plugin.scripting.config_limits import configured_python_max_data_cells
from plugin.scripting.payload_codec import is_dataframe_payload, is_split_grid, find_image_payloads
# Optional: reset worker init/cell sessions on workbook close (see python_workbook_lifecycle.py).
# from plugin.calc.python.workbook_lifecycle import ensure_calc_workbook_unload_resets_python
from plugin.scripting.document_scripts import build_python_eval_init_kwargs, get_calc_document_from_ctx
from plugin.scripting.session_manager import workbook_session_id
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


def _strip_dataframe_envelope(result: Any) -> Any:
    """If *result* is a dataframe payload envelope, return its inner 'data' grid for Calc consumers.
    This lets =PY() matrix/session/index paths treat DF results like ordinary rectangular lists
    (the columns are available for Writer/chat paths via the envelope)."""
    if is_dataframe_payload(result):
        return result.get("data")
    return result


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
        # Computed NaN (or NaN from a numeric grid that contained blanks) is returned as-is.
        # The Calc add-in bridge renders a raw NaN double as a cascading error (#NUM! or #VALUE!).
        # Python None is mapped to "" (empty cell). We intentionally do NOT collapse NaN here.
        if math.isnan(val):
            return val
        return val
    if isinstance(val, str):
        return val
    if isinstance(val, (list, tuple)):
        inner = val[0] if val else None
        if isinstance(inner, (list, tuple)):
            return tuple(tuple(to_calc_compatible(cell) for cell in row) for row in val)
        return tuple(to_calc_compatible(item) for item in val)
    return str(val)


def _get_calc_doc(ctx: Any) -> Any | None:
    try:
        from plugin.framework.uno_context import get_desktop
        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent()
        if doc is not None and hasattr(doc, "getSheets"):
            return doc
        comps = desktop.getComponents()
        if comps:
            enum = comps.createEnumeration()
            while enum and enum.hasMoreElements():
                elem = enum.nextElement()
                model = None
                if hasattr(elem, "getURL") and callable(getattr(elem, "getURL")):
                    model = elem
                elif hasattr(elem, "getController") and elem.getController():
                    model = elem.getController().getModel()
                if model and hasattr(model, "getSheets"):
                    return model
    except Exception:
        pass
    return None


def session_key(ctx: Any, code: str) -> tuple:
    from plugin.framework.thread_guard import on_main_thread
    from plugin.framework.queue_executor import execute_on_main_thread

    def _session_key_impl() -> tuple:
        doc_url = ""
        sheet_name = ""
        try:
            if not (hasattr(ctx, "ServiceManager") or hasattr(ctx, "getServiceManager")):
                return (doc_url, sheet_name, code)
            doc = _get_calc_doc(ctx)
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

    if not on_main_thread():
        return execute_on_main_thread(_session_key_impl)

    return _session_key_impl()


class WorkerResultSession:
    """Caches one worker list result across multiple =PY() calls in a recalc pass."""

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


# The spill registry tracks coordinates that were spilled by each formula cell.
# Key: (doc_url, sheet_name, formula_row, formula_col)
# Value: list of (spilled_row, spilled_col) coordinates
SPILL_REGISTRY: dict[tuple[str, str, int, int], list[tuple[int, int]]] = {}
LOADED_DOCUMENTS: set[str] = set()


def load_spill_registry_for_doc(doc: Any) -> None:
    """Load the document's spill registry from its UserDefinedProperties."""
    try:
        from plugin.doc.document_helpers import get_document_property
        import json
        raw = get_document_property(doc, "WriterAgentSpillRegistry", None)
        if raw:
            data = json.loads(raw)
            doc_url = getattr(doc, "getURL", lambda: "")() or ""
            for key, value in data.items():
                parts = key.split(":")
                if len(parts) == 2:
                    sheet_name, coords = parts
                    row_col = coords.split(",")
                    if len(row_col) == 2:
                        frow, fcol = int(row_col[0]), int(row_col[1])
                        spill_coords = [(int(r), int(c)) for r, c in value]
                        SPILL_REGISTRY[(doc_url, sheet_name, frow, fcol)] = spill_coords
    except Exception:
        log.exception("Failed to load spill registry from document property")


def save_spill_registry_for_doc(doc: Any) -> None:
    """Save the document's spill registry to its UserDefinedProperties."""
    try:
        from plugin.doc.document_helpers import set_document_property
        import json
        doc_url = getattr(doc, "getURL", lambda: "")() or ""
        doc_spills = {}
        for key, value in SPILL_REGISTRY.items():
            k_url, sheet_name, frow, fcol = key
            if k_url == doc_url:
                doc_spills[f"{sheet_name}:{frow},{fcol}"] = value
        set_document_property(doc, "WriterAgentSpillRegistry", json.dumps(doc_spills))
    except Exception:
        log.exception("Failed to save spill registry to document property")


def locate_formula_cell(ctx: Any, sheet: Any, code_str: str) -> tuple[int, int] | None:
    """Find the row and column coordinates of the cell containing the Python formula."""
    # 1. Fast-path: check active selection and adjacent cells (above, left)
    try:
        if not (hasattr(ctx, "ServiceManager") or hasattr(ctx, "getServiceManager")):
            return None
        doc = _get_calc_doc(ctx)
        if doc is not None:
            ctrl = doc.getCurrentController()
            if ctrl is not None:
                selection = ctrl.getSelection()
                if selection is not None and hasattr(selection, "getRangeAddress"):
                    addr = selection.getRangeAddress()
                    candidates = [
                        (addr.StartRow, addr.StartColumn),
                        (addr.StartRow - 1, addr.StartColumn),
                        (addr.StartRow, addr.StartColumn - 1),
                    ]
                    for r, c in candidates:
                        if r >= 0 and c >= 0:
                            cell = sheet.getCellByPosition(c, r)
                            formula = cell.getFormula()
                            if ("PYTHON" in formula or "PY" in formula) and code_str in formula:
                                return (r, c)
    except Exception:
        pass

    # 2. Fallback: query the sheet for formula cells
    try:
        # com.sun.star.sheet.CellFlags.FORMULA = 16
        formula_cells = sheet.queryContentCells(16)
        if formula_cells is not None:
            for i in range(formula_cells.getCount()):
                cell_range = formula_cells.getByIndex(i)
                addr = cell_range.getRangeAddress()
                for r in range(addr.StartRow, addr.EndRow + 1):
                    for c in range(addr.StartColumn, addr.EndColumn + 1):
                        cell = sheet.getCellByPosition(c, r)
                        formula = cell.getFormula()
                        if ("PYTHON" in formula or "PY" in formula) and code_str in formula:
                            return (r, c)
    except Exception:
        pass

    return None


def perform_deferred_spill(
    ctx: Any,
    doc_url: str,
    sheet_name: str,
    formula_row: int,
    formula_col: int,
    grid: list[list[Any]]
) -> None:
    """Clear old spilled cells and write new values deferred (collision check is done synchronously)."""
    try:
        if not (hasattr(ctx, "ServiceManager") or hasattr(ctx, "getServiceManager")):
            return
        doc = _get_calc_doc(ctx)
        if doc is None:
            return
        
        current_url = getattr(doc, "getURL", lambda: "")() or ""
        if current_url != doc_url:
            return
        
        sheet = doc.getSheets().getByName(sheet_name)
        if sheet is None:
            return

        reg_key = (doc_url, sheet_name, formula_row, formula_col)
        
        # 1. Clear previously spilled cells
        previous_spills = SPILL_REGISTRY.get(reg_key, [])
        for r, c in previous_spills:
            if (r, c) != (formula_row, formula_col):
                try:
                    cell = sheet.getCellByPosition(c, r)
                    # Clear contents: VALUE, DATETIME, STRING, FORMULA (23)
                    cell.clearContents(23)
                except Exception:
                    pass

        # 2. Determine bounds
        num_rows = len(grid)
        num_cols = max(len(row) for row in grid) if num_rows > 0 else 0
        if num_rows == 0 or num_cols == 0:
            SPILL_REGISTRY[reg_key] = []
            save_spill_registry_for_doc(doc)
            return

        # 3. Coerce and pad grid values for rectangular setDataArray block write
        coerced_grid = []
        for row in grid:
            coerced_row = []
            for col_idx in range(num_cols):
                val = row[col_idx] if col_idx < len(row) else None
                calc_val = to_calc_compatible(val)
                if isinstance(calc_val, bool):
                    calc_val = 1.0 if calc_val else 0.0
                elif calc_val is None:
                    calc_val = ""
                coerced_row.append(calc_val)
            coerced_grid.append(coerced_row)

        # 4. Spill new values using setDataArray to avoid O(N) individual cell writes
        if num_cols > 1:
            first_row_range = sheet.getCellRangeByPosition(
                formula_col + 1, formula_row, formula_col + num_cols - 1, formula_row
            )
            first_row_range.setDataArray((tuple(coerced_grid[0][1:]),))

        if num_rows > 1:
            remaining_range = sheet.getCellRangeByPosition(
                formula_col, formula_row + 1, formula_col + num_cols - 1, formula_row + num_rows - 1
            )
            remaining_range.setDataArray(tuple(tuple(row) for row in coerced_grid[1:]))

        new_spills = []
        for r_offset in range(num_rows):
            for c_offset in range(num_cols):
                if (r_offset, c_offset) == (0, 0):
                    continue
                new_spills.append((formula_row + r_offset, formula_col + c_offset))

        SPILL_REGISTRY[reg_key] = new_spills
        save_spill_registry_for_doc(doc)

    except Exception:
        log.exception("Error in perform_deferred_spill")


def finalize_python_return(
    ctx: Any,
    code: str,
    result: Any,
    *,
    index_arg: Any = None,
    worker_data: Any = None,
) -> float | str | bool | tuple:
    """Map worker result to a single value Calc's add-in bridge accepts."""
    # Worker egress (payload_codec.child_pack_result + host_unpack_data) always yields plain
    # lists/scalars on the host — NumPy lives only in the venv subprocess, not in LO's Python.
    result = _strip_dataframe_envelope(result)

    # Auto-spill check: If it's a list/tuple, index_arg is not provided, and it's not a matrix selection
    is_matrix = False
    if isinstance(result, (list, tuple)) and index_arg is None and len(result) > 0:
        from plugin.framework.config import get_config_bool
        if get_config_bool("scripting.python_auto_spill"):
            try:
                doc = None
                if not (hasattr(ctx, "ServiceManager") or hasattr(ctx, "getServiceManager")):
                    is_matrix = True
                else:
                    doc = _get_calc_doc(ctx)
                if doc is not None:
                    ctrl = doc.getCurrentController()
                    if ctrl is not None:
                         selection = ctrl.getSelection()
                         if selection is not None and hasattr(selection, "getRangeAddress"):
                             addr = selection.getRangeAddress()
                             is_matrix = (addr.EndColumn - addr.StartColumn > 0) or (addr.EndRow - addr.StartRow > 0)
            except Exception:
                pass
        else:
            is_matrix = True

        if not is_matrix:
            grid_to_spill = []
            first_elem = result[0]
            if isinstance(first_elem, (list, tuple)):
                grid_to_spill = [list(row) for row in result]
            else:
                grid_to_spill = [[x] for x in result]

            # Get document and sheet to locate formula cell
            try:
                if not (hasattr(ctx, "ServiceManager") or hasattr(ctx, "getServiceManager")):
                    return to_calc_compatible(grid_to_spill[0][0])
                doc = _get_calc_doc(ctx)
                if doc is not None:
                    doc_url = getattr(doc, "getURL", lambda: "")() or ""
                    ctrl = doc.getCurrentController()
                    if ctrl is not None:
                        sheet = ctrl.getActiveSheet()
                        if sheet is not None:
                            sheet_name = sheet.getName()
                            formula_coord = locate_formula_cell(ctx, sheet, code)
                            log.debug("Spill: located formula cell at %r for code %r", formula_coord, code)
                            if formula_coord is not None:
                                formula_row, formula_col = formula_coord
                                
                                # Check for collisions synchronously
                                if doc_url not in LOADED_DOCUMENTS:
                                    load_spill_registry_for_doc(doc)
                                    LOADED_DOCUMENTS.add(doc_url)
                                
                                num_rows = len(grid_to_spill)
                                num_cols = max(len(row) for row in grid_to_spill) if num_rows > 0 else 0
                                reg_key = (doc_url, sheet_name, formula_row, formula_col)
                                previous_spills = SPILL_REGISTRY.get(reg_key, [])
                                prev_spill_set = set(previous_spills)
                                
                                log.debug("Spill: previous spills for cell %r: %r", reg_key, previous_spills)
                                
                                try:
                                    from com.sun.star.table.CellContentType import EMPTY
                                except ImportError:
                                    EMPTY = cast("Any", 0)

                                collides = False
                                for r_offset in range(num_rows):
                                    for c_offset in range(num_cols):
                                        target_r = formula_row + r_offset
                                        target_c = formula_col + c_offset
                                        
                                        if target_r >= 1048576 or target_c >= 16384:
                                            log.debug("Spill: collision: target coordinate %r is out of bounds", (target_r, target_c))
                                            collides = True
                                            break
                                        if (target_r, target_c) == (formula_row, formula_col):
                                            continue
                                        if (target_r, target_c) in prev_spill_set:
                                            continue
                                        cell = sheet.getCellByPosition(target_c, target_r)
                                        cell_type = cell.getType()
                                        if cell_type != EMPTY:
                                            log.debug("Spill: collision: cell at %r (type=%s, val=%r, formula=%r) is not empty", 
                                                      (target_r, target_c), cell_type, cell.getValue() or cell.getString(), cell.getFormula())
                                            collides = True
                                            break
                                    if collides:
                                        break
                                
                                if collides:
                                    return "#SPILL!"
                                
                                t = threading.Timer(
                                    0.1,
                                    perform_deferred_spill,
                                    args=(ctx, doc_url, sheet_name, formula_row, formula_col, grid_to_spill)
                                )
                                t.start()
                                
                                return to_calc_compatible(grid_to_spill[0][0])
            except Exception:
                log.exception("Error checking spill collision or locating formula cell")

    if isinstance(result, (list, tuple)):
        if index_arg is not None:
            flat = flatten_result_values(result)
            idx = coerce_index(index_arg)
            if idx < 0 or idx >= len(flat):
                return f"Error: index {idx} out of range (result length {len(flat)})"
            return to_calc_compatible(flat[idx])
        
        return scalar_for_list_result(ctx, code, result, worker_data=worker_data)

    return to_calc_compatible(result)



# Backward-compatible alias for tests and callers.
_insert_image_result_on_sheet = insert_image_result_on_sheet


def _format_error_for_display(exc: BaseException) -> str:
    """Cell-safe error text without importing ``plugin.framework.client`` (loads LLM stack)."""
    err: Exception = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
    payload = format_error_payload(err)
    return _("Error: {0}").format(payload.get("message", str(exc)))


def _code_uses_indexed_multi_data(code: str) -> bool:
    """True when inline code references ``data[n]`` (all PY args are data ranges, not a matrix index)."""
    return "data[" in (code or "")


def get_python_init_kwargs(ctx: Any) -> dict[str, Any]:
    from plugin.framework.thread_guard import on_main_thread
    from plugin.framework.queue_executor import execute_on_main_thread

    def _get_python_init_kwargs_impl() -> dict[str, Any]:
        doc = get_calc_document_from_ctx(ctx)
        if doc is not None:
            return build_python_eval_init_kwargs(doc)
        return {}

    if not on_main_thread():
        return execute_on_main_thread(_get_python_init_kwargs_impl)

    return _get_python_init_kwargs_impl()


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
        if py_data is not None:
            if is_multi and not _code_uses_indexed_multi_data(code):
                last_arg = args[-1]
                if not isinstance(last_arg, (list, tuple)) or count_cells(py_data[-1]) == 1:
                    idx_val = py_data[-1]
                    while isinstance(idx_val, list) and idx_val:
                        idx_val = idx_val[0]
                    index_arg = idx_val
                    py_data = py_data[:-1]
                    args = args[:-1]
                    is_multi = len(args) > 1
                    if py_data:
                        if not is_multi:
                            py_data = py_data[0]
                    else:
                        py_data = None
            elif is_scalar_index_arg(py_data) and not is_split_grid(py_data):
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
        # Synchronous: =PY() runs during Calc recalc; UI event pumping from
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
            session_id = workbook_session_id(ctx)
            init_kwargs = get_python_init_kwargs(ctx)
            res = run_code_in_user_venv(
                ctx,
                code,
                data=worker_data,
                session_id=session_id,
                **init_kwargs,
            )
        log.debug("PYTHON res from worker: %r", res)
        if res.get("status") == "ok":
            result = res.get("result")
            result = _strip_dataframe_envelope(result)
            log.debug("PYTHON raw result: %r (type: %s)", result, type(result).__name__)
            images = find_image_payloads(result)
            if images:
                for img in images:
                    insert_image_result_on_sheet(ctx, img)
                return _("Image inserted") if len(images) == 1 else _("Images inserted")
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
