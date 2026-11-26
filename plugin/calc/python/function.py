# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""=PY() / =PYTHON() add-in entry.

``execute_python_addin`` runs one formula call: ``build_cell_context``
resolves the caller's workbook state, worker session, and ``FormulaOrigin`` /
``is_matrix`` once into a ``CellContext`` (carried into ``finalize_python_return``
and ``spill``), the user venv runs the code, and ``finalize_python_return``
shapes the result (scalar, a native matrix (top-left value before LibreOffice 26.8), or a deferred spill).
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Literal

from plugin.calc.calc_addin_data import (
    calc_addin_args_from_split,
    check_python_data_size,
    pack_calc_data_for_wire,
    pack_calc_multi_data_for_wire,
    split_python_addin_data_args,
)
from plugin.calc.python.addin_values import (
    addin_result_for_worker_status,
    flatten_result_values,
    format_addin_error_for_display,
    helper_error_log_message,
    is_helper_error_dict,
    matrix_result,
    py_addin_cell_value,
    result_as_spill_grid,
)
from plugin.calc.python.diagnostics import diagnostics_workbook_key, get_diagnostics_store
from plugin.calc.python.formula_locator_cache import FormulaOrigin, bind_formula_locate_memo, locate_formula_cell_in_doc
from plugin.calc.python.geometric_recalc import ensure_geometric_strip_index_for_eval, maybe_strip_geometric_eval_args
from plugin.calc.python.image_egress import ImageEgressError, insert_image_result_on_sheet
from plugin.calc.python.spill import (
    PreparedSpill,
    cell_is_matrix,
    prepare_auto_spill,
    queue_deferred_spill_write,
    queue_off_main_auto_spill,
)
from plugin.calc.python.workbook_state import WorkbookState
from plugin.framework.config import get_config
from plugin.framework.errors import best_effort
from plugin.framework.i18n import _
from plugin.framework.queue_executor import main
from plugin.framework.thread_guard import background_if_dummy_thread, in_sync_host_dispatch, sync_host_dispatch
from plugin.scripting.calc_range import result_to_calc_grid
from plugin.scripting.config_limits import configured_python_max_data_cells
from plugin.scripting.payload_codec import find_image_payloads, is_dataframe_payload, strip_image_payloads
from plugin.scripting.session_manager import workbook_session_id
from plugin.scripting.venv_worker import run_code_in_user_venv

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CellContext:
    """Per-call facts about the calling workbook. On the UI thread they come from UNO; off-main only the passed-through doc and already-known state are used."""

    ctx: Any
    on_main: bool
    doc: Any
    state: WorkbookState | None  # for_doc(doc) on main, known(doc) off main
    session_id: str | None  # workbook_session_id(doc) on main, else None
    init_kwargs: dict[str, Any]
    scoped_dir: str | None
    origin: FormulaOrigin | None  # unique located =PY() cell; None off main, with no doc, or when 0 / 2+ cells match
    is_matrix: bool  # origin is part of a multi-cell array block; False whenever origin is None
    diag_workbook_key: str = "unknown"  # diagnostics_workbook_key(doc) on main (reads, never mints), else "unknown"
    code: str = ""


def _init_kwargs_for_doc(doc: Any, *, shared_session: bool) -> dict[str, Any]:
    # Lazy: document_scripts loads the scripting domain registry, which the first =PY() must not load (test_function_module_avoids_document_helpers_import).
    from plugin.scripting.document_scripts import build_python_eval_init_kwargs

    # The {} default names no init session, so a failed read leaves the workbook kernel alone.
    return best_effort(lambda: build_python_eval_init_kwargs(doc, shared_session=shared_session), "get_python_init_kwargs", {})


