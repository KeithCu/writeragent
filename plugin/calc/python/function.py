# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""=PY() / =PYTHON() add-in entry.

``execute_python_addin`` runs one formula call: ``build_cell_context``
resolves the caller's workbook state and worker session into a
``CellContext``, the user venv runs the code, and ``finalize_python_return``
shapes the result (scalar, a matrix cached in ``WorkerResultSession`` for the
rest of the recalc, or a deferred spill). ``clear_python_addin_cache`` drops
every workbook's matrix cache.
"""
# pyright: reportUnusedFunction=false

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
import re
import threading
import time
from typing import Any

from plugin.calc.calc_addin_data import (
    calc_addin_args_from_split,
    check_python_data_size,
    pack_calc_data_for_wire,
    pack_calc_multi_data_for_wire,
    split_python_addin_data_args,
)
from plugin.calc.python.addin_values import (
    _addin_result_for_worker_status,
    _flat_result_equal,
    _format_error_for_display,
    _helper_error_log_message,
    _is_helper_error_dict,
    _result_as_spill_grid,
    _unwrap_single_cell,
    coerce_index,
    flatten_result_values,
    is_scalar_index_arg,
    py_addin_cell_value,
)
from plugin.calc.python.image_egress import ImageEgressError, insert_image_result_on_sheet
from plugin.calc.python.spill import (
    PreparedSpill,
    _cell_is_matrix,
    _prepare_auto_spill,
    _queue_deferred_spill_write,
    _queue_off_main_auto_spill,
    _selection_is_multi_cell,
)
from plugin.calc.python.workbook_state import WorkbookState
from plugin.framework.errors import best_effort, uno_read
from plugin.framework.i18n import _
from plugin.framework.queue_executor import main
from plugin.framework.thread_guard import background_if_dummy_thread, sync_host_dispatch
from plugin.scripting.calc_range import result_to_calc_grid
from plugin.scripting.config_limits import configured_python_max_data_cells
from plugin.scripting.payload_codec import find_image_payloads, is_split_grid, wire_cell_count
from plugin.scripting.session_manager import workbook_session_id
from plugin.scripting.venv_worker import run_code_in_user_venv

log = logging.getLogger(__name__)

_MATRIX_SCALAR_SESSIONS_LOCK = threading.Lock()

_PY_PASS_GAP_SEC = 2.0

_INDEXED_BINDING_RE = re.compile(r"(?<![A-Za-z0-9_.])(?:data|ranges)\[")


def _code_uses_indexed_multi_data(code: str) -> bool:
    """True when inline code references bare ``data[n]`` or ``ranges[n]`` (a preceding ``.`` does not count; all PY args are data ranges)."""
    return bool(_INDEXED_BINDING_RE.search(code or ""))


@dataclass(eq=False, slots=True)
class WorkerResultSession:
    """Caches one worker list result across multiple =PY() calls in a recalc pass."""

    raw: Any
    flat: tuple[Any, ...]
    next_index: int = 0
    timestamp: float = field(default_factory=time.monotonic)


def _worker_data_token(worker_data: Any) -> str:
    """Stable sha256 digest of worker data to identify matrix sessions."""
    h = hashlib.sha256()
    seen: set[int] = set()

    def _walk(obj: Any) -> None:
        if obj is None:
            h.update(b"N")
            return
        if isinstance(obj, bool):
            h.update(b"B1" if obj else b"B0")
            return
        if isinstance(obj, (int, float)):
            h.update(b"F" + repr(obj).encode("ascii", "replace"))
            return
        if isinstance(obj, str):
            h.update(b"S" + obj.encode("utf-8", "surrogatepass"))
            return
        if isinstance(obj, (bytes, bytearray)):
            h.update(b"Y" + bytes(obj))
            return
        obj_id = id(obj)
        if obj_id in seen:
            h.update(b"cycle")
            return
        seen.add(obj_id)
        try:
            if isinstance(obj, dict):
                h.update(b"D")
                for k, v in sorted(obj.items(), key=lambda kv: str(kv[0])):
                    _walk(str(k))
                    _walk(v)
                return
            if isinstance(obj, (list, tuple)):
                h.update(b"L")
                for item in obj:
                    _walk(item)
                return
            h.update(b"r" + repr(obj).encode("utf-8", "replace"))
        finally:
            seen.remove(obj_id)

    _walk(worker_data)
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class CellContext:
    """Per-call facts about the calling workbook. On the UI thread they come from UNO; off-main only the passed-through doc and already-known state are used."""

    ctx: Any
    on_main: bool
    doc: Any
    state: WorkbookState | None  # for_doc(doc) when on_main and doc is not None, else known(doc)
    session_id: str | None  # sk[2] when on_main, else None
    init_kwargs: dict[str, Any]
    scoped_dir: str | None
    origin: str  # "row,col" or ""
    workbook_key: str = ""  # calc_workbook_base_session_id(doc) when on_main and doc is not None, else ""
    diag_workbook_key: str = "unknown"  # existing_calc_session_id(doc) or "unknown" when on_main, else "unknown"
    sk: tuple[str, ...] = ()
    code: str = ""


def _session_key_tuple(ctx: Any, code: str, doc: Any | None, *, on_main: bool) -> tuple[str, ...]:
    if not on_main:
        return ("", "", "", code, "")
    doc_url = ""
    sheet_name = ""
    sid = ""
    origin = ""
    try:
        target = doc
        if target is not None:
            url_val = getattr(target, "getURL", lambda: "")()
            doc_url = url_val if isinstance(url_val, str) else ""
            sid = workbook_session_id(target) or ""
            from plugin.calc.python.formula_locator_cache import locate_formula_cell_in_doc

            located = locate_formula_cell_in_doc(target, code)
            if located is not None:
                sheet, _cell, coord = located
                name_val = getattr(sheet, "getName", lambda: "")()
                sheet_name = name_val if isinstance(name_val, str) else ""
                origin = f"{coord[0]},{coord[1]}"

    except Exception:
        log.debug("session_key inline metadata lookup exception", exc_info=True)
    return (doc_url, sheet_name, sid, code, origin)


def _init_kwargs_for_doc(doc: Any) -> dict[str, Any]:
    from plugin.scripting.document_scripts import build_python_eval_init_kwargs

    return best_effort(lambda: build_python_eval_init_kwargs(doc), "get_python_init_kwargs", {}) or {}


def build_cell_context(ctx: Any, doc: Any, code: str, *, on_main: bool | None = None) -> CellContext:
    if on_main is None:
        on_main = main.is_current()
    if on_main:
        # Explicit on_main: use for_doc on UI thread; off-main callers use known() only (no UNO create).
        state = WorkbookState.for_doc(doc) if doc is not None else None
        sk = _session_key_tuple(ctx, code, doc, on_main=True)
        session_id = sk[2] or None
        origin = sk[4]
        init_kwargs = _init_kwargs_for_doc(doc) if doc is not None else {}
        scoped_dir = None
        if doc is not None:
            try:
                from plugin.doc.document_research import get_document_directory

                folder = get_document_directory(doc)
                scoped_dir = folder or None
            except Exception:
                log.debug("scoped_dir binding failed", exc_info=True)
        from plugin.scripting.session_manager import calc_workbook_base_session_id, existing_calc_session_id

        workbook_key = calc_workbook_base_session_id(doc) if doc is not None else ""
        diag_workbook_key = (
            best_effort(lambda: existing_calc_session_id(doc) or "unknown", "diagnostics workbook key", "unknown")
            if doc is not None
            else "unknown"
        )
    else:
        state = WorkbookState.known(doc) if doc is not None else None
        sk = ("", "", "", code, "")
        session_id = None
        origin = ""
        init_kwargs = {}
        scoped_dir = None
        workbook_key = ""
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
        workbook_key=workbook_key,
        diag_workbook_key=diag_workbook_key,
        sk=sk,
        code=code,
    )


def _get_calc_doc(ctx: Any) -> Any | None:
    from plugin.framework.thread_guard import guard_uno

    if not main.is_current():
        return None
    from plugin.framework.uno_context import get_desktop

    def _probe() -> Any | None:
        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent() if desktop is not None else None
        if doc is not None and hasattr(doc, "getSheets"):
            return guard_uno(doc)
        return None

    return uno_read(_probe, None)


def _expire_matrix_sessions(matrix: dict[tuple[Any, ...], WorkerResultSession], now: float) -> None:
    stale = [
        k for k, session in matrix.items()
        if (now - session.timestamp) > _PY_PASS_GAP_SEC
    ]
    for k in stale:
        matrix.pop(k, None)


def scalar_for_list_result(
    cell: CellContext,
    result: Any,
    *,
    worker_data: Any = None,
    data_token: str | None = None,
) -> float | str | bool:
    """Return one Calc scalar per invocation when the worker produced a list."""
    flat: tuple[Any, ...] = tuple(py_addin_cell_value(v) for v in flatten_result_values(result))
    if not flat:
        return ""
    sk = cell.sk
    if not sk[4]:
        return flat[0]
    token = data_token if data_token is not None else _worker_data_token(worker_data)
    key = (threading.get_ident(), sk, token)
    now = time.monotonic()

    wb_state = cell.state
    if wb_state is None:
        return flat[0]

    with _MATRIX_SCALAR_SESSIONS_LOCK:
        _expire_matrix_sessions(wb_state.matrix, now)
        state = wb_state.matrix.get(key)
        if state is None or not _flat_result_equal(state.flat, flat):
            state = WorkerResultSession(result, flat, timestamp=now)
            wb_state.matrix[key] = state
        state.timestamp = now
        idx = state.next_index
        state.next_index = idx + 1
        if state.next_index >= len(state.flat):
            wb_state.matrix.pop(key, None)
    return state.flat[idx]


def clear_python_addin_cache() -> None:
    """Clear formula result cache across all threads (e.g. on session reset or document reload)."""
    with _MATRIX_SCALAR_SESSIONS_LOCK:
        for state in WorkbookState.all_states():
            state.matrix.clear()


def _record_py_diagnostic(
    ctx: Any,
    code: str,
    res: dict[str, Any] | None,
    *,
    status: str,
    message: str = "",
    traceback: str = "",
    doc: Any | None = None,
    workbook_key: str | None = None,
) -> None:
    """Record stdout/errors for the LibrePy sidebar without extra UNO work."""
    from plugin.calc.python.diagnostics import diagnostics_workbook_key, get_diagnostics_store

    stdout = ""
    tb = traceback
    msg = message
    if isinstance(res, dict):
        stdout = str(res.get("stdout") or "")
        if not msg:
            msg = str(res.get("message") or res.get("error") or "")
        if not tb:
            raw_tb = res.get("traceback")
            tb = str(raw_tb) if raw_tb else ""
    if status == "ok" and not (stdout or "").strip():
        return
    wb_key = workbook_key if workbook_key is not None else (
        getattr(ctx, "diag_workbook_key", None) or diagnostics_workbook_key(doc)
    )
    best_effort(
        lambda: get_diagnostics_store().record(
            workbook_key=wb_key,
            code=code or "",
            status=status,
            message=msg,
            stdout=stdout,
            traceback=tb,
        ),
        "record_python_eval",
    )


def finalize_python_return(
    cell: CellContext,
    result: Any,
    *,
    index_arg: Any = None,
    worker_data: Any = None,
    data_token: str | None = None,
) -> float | str | bool | tuple[Any, ...]:
    """Map worker result to a single value Calc's add-in bridge accepts."""
    on_main = cell.on_main
    code = cell.code
    doc = cell.doc
    ctx = cell.ctx

    result = result_to_calc_grid(result)

    is_matrix = False
    if isinstance(result, (list, tuple)) and index_arg is None and len(result) > 0:
        from plugin.framework.config import get_config

        if get_config("scripting.python_auto_spill"):
            if on_main:
                try:
                    if doc is not None:
                        from plugin.calc.python.formula_locator_cache import locate_formula_cell_in_doc

                        located = locate_formula_cell_in_doc(doc, code)
                        if located is not None:
                            sheet, formula_cell, _coord = located
                            is_matrix = _cell_is_matrix(sheet, formula_cell)
                        else:
                            is_matrix = _selection_is_multi_cell(doc)
                except Exception:
                    log.debug("matrix check failed", exc_info=True)
        else:
            is_matrix = True

        if not is_matrix:
            grid_to_spill = _result_as_spill_grid(result)
            if grid_to_spill and grid_to_spill[0]:
                try:
                    if not on_main:
                        if doc is not None:
                            _queue_off_main_auto_spill(ctx, code, grid_to_spill, doc)
                        return py_addin_cell_value(grid_to_spill[0][0])

                    if doc is not None:
                        prepared = _prepare_auto_spill(ctx, code, grid_to_spill, doc)
                        if prepared == "#SPILL!":
                            return "#SPILL!"
                        if isinstance(prepared, PreparedSpill):
                            _queue_deferred_spill_write(code, grid_to_spill, doc, prepared)
                            return py_addin_cell_value(grid_to_spill[0][0])
                except Exception:
                    log.exception("Error checking spill collision or locating formula cell")

    if isinstance(result, (list, tuple)):
        if index_arg is not None:
            flat = flatten_result_values(result)
            idx = coerce_index(index_arg)
            if idx < 0 or idx >= len(flat):
                return f"Error: index {idx} out of range (result length {len(flat)})"
            return py_addin_cell_value(flat[idx])

        return scalar_for_list_result(cell, result, worker_data=worker_data, data_token=data_token)

    return py_addin_cell_value(result)


def execute_python_addin(ctx: Any, code: str, data: Any = None, *, doc: Any | None = None) -> Any:
    """Run *code* in the user venv and return a Calc-compatible scalar (or error string)."""
    with background_if_dummy_thread("python_addin"):
        with sync_host_dispatch():
            from plugin.calc.python.formula_locator_cache import bind_formula_locate_memo

            with bind_formula_locate_memo():
                return _execute_python_addin_impl(ctx, code, data, doc=doc)


def _execute_python_addin_impl(ctx: Any, code: str, data: Any = None, *, doc: Any | None = None) -> Any:
    target_doc: Any = doc
    on_main = main.is_current()
    cell_ctx = build_cell_context(ctx, target_doc, code, on_main=on_main)

    try:
        args = split_python_addin_data_args(data)
        from plugin.calc.python.geometric_recalc import ensure_geometric_strip_index_for_eval, maybe_strip_geometric_eval_args

        ensure_geometric_strip_index_for_eval(target_doc, ctx, cell_ctx=cell_ctx)
        args = maybe_strip_geometric_eval_args(code, args, doc=target_doc, cell_ctx=cell_ctx)
        py_data = calc_addin_args_from_split(args)
        is_multi = len(args) > 1
        index_arg = None
        if py_data is not None:
            if is_multi and not _code_uses_indexed_multi_data(code):
                last_arg = args[-1]
                if not isinstance(last_arg, (list, tuple)) or wire_cell_count(py_data[-1]) == 1:
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
                index_arg = _unwrap_single_cell(py_data)
        max_cells = configured_python_max_data_cells()
        if py_data is not None:
            size_err = check_python_data_size(py_data, max_cells=max_cells, multi=is_multi)
            if size_err:
                ret = f"Error: {size_err}"
                _record_py_diagnostic(ctx, code, None, status="error", message=ret, doc=target_doc, workbook_key=cell_ctx.diag_workbook_key)
                return ret
            worker_data = pack_calc_multi_data_for_wire(py_data) if is_multi else pack_calc_data_for_wire(py_data)
        else:
            worker_data = None

        tid = threading.get_ident()
        sk = cell_ctx.sk
        unique_origin = bool(cell_ctx.origin)
        now = time.monotonic()
        cached = None
        data_token: str | None = None
        if unique_origin:
            data_token = _worker_data_token(worker_data)
            cache_key = (tid, sk, data_token)
            wb_state = cell_ctx.state
            if wb_state is not None:
                with _MATRIX_SCALAR_SESSIONS_LOCK:
                    _expire_matrix_sessions(wb_state.matrix, now)
                    cached = wb_state.matrix.get(cache_key)
        if isinstance(cached, WorkerResultSession) and cached.next_index < len(cached.flat):
            res = {"status": "ok", "result": cached.raw}
        else:
            from plugin.framework.thread_guard import in_sync_host_dispatch

            session_id = cell_ctx.session_id
            init_kwargs = cell_ctx.init_kwargs

            log.debug(
                "PYTHON eval: target_doc=%s session_id=%r has_init=%s on_main=%s in_sync_host=%s",
                target_doc is not None,
                session_id,
                bool(init_kwargs),
                cell_ctx.on_main,
                in_sync_host_dispatch(),
            )
            res = run_code_in_user_venv(
                code,
                data=worker_data,
                bindings={"scoped_dir": cell_ctx.scoped_dir},
                session_id=session_id,
                python_tool_domain="",
                **init_kwargs,
            )
        if res.get("status") == "ok":
            result = res.get("result")
            if isinstance(result, dict) and _is_helper_error_dict(result):
                err_text = _helper_error_log_message(result)
                addin_val = py_addin_cell_value(result)
                _record_py_diagnostic(ctx, code, res, status="error", message=err_text, doc=target_doc, workbook_key=cell_ctx.diag_workbook_key)
                return addin_val
            _record_py_diagnostic(ctx, code, res, status="ok", doc=target_doc, workbook_key=cell_ctx.diag_workbook_key)
            images = find_image_payloads(result)
            if images:
                # Every slot is processed even when one fails, so later plots still refresh
                # instead of keeping the previous run's pictures. The first failure is then
                # raised and becomes the cell's error text.
                first_error: ImageEgressError | None = None
                for i, img in enumerate(images):
                    try:
                        insert_image_result_on_sheet(ctx, img, code=code, doc=target_doc, image_index=i, image_count=len(images), notify_cell=True)
                    except ImageEgressError as err:
                        first_error = first_error or err
                if first_error is not None:
                    raise first_error
                return _("Image inserted") if len(images) == 1 else _("Images inserted")
            return finalize_python_return(cell_ctx, result, index_arg=index_arg, worker_data=worker_data, data_token=data_token)

        raw_message = str(res.get("message") or res.get("error") or "")
        addin_cell = _addin_result_for_worker_status(res)
        message = addin_cell if isinstance(addin_cell, str) else raw_message
        _record_py_diagnostic(ctx, code, res, status="error", message=message, doc=target_doc, workbook_key=cell_ctx.diag_workbook_key)
        return addin_cell
    except Exception as e:
        log.exception("PYTHON unexpected error during execution")
        err_msg = _format_error_for_display(e)
        _record_py_diagnostic(ctx, code, None, status="error", message=err_msg, traceback=str(e), doc=target_doc, workbook_key=cell_ctx.diag_workbook_key)
        return err_msg