def build_cell_context(ctx: Any, doc: Any, code: str, *, on_main: bool) -> CellContext:
    if on_main and doc is not None:
        # Explicit on_main: use for_doc on UI thread; off-main callers use known() only (no UNO create).
        state = WorkbookState.for_doc(doc)
        session_id = best_effort(lambda: workbook_session_id(doc), "workbook session id", None) or None
        located = best_effort(lambda: locate_formula_cell_in_doc(doc, code), "locate formula cell", None)
        origin = FormulaOrigin.from_located(located) if located is not None else None
        # is_matrix is read by finalize_python_return; collapseToCurrentArray takes SolarMutex
        # (which the add-in upcall already holds) and reads ScDocument::GetMatrixFormulaRange
        # (sc/source/ui/unoobj/cursuno.cxx ScCellCursorObj::collapseToCurrentArray).
        is_matrix = cell_is_matrix(origin.sheet, origin.cell) if origin is not None else False
        init_kwargs = _init_kwargs_for_doc(doc, shared_session=session_id is not None)
        scoped_dir = None
        try:
            # Lazy: plugin.doc.document_research is not in the LibrePy bundle; there the ImportError lands in the except below and the run has no scoped folder.
            from plugin.doc.document_research import get_document_directory

            folder = get_document_directory(doc)
            scoped_dir = folder or None
        except Exception:
            log.debug("scoped_dir binding failed", exc_info=True)
        diag_workbook_key = diagnostics_workbook_key(doc)
    else:
        state = WorkbookState.known(doc) if doc is not None else None
        session_id = None
        origin = None
        is_matrix = False
        init_kwargs = {}
        scoped_dir = None
        diag_workbook_key = "unknown"
    return CellContext(
        ctx=ctx,
        on_main=on_main,
        doc=doc,
        state=state,
        session_id=session_id,
        init_kwargs=init_kwargs,
        scoped_dir=scoped_dir,
        origin=origin,
        is_matrix=is_matrix,
        diag_workbook_key=diag_workbook_key,
        code=code,
    )


def _record_py_diagnostic(
    code: str,
    res: dict[str, Any] | None,
    *,
    status: str,
    message: str = "",
    traceback: str = "",
    workbook_key: str,
) -> None:
    """Record stdout/errors for the LibrePy sidebar without extra UNO work."""
    stdout = ""
    tb = traceback
    msg = message
    if res is not None:
        stdout = str(res.get("stdout") or "")
        if not msg:
            msg = str(res.get("message") or res.get("error") or "")
        if not tb:
            raw_tb = res.get("traceback")
            tb = str(raw_tb) if raw_tb else ""
    if status == "ok" and not stdout.strip():
        return
    best_effort(
        lambda: get_diagnostics_store().record(
            workbook_key=workbook_key,
            code=code,
            status=status,
            message=msg,
            stdout=stdout,
            traceback=tb,
        ),
        "record_python_eval",
    )


def _auto_spill_grid(cell: CellContext, result: Any) -> list[list[Any]] | None:
    """The grid *result* spills into, ``[]`` to clear a stale spill, or None when auto-spill is off."""
    if cell.is_matrix or not get_config("scripting.python_auto_spill"):
        return None
    if not isinstance(result, (list, tuple)) or not result:
        return []
    grid = result_as_spill_grid(result)
    return grid if grid[0] else []


def _dispatch_auto_spill(cell: CellContext, grid_to_spill: list[list[Any]]) -> Literal["#SPILL!", "dispatched"] | None:
    """Queue a spill write or stale-spill clear.

    Returns ``"#SPILL!"`` when an array spill is blocked, ``"dispatched"`` when a main-thread
    write or clear was queued, or ``None`` when skipped or queued off-main.
    """
    try:
        if not grid_to_spill and (
            cell.state is None or (cell.state.loaded and not cell.state.has_spill_activity())
        ):
            return None
        if not cell.on_main:
            if cell.doc is not None:
                queue_off_main_auto_spill(cell.ctx, cell.code, grid_to_spill, cell.doc)
            return None
        if cell.origin is not None and cell.state is not None:
            prepared = prepare_auto_spill(cell.ctx, cell.doc, cell.state, cell.origin, cell.code, grid_to_spill)
            if prepared == "#SPILL!":
                return "#SPILL!"
            if isinstance(prepared, PreparedSpill):
                queue_deferred_spill_write(cell.state, cell.doc, prepared, grid_to_spill)
                return "dispatched"
    except Exception:
        log.exception("Error dispatching auto-spill")
    return None


def finalize_python_return(
    cell: CellContext,
    result: Any,
) -> Any:
    """Map worker result to a scalar or native matrix Calc's add-in bridge accepts."""
    result = result_to_calc_grid(result)

    grid_to_spill = _auto_spill_grid(cell, result)
    if grid_to_spill is not None:
        status = _dispatch_auto_spill(cell, grid_to_spill)
        if status == "#SPILL!":
            return "#SPILL!"
        if status == "dispatched" and grid_to_spill:
            return py_addin_cell_value(grid_to_spill[0][0])

    if isinstance(result, (list, tuple)):
        return matrix_result(cell.ctx, result)

    return py_addin_cell_value(result)


def execute_python_addin(ctx: Any, code: str, data: Any = None, *, doc: Any | None = None) -> Any:
    """Run *code* in the user venv and return a Calc-compatible scalar, matrix, or error string."""
    with background_if_dummy_thread("python_addin"):
        with sync_host_dispatch():
            with bind_formula_locate_memo():
                return _execute_python_addin_impl(ctx, code, data, doc=doc)


def _cells_beside_images(result: Any) -> list[Any] | None:
    """The result with its images blanked, or None when there is nothing for cells to show.

    A dict (a viz helper's report, a dict of figures, a nested DataFrame envelope), a list
    inside one cell, or a DataFrame/Series whose body holds only figures has no cell form,
    so such a result shows the inserted text instead.
    """
    grid = strip_image_payloads(result_to_calc_grid(result), placeholder=None)
    if not isinstance(grid, list):
        return None
    body = grid[1:] if is_dataframe_payload(result) else grid
    cells = flatten_result_values(grid)
    if any(isinstance(c, (dict, list, tuple)) for c in cells) or all(c is None for c in flatten_result_values(body)):
        return None
    return grid


def _execute_python_addin_impl(ctx: Any, code: str, data: Any = None, *, doc: Any | None = None) -> Any:
    on_main = main.is_current()
    cell_ctx = build_cell_context(ctx, doc, code, on_main=on_main)

    try:
        args = split_python_addin_data_args(data)
        ensure_geometric_strip_index_for_eval(cell_ctx)
        args = maybe_strip_geometric_eval_args(code, args, cell_ctx)
        py_data = calc_addin_args_from_split(args)
        is_multi = len(args) > 1
        max_cells = configured_python_max_data_cells()
        if py_data is not None:
            size_err = check_python_data_size(py_data, max_cells=max_cells, multi=is_multi)
            if size_err:
                ret = f"Error: {size_err}"
                _record_py_diagnostic(code, None, status="error", message=ret, workbook_key=cell_ctx.diag_workbook_key)
                return ret
            worker_data = pack_calc_multi_data_for_wire(py_data) if is_multi else pack_calc_data_for_wire(py_data)
        else:
            worker_data = None

        log.debug(
            "PYTHON eval: target_doc=%s session_id=%r has_init=%s on_main=%s in_sync_host=%s",
            doc is not None,
            cell_ctx.session_id,
            "init_script" in cell_ctx.init_kwargs,
            cell_ctx.on_main,
            in_sync_host_dispatch(),
        )
        res = run_code_in_user_venv(
            code,
            data=worker_data,
            bindings={"scoped_dir": cell_ctx.scoped_dir},
            session_id=cell_ctx.session_id,
            python_tool_domain="",
            **cell_ctx.init_kwargs,
        )
        if res.get("status") == "ok":
            result = res.get("result")
            if isinstance(result, dict) and is_helper_error_dict(result):
                err_text = helper_error_log_message(result)
                addin_val = py_addin_cell_value(result)
                _record_py_diagnostic(code, res, status="error", message=err_text, workbook_key=cell_ctx.diag_workbook_key)
                return addin_val
            _record_py_diagnostic(code, res, status="ok", workbook_key=cell_ctx.diag_workbook_key)
            images = find_image_payloads(result)
            if images:
                cells = _cells_beside_images(result)
                spill = _auto_spill_grid(cell_ctx, cells)
                spill_cols = max(len(row) for row in spill) if spill else 1
                # Every slot is processed even when one fails, so later plots still refresh
                # instead of keeping the previous run's pictures. The first failure is then
                # raised and becomes the cell's error text.
                first_error: ImageEgressError | None = None
                for i, img in enumerate(images):
                    try:
                        insert_image_result_on_sheet(
                            img,
                            code=code,
                            doc=doc,
                            image_index=i,
                            image_count=len(images),
                            notify_cell=True,
                            spill_cols=spill_cols,
                        )
                    except ImageEgressError as err:
                        first_error = first_error or err
                if first_error is not None:
                    raise first_error
                if cells is None:
                    _dispatch_auto_spill(cell_ctx, [])
                    return _("Image inserted") if len(images) == 1 else _("Images inserted")
                return finalize_python_return(cell_ctx, cells)
            return finalize_python_return(cell_ctx, result)

        raw_message = str(res.get("message") or res.get("error") or "")
        addin_cell = addin_result_for_worker_status(res)
        message = addin_cell if isinstance(addin_cell, str) else raw_message
        _record_py_diagnostic(code, res, status="error", message=message, workbook_key=cell_ctx.diag_workbook_key)
        return addin_cell
    except Exception as e:
        log.exception("PYTHON unexpected error during execution")
        err_msg = format_addin_error_for_display(e)
        _record_py_diagnostic(code, None, status="error", message=err_msg, traceback=str(e), workbook_key=cell_ctx.diag_workbook_key)
        return err_msg
