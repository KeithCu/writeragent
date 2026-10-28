# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Dialog and execution logic for Tools → Run Python Script (Writer, Calc, and Draw/Impress).

LibrePy and WriterAgent both register this menu. Writer inserts HTML at the
selection; Calc writes values from the active selection; Draw/Impress shows a
message only.

Concurrency: _monaco_run_in_flight is a process-wide set of document keys.
Monaco on_save checks and updates it on the UI thread only. It is not locked.
"""

import ast
import html as html_mod
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from plugin.framework.uno_context import get_ctx, get_desktop, get_runtime_uid
from plugin.framework.config import get_config_str
from plugin.framework.errors import reraise_if_disposed
from plugin.framework.i18n import _
from plugin.chatbot.dialogs import msgbox
from plugin.scripting.editor_errors import editor_error_markers, exception_traceback
from plugin.scripting.editor_host import DeferredEditorResult, launch_monaco_editor, monaco_editor_available
from plugin.scripting.venv_worker import run_code_in_user_venv
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.python_runner_ui import show_python_input_dialog
from plugin.writer.format import insert_content_at_position
from plugin.doc.doc_type import is_calc, is_writer, is_draw
from plugin.calc.address_utils import index_to_column
from plugin.scripting.payload_codec import is_dataframe_payload
from plugin.scripting.helper_domain import (
    HelperScriptMeta,
    format_elapsed_time,
    parse_run_import_call_spec,
    plot_insert_ok_outcome,
    rps_error_outcome,
    rps_insert_failed_outcome,
    rps_ok_outcome,
    script_header_needs_data_binding,
)

if TYPE_CHECKING:
    from plugin.scripting.domain_registry import RpsDomainSpec

log = logging.getLogger("writeragent.scripting")

_monaco_run_in_flight: set[str] = set()


def _monaco_run_document_key(doc: Any) -> str:
    """Document key for Monaco in-flight run tracking.

    Uses get_runtime_uid(doc). Non-empty UID becomes 'uid:' + uid;
    otherwise falls back to a per-object id ('id:' + id(doc)).
    """
    uid = get_runtime_uid(doc)
    if uid:
        return "uid:" + uid
    return f"id:{id(doc)}"


def _html_insert_text(value: Any) -> str:
    """Escape a script result so it stays text after Writer/Calc HTML insert.

    ``insert_content_at_position`` and ``insert_cell_html_rich`` call
    ``html.unescape`` before the StarWriter HTML filter, so a single
    ``html.escape`` is undone and ``<`` becomes markup again. Double-escape
    so a literal ``<`` survives as text (``&amp;lt;`` → ``&lt;`` → ``<``).
    """
    return html_mod.escape(html_mod.escape(str(value)))


def _format_list_to_table(data: list[Any], *, headers: list[Any] | None = None) -> str:
    """Convert a list of dicts or lists to an HTML table.

    With headers (dataframe egress), always emit <table>, including a
    one-column series. Without headers, a flat list of scalars is
    <br>-joined text; that is intentional.
    """
    if not data:
        return ""

    parts = []

    # Explicit headers (e.g. from dataframe payload) take precedence for order and 1-col cases.
    if headers:
        parts.append('<table border="1"><thead><tr>')
        for h in headers:
            parts.append(f"<th>{_html_insert_text(h)}</th>")
        parts.append("</tr></thead><tbody>")
        # data may be list of lists (2d) or flat list (1-col series-like)
        if data and isinstance(data[0], (list, tuple)):
            for row in data:
                parts.append("<tr>")
                for cell in row:
                    parts.append(f"<td>{_html_insert_text(cell)}</td>")
                parts.append("</tr>")
        else:
            for v in data:
                parts.append(f"<tr><td>{_html_insert_text(v)}</td></tr>")
        parts.append("</tbody></table>")
        return "".join(parts)

    # Handle list of dicts (e.g. pandas records) -- legacy path
    # Dict headers must come from the first-seen union of every dict row.
    # What was wrong: a non-string key was stored as str(k) and read back with
    # row.get on that string, so {1: "a"} rendered a header 1 and a blank cell.
    # How it happened: setdefault(str(k), None) dropped the original key, and
    # test_format_list_of_dicts only used string keys.
    # Why this change fixes it: the header string maps to the first-seen original
    # key, and a row that only has a different object with the same str() is scanned.
    if any(isinstance(row, dict) for row in data):
        seen_keys: dict[str, Any] = {}
        for row in data:
            if isinstance(row, dict):
                for k in row.keys():
                    seen_keys.setdefault(str(k), k)
        keys = list(seen_keys.keys())
        parts.append('<table border="1"><thead><tr>')
        for key in keys:
            parts.append(f"<th>{_html_insert_text(key)}</th>")
        parts.append("</tr></thead><tbody>")
        for row in data:
            parts.append("<tr>")
            if isinstance(row, dict):
                for key in keys:
                    orig_key = seen_keys[key]
                    if orig_key in row:
                        val = row[orig_key]
                    else:
                        val = ""
                        for candidate_k, candidate_v in row.items():
                            if str(candidate_k) == key:
                                val = candidate_v
                                break
                    parts.append(f"<td>{_html_insert_text(val)}</td>")
            else:
                parts.append(f"<td>{_html_insert_text(row)}</td>")
                for _col_idx in range(len(keys) - 1):
                    parts.append("<td></td>")
            parts.append("</tr>")
        parts.append("</tbody></table>")
        return "".join(parts)

    # Handle list of lists (table)
    if isinstance(data[0], (list, tuple)):
        parts.append('<table border="1">')
        for row in data:
            parts.append("<tr>")
            if isinstance(row, (list, tuple)):
                for cell in row:
                    parts.append(f"<td>{_html_insert_text(cell)}</td>")
            else:
                parts.append(f"<td>{_html_insert_text(row)}</td>")
            parts.append("</tr>")
        parts.append("</table>")
        return "".join(parts)

    # Fallback: list of primitives
    return "<br>".join(_html_insert_text(x) for x in data)


def is_shape_tool_status_result(result: Any) -> bool:
    """True for shape_upsert/edit status dicts that must not be HTML-dumped into Writer."""
    if not isinstance(result, dict) or not result:
        return False
    if "geometry_applied" in result or "shape_count_after" in result or "custom_shape_engine" in result:
        return True
    msg = str(result.get("message") or "")
    if result.get("status") == "ok" and ("index" in result or "page" in result):
        if msg.startswith("Created ") or msg == "Shape updated":
            return True
    return False


def format_result_for_writer(result: Any) -> str:
    """Format the Python execution result for insertion into Writer.

    - Lists of dicts/lists become HTML tables.
    - Dicts become a series of sections (with tables for nested lists).
    - Strings/primitives are returned as-is (with newline conversion).
    """
    if result is None:
        return ""
    if isinstance(result, (list, dict)) and not result:
        return ""
    if isinstance(result, str) and not result:
        return ""

    if isinstance(result, dict) and is_dataframe_payload(result):
        cols = result.get("columns") or []
        data = result.get("data") or []
        return _format_list_to_table(data if isinstance(data, list) else [], headers=cols if cols else None)

    if isinstance(result, list):
        return _format_list_to_table(result)

    if isinstance(result, dict):
        html_parts = []
        # Priority keys to show without a bold label if they are strings
        priority_keys = ("title", "summary", "summary_text", "message", "text", "result")

        # Use original insertion order. Skip underscores.
        sorted_keys = [k for k in result.keys() if not str(k).startswith("_")]

        for key in sorted_keys:
            val = result[key]
            if isinstance(val, list) and val:
                table = _format_list_to_table(val)
                if table:
                    html_parts.append(f"<h3>{_html_insert_text(key)}</h3>")
                    html_parts.append(table)
            else:
                escaped = _html_insert_text(val).replace("\n", "<br>")
                lower_key = str(key).lower()
                if lower_key in priority_keys:
                    html_parts.append(f"<p><b>{escaped}</b></p>")
                else:
                    html_parts.append(f"<p><b>{_html_insert_text(key)}:</b> {escaped}</p>")

        return "\n".join(html_parts)

    return _html_insert_text(result).replace("\n", "<br>")


def insert_result_into_calc(doc: Any, uno_ctx: Any, result: Any) -> None:
    """Insert the result of a Python script into a Calc document.

    Formats structured and tabular data via HTML and inserts via controller
    transferable paste (insert_cell_html_rich). This registers a native C++
    ScUndo action in LibreOffice Calc, allowing single-step Ctrl+Z undo.
    """
    if result is None:
        return
    if is_shape_tool_status_result(result):
        log.debug("Skipping Calc result insert for shape tool status dict (keys=%s)", sorted(result.keys()))
        return

    # Determine anchor cell from selection
    controller = doc.getCurrentController() if doc else None
    selection = controller.getSelection() if controller else None

    start_col = 0
    start_row = 0
    if selection and hasattr(selection, "getRangeAddress"):
        addr = selection.getRangeAddress()
        start_col = addr.StartColumn
        start_row = addr.StartRow

    anchor_addr = f"{index_to_column(start_col)}{start_row + 1}"
    formatted = format_result_for_writer(result)
    if formatted:
        from plugin.calc.rich_html import insert_cell_html_rich

        insert_cell_html_rich(doc, uno_ctx, anchor_addr, formatted)


def insert_result_into_draw(doc: Any, uno_ctx: Any, result: Any) -> None:
    """Insert the result of a Python script into a Draw/Impress document."""
    del doc, result
    msgbox(uno_ctx, _("Info"), _("Result insertion into Draw/Impress is not yet supported. PRs welcome!"))


def script_uses_run_import(code: str, *, run_name: str) -> bool:
    """True when *code* contains a call to *run_name*.

    The substring fallback runs only when ast.parse raises. Using it on valid
    source matched comments such as ``# run_vision(`` and sent the script into
    that domain.
    """
    if not code or not run_name:
        return False
    try:
        tree = ast.parse(code)
    except (SyntaxError, TypeError, ValueError):
        return f"{run_name}(" in code
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == run_name:
            return True
    return False


def script_imports_module(code: str, module_name: str) -> bool:
    """True when *code* imports *module_name* (import or from-import).

    A raw substring search matches comments and docstrings. Walk the AST for
    a real import, and fall back to the substring only when ast.parse raises.
    """
    if not code or not module_name:
        return False
    try:
        tree = ast.parse(code)
    except (SyntaxError, TypeError, ValueError):
        return module_name in code
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name or alias.name.startswith(module_name + "."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == module_name:
                return True
            if node.module and module_name.startswith(node.module + "."):
                sub = module_name[len(node.module) + 1 :]
                for alias in node.names:
                    if alias.name == sub:
                        return True
    return False
def _prepare_vision(ctx: Any, doc: Any, code: str, t0: float) -> dict[str, Any] | None:
    """Return prepared vision execution dict, early error dict, or None if not a vision script."""
    if "run_vision" not in code or not script_uses_run_import(code, run_name="run_vision"):
        return None

    from plugin.vision.vision_common import merge_vision_params
    from plugin.vision.vision_runner import supports_vision_manual

    if not supports_vision_manual(doc):
        return {"early_outcome": {"ok": False, "message": _("Vision helpers require a Writer or Calc document.")}}
    call_spec = parse_run_import_call_spec(code, run_name="run_vision") or {}
    raw_params = call_spec.get("params") if isinstance(call_spec.get("params"), dict) else None
    params = merge_vision_params(ctx, raw_params)
    image_name = str(params.get("image_name") or "").strip() or None
    helper_name = str(call_spec.get("helper") or "extract_text").strip() or "extract_text"

    return {
        "early_outcome": None,
        "is_vision_selection": True,
        "ctx": ctx,
        "doc": doc,
        "code": code,
        "t0": t0,
        "helper_name": helper_name,
        "params": params,
        "image_name": image_name,
        "py_data": {},
        "session_id": "",
    }


def _prepare_rps_execution(ctx: Any, doc: Any, code: str, *, data_range: str | None = None, pin_token: str | None = None) -> dict[str, Any]:
    """Main-thread document reads before the venv wait.

    Returns ``early_outcome`` when the script should not start. Otherwise the
    dict is the input for :func:`_run_prepared_rps` and :func:`_finish_rps_execution`.
    The native dialog runs only :func:`_run_prepared_rps` off the UNO thread.
    """
    from plugin.calc.bridge import calc_selection_to_a1
    from plugin.calc.python.formula_edit import parse_data_binding_text
    from plugin.calc.calc_addin_data import _resolve_python_data

    t0 = time.perf_counter()

    def _early(outcome: dict[str, Any]) -> dict[str, Any]:
        return {"early_outcome": outcome}

    # Vision does not use py_data, so the vision branch runs before
    # _resolve_python_data. Resolving Calc data first failed when a graphic
    # was selected and returned an early error for a vision helper.
    vis = _prepare_vision(ctx, doc, code, t0)
    if vis is not None:
        return vis

    def _resolve_data_ranges() -> list[str] | None:
        binding = str(data_range).strip() if data_range else ""
        if binding:
            ranges = parse_data_binding_text(binding)
            if ranges:
                return ranges
            return [binding]
        sel = calc_selection_to_a1(doc)
        return [sel] if sel else None

    py_data = None
    bindings: dict[str, Any] | None = None
    if is_calc(doc):
        drs = _resolve_data_ranges()
        if drs:
            # Pass the full address list so multi Data: bindings become data / ranges.
            py_data, err = _resolve_python_data(doc, data_range=drs, data=None)
            if err:
                return _early({"ok": False, "message": err})

    # A substring search matches comments and docstrings. Check the import
    # with script_imports_module (AST), or document bindings get prepended
    # for a mention that is not an import.
    if is_writer(doc) and (
        script_uses_run_import(code, run_name="run_text_analytics")
        or script_imports_module(code, "writeragent.scripting.text_analytics")
    ):
        from plugin.scripting.text_analytics import resolve_text_analytics_document_inputs

        call_spec = parse_run_import_call_spec(code, run_name="run_text_analytics") or {}
        helper = str(call_spec.get("helper") or "full")
        text, document_context = resolve_text_analytics_document_inputs(doc, helper)
        # topics/sentiment return list[str]. Do not str() that list.
        assert isinstance(text, (str, list)), type(text)
        assert isinstance(document_context, dict)
        bindings = {"text": text, "document_context": document_context}

    from plugin.scripting.session_manager import rps_session_id
    from plugin.calc.python.workbook_lifecycle import ensure_python_session_cleared_on_unload

    try:
        rps_sid = rps_session_id(ctx, doc)
        ensure_python_session_cleared_on_unload(ctx, doc, rps_sid)
    except Exception as e:
        log.exception("prepare Run Python Script failed")
        return _early(rps_error_outcome(str(e), t0=t0, traceback=exception_traceback(e)))

    return {
        "early_outcome": None,
        "ctx": ctx,
        "doc": doc,
        "code": code,
        "t0": t0,
        "bindings": bindings,
        "py_data": py_data,
        "session_id": rps_sid,
        "script_session_id": pin_token,
    }


def _run_prepared_rps(prepared: dict[str, Any]) -> dict[str, Any]:
    """Blocking venv IPC. Callers must not touch the document model here."""
    if prepared.get("is_vision_selection"):
        from plugin.vision.vision_runner import run_and_insert_vision_for_selection

        ctx = prepared["ctx"]
        doc = prepared["doc"]
        helper_name = prepared["helper_name"]
        params = prepared["params"]
        image_name = prepared.get("image_name")

        params_dict = dict(params)
        if image_name:
            params_dict["image_name"] = image_name

        # run_and_insert_vision_for_selection safely marshals document operations to the main thread
        # while keeping the 120s OCR wait on this background thread.
        result = run_and_insert_vision_for_selection(
            ctx, doc, helper=helper_name, params=params_dict, insert_into_document=False
        )

        return {"status": "ok", "vision_selection_result": result}

    return run_code_in_user_venv(
        prepared["ctx"],
        prepared["code"],
        data=prepared["py_data"],
        bindings=prepared.get("bindings"),
        session_id=prepared["session_id"],
        script_session_id=prepared.get("script_session_id"),
    )


def _finish_vision_execution(prepared: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Main-thread result insert for vision helpers."""
    from plugin.vision.vision_egress import insert_vision_result

    t0 = prepared["t0"]
    result = response.get("vision_selection_result") or {}

    # Perform UNO document mutations on main thread
    indiv_results = result.get("individual_results", [])
    for indiv_res in indiv_results:
        # insert_vision_result requires the image_name to be set in params for correct insertion
        # The background thread set it in the response context or we pass it
        img_name = indiv_res.get("image_name") or indiv_res.get("context", {}).get("image_name")
        insert_params = dict(prepared.get("params") or {})
        if img_name:
            insert_params["image_name"] = img_name
        insert_vision_result(prepared["ctx"], prepared["doc"], indiv_res, params=insert_params)

    # A multi-image vision run that fails on image N must still insert
    # results 1..N-1. Returning only the error dict dropped them.
    if result.get("status") == "error":
        return rps_error_outcome(str(result.get("message") or _("Vision helper failed.")), t0=t0)

    helper_name = prepared["helper_name"]
    formatted_time = format_elapsed_time(time.perf_counter() - t0)
    count = int(result.get("images_processed") or 0)
    if count > 1:
        status_ok = _("Vision '{helper}' completed. Inserted formatted HTML for {count} images. (took {time})").format(helper=helper_name, count=count, time=formatted_time)
    else:
        status_ok = _("Vision '{helper}' completed. Inserted formatted HTML. (took {time})").format(helper=helper_name, time=formatted_time)
    return rps_ok_outcome(status_ok, result=result, stdout=None)


def try_rps_post_venv(
    spec: "RpsDomainSpec",
    *,
    ctx: Any,
    doc: Any,
    result_data: Any,
    t0: float,
    stdout: str | None,
    code: str | None = None,
) -> dict[str, Any] | None:
    """Route a generic venv result through domain is_result + insert, or None."""
    if spec.post_venv_calc_only and not is_calc(doc):
        return None
    if not spec.is_result(result_data):
        return None
    insert_kwargs: dict[str, Any] = {}
    if spec.insert_kwargs_fn is not None and code:
        insert_kwargs = spec.insert_kwargs_fn(ctx, code)
    try:
        row_count = spec.insert(ctx, doc, result_data, **insert_kwargs)
    except Exception as e:
        reraise_if_disposed(e)
        return rps_insert_failed_outcome(e, t0=t0)

    # Synthetic meta for format_ok when no header was used.
    helper = ""
    if isinstance(result_data, dict):
        helper = str(result_data.get("helper") or "")
    meta = HelperScriptMeta(helper=helper, params={})
    return spec.format_ok(meta=meta, result=result_data, t0=t0, row_count=row_count, stdout=stdout)


def _finish_script_execution(prepared: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Main-thread result insert for general Python scripts."""
    from plugin.scripting.domain_registry import get_post_venv_domains
    from plugin.scripting.viz import try_insert_plot_result

    ctx = prepared["ctx"]
    doc = prepared["doc"]
    code = prepared["code"]
    t0 = prepared["t0"]
    elapsed = time.perf_counter() - t0
    formatted_time = format_elapsed_time(elapsed)

    if response.get("status") != "ok":
        error_msg = response.get("message", _("Unknown error"))
        log.error("Python script failed: %s", error_msg)
        return rps_error_outcome(str(error_msg), t0=t0)

    result_data = response.get("result")
    stdout = response.get("stdout")

    if result_data is None and not stdout:
        return {
            "ok": True,
            "status_ok_text": _("Script executed successfully, but returned no result and produced no output. (took {time})").format(time=formatted_time),
            "stdout": stdout,
            "result": result_data,
            "no_output": True,
        }

    if doc:
        try:
            # Domain-shaped results from generic venv execution (ordered registry).
            for spec in get_post_venv_domains():
                if spec.id == "viz":
                    # Viz domain result first, then raw matplotlib envelope below.
                    post = try_rps_post_venv(spec, ctx=ctx, doc=doc, result_data=result_data, t0=t0, stdout=stdout, code=code)
                    if post is not None:
                        return post
                    if try_insert_plot_result(ctx, doc, result_data):
                        return plot_insert_ok_outcome(helper="", title="Plot", t0=t0, stdout=stdout, result=result_data)
                    continue
                post = try_rps_post_venv(spec, ctx=ctx, doc=doc, result_data=result_data, t0=t0, stdout=stdout, code=code)
                if post is not None:
                    return post

            if isinstance(result_data, dict) and is_shape_tool_status_result(result_data):
                log.debug("Skipping result insert for shape tool status dict (keys=%s)", sorted(result_data.keys()))
            elif is_calc(doc):
                insert_result_into_calc(doc, ctx, result_data)
            elif is_writer(doc):
                formatted = format_result_for_writer(result_data)
                if formatted:
                    from plugin.writer.format import run_writer_mutation_with_optional_review

                    run_writer_mutation_with_optional_review(doc, ctx, lambda: insert_content_at_position(doc, ctx, formatted, "selection"))
            elif is_draw(doc):
                insert_result_into_draw(doc, ctx, result_data)
            else:
                return {"ok": False, "message": _("Unsupported document type for result insertion. (took {time})").format(time=formatted_time)}
        except Exception as e:
            reraise_if_disposed(e)
            # Logging (type/str/repr + traceback) lives in rps_insert_failed_outcome —
            # previously this catch painted the RPS dialog with no debug-log line.
            return rps_insert_failed_outcome(e, t0=t0)

    if stdout:
        log.info("Python script stdout: %s", stdout)

    return {"ok": True, "status_ok_text": _("Script executed successfully. (took {time})").format(time=formatted_time), "stdout": stdout, "result": result_data}


def _finish_rps_execution(prepared: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Main-thread result insert. *response* is the venv worker payload."""
    if prepared.get("is_vision_selection"):
        return _finish_vision_execution(prepared, response)
    return _finish_script_execution(prepared, response)


def execute_and_insert_result(ctx: Any, doc: Any, code: str, *, data_range: str | None = None) -> dict[str, Any]:
    """Run code in the user venv and insert the result into doc when possible.

    Synchronous entry used by tests. The extension UI does not call this.
    The native dialog and Monaco Run both use
    plugin.scripting.python_runner.start_native_script_run, which keeps
    the venv wait off the UNO event thread.
    """
    from plugin.scripting.session_manager import pin_script_document, release_script_document

    pin_token = pin_script_document(doc)
    try:
        prepared = _prepare_rps_execution(ctx, doc, code, data_range=data_range, pin_token=pin_token)
        early = prepared.get("early_outcome")
        if early is not None:
            return early
        try:
            response = _run_prepared_rps(prepared)
        except Exception as e:
            log.exception("execute_and_insert_result failed")
            return rps_error_outcome(str(e), t0=prepared["t0"], traceback=exception_traceback(e))
        return _finish_rps_execution(prepared, response)
    finally:
        release_script_document(pin_token)


def start_native_script_run(
    ctx: Any,
    doc: Any,
    code: str,
    *,
    on_complete: Any,
    data_range: str | None = None,
) -> None:
    """Run a script without blocking the UNO event thread.

    The Run button must not call ``execute_and_insert_result`` on the dialog
    dispatch thread. Subprocess spawn and the venv wait froze the native XDL
    dialog until the script finished. Monaco Run did the same inside the
    editor save handler. The native listener and Monaco Run both call this.
    ``data_range`` is the Monaco data-binding text; the native dialog leaves
    it empty and uses the current selection. Document prep and result insert
    stay on the main thread (``execute_on_main_thread``). Only the venv IPC
    wait runs in the background. ``on_complete`` is posted back to the main
    thread. After the worker is scheduled, ``on_complete`` is called exactly
    once on the main thread. A non-dict from preparation is an error outcome,
    not a silent return.
    """
    from plugin.framework.queue_executor import execute_on_main_thread, post_to_main_thread
    from plugin.scripting.editor_errors import exception_traceback
    from plugin.scripting.helper_domain import rps_error_outcome

    def _deliver(outcome: dict[str, Any]) -> None:
        def _apply() -> None:
            on_complete(outcome)

        post_to_main_thread(_apply)

    def _native_script_run_worker() -> None:
        from plugin.scripting.session_manager import pin_script_document, release_script_document

        pin_token = pin_script_document(doc)
        try:
            prepared: dict[str, Any] | None = None
            try:
                prepared = execute_on_main_thread(
                    _prepare_rps_execution, ctx, doc, code, data_range=data_range, pin_token=pin_token
                )
                if not isinstance(prepared, dict):
                    # Why this change fixes it: the worker still posts one error outcome. A non-dict has no script_session_id, so the pin is released by the outer finally instead.
                    log.error("native script run: prepare returned %s", type(prepared).__name__)
                    _deliver({"ok": False, "message": _("Script preparation failed.")})
                    return
                early = prepared.get("early_outcome")
                if early is not None:
                    _deliver(early)
                    return
                try:
                    response = _run_prepared_rps(prepared)
                except Exception as exc:
                    log.exception("native script run failed")
                    _deliver(rps_error_outcome(str(exc), t0=prepared["t0"], traceback=exception_traceback(exc)))
                    return
                outcome = execute_on_main_thread(_finish_rps_execution, prepared, response)
                _deliver(outcome)
            except Exception as exc:
                log.exception("native script run failed")
                _deliver({"ok": False, "message": str(exc)})
        finally:
            release_script_document(pin_token)

    run_in_background(_native_script_run_worker, name="native-run-python-script", dedicated=True)


def _monaco_editor_run_payload(outcome: dict[str, Any], run_ok_text: str, code: str = "") -> dict[str, Any]:
    """Map a native-run outcome to the Monaco saved or error frame."""
    if not outcome.get("ok"):
        message = _("Unknown error")
        traceback_text = None
        raw = outcome.get("message")
        if isinstance(raw, str) and raw:
            message = raw
        tb = outcome.get("traceback")
        if isinstance(tb, str) and tb:
            traceback_text = tb
        payload: dict[str, Any] = {
            "type": "error",
            "message": message,
            "markers": editor_error_markers(message, traceback_text or "", code),
        }
        if traceback_text:
            payload["traceback"] = traceback_text
        return payload
    status = outcome.get("status_ok_text", run_ok_text)
    if not isinstance(status, str) or not status:
        status = run_ok_text
    stdout = outcome.get("stdout")
    if isinstance(stdout, str) and stdout.strip():
        status = _("{status} ({count} line(s) printed.)").format(status=status, count=len(stdout.splitlines()))
    return {"type": "saved", "ok": True, "status_ok_text": status, "markers": []}


def _picker_template_name(name: str) -> bool:
    """True for a read-only built-in picker label such as ``[Vision] …``."""
    from plugin.scripting.document_scripts import is_picker_template_name

    return is_picker_template_name(name)


def _run_python_monaco(ctx: Any, doc: Any, *, initial_code: str, selected_script_name: str, exe: str) -> bool:
    """Open Monaco for Run Python Script. Return True when the editor session started."""
    run_ok_text = _("Script executed successfully.")
    save_ok_text = _("Script saved.")
    initial_binding = ""
    if is_calc(doc):
        from plugin.calc.bridge import calc_selection_to_a1

        initial_binding = calc_selection_to_a1(doc) or ""
    show_binding = is_calc(doc) and script_header_needs_data_binding(initial_code, doc=doc)

    def on_save(code: str, _save_as_plain: bool, data_binding: str | None = None, action: str = "run") -> dict[str, Any] | DeferredEditorResult:
        # What was wrong: reopening Tools → Run Python Script while a Run was
        # in flight started a second run, and both could insert.
        # How it happened: run_busy lived in the _run_python_monaco closure.
        # _register_load_session replaces existing.on_save for the same target
        # (a clean buffer proceeds; Don't save proceeds; Save queues
        # request_save, and the synchronous save path then activates the new
        # callback). PythonWorkerManager._io_session releases _io_lock before
        # execute returns, so _finish_rps_execution runs after the lock is
        # free. A second background run can enter the venv during that insert,
        # or wait up to _IO_LOCK_ACQUIRE_TIMEOUT_SEC (600s) if the first venv
        # call is still inside the lock.
        # Why this change fixes it: the in-flight set is keyed by document
        # RuntimeUID and is shared by every on_save for that document,
        # including a closure installed by a later menu open.
        run_key = _monaco_run_document_key(doc)
        if action != "save" and run_key in _monaco_run_in_flight:
            return {"type": "error", "message": _("A script is already running.")}
        # Save the edited code back to the currently selected script
        from plugin.scripting.document_scripts import resolve_run_script_name_config_key

        name_config_key = resolve_run_script_name_config_key(doc)
        last_name = get_config_str(name_config_key).strip()
        if not last_name:
            if action == "save":
                # What was wrong: Saving with an empty script selection returned success and cleared dirty state without saving.
                # How it happened: Blank or whitespace last_name skipped save_selected_script and fell through to the saved frame.
                # Why this change fixes it: Return an error frame on Save when no script is selected, preserving the dirty buffer.
                return {"type": "error", "message": _("No script is selected. Use New or Save As to keep this code.")}
        else:
            from plugin.scripting.document_scripts import save_selected_script

            err = save_selected_script(
                doc,
                last_name,
                code,
                allow_builtin_skip=(action != "save"),
                is_builtin_fn=_picker_template_name,
            )
            if err:
                return {"type": "error", "message": err}
        if action == "save":
            return {"type": "saved", "ok": True, "status_ok_text": save_ok_text}
        # Run must not call execute_and_insert_result on the UI thread inside
        # the editor save handler. The pipe reader marshals on_save onto the
        # UI thread and blocks until it returns, so the venv wait froze
        # LibreOffice. The library write above stays on the UI thread. The
        # venv wait uses the same prepare/finish split as the native Run
        # button. The Monaco frame is delivered when that run finishes.
        _monaco_run_in_flight.add(run_key)
        snapshot = code
        binding = data_binding

        def _start(deliver: Callable[[dict[str, Any]], None]) -> None:
            def _on_complete(outcome: dict[str, Any]) -> None:
                _monaco_run_in_flight.discard(run_key)
                deliver(_monaco_editor_run_payload(outcome, run_ok_text, snapshot))

            try:
                start_native_script_run(ctx, doc, snapshot, data_range=binding, on_complete=_on_complete)
            except Exception:
                _monaco_run_in_flight.discard(run_key)
                raise

        return DeferredEditorResult(_start)

    load_msg: dict[str, Any] = {
        "type": "load",
        "mode": "run_script",
        "language": "python",
        "code": initial_code,
        "selected_script_name": selected_script_name,
        "title": _("Run Python Script"),
        "run_label": _("Run"),
        "save_label": _("Save"),
        "close_label": _("Close"),
        "show_plain_text": False,
        "show_data_binding": show_binding,
        "data_binding": initial_binding or "",
        "data_binding_title": _("Select data range or enter A1 address (injected as data)."),
        "status_ok_text": run_ok_text,
        "saved_ok_text": save_ok_text,
        "run_script_doc": doc,
        "script_name": selected_script_name,
        "doc_url": "",
        "resource": "run_script",
    }
    from plugin.scripting.document_scripts import document_scripts_identity

    load_msg["doc_url"] = document_scripts_identity(doc)
    return launch_monaco_editor(ctx, exe=exe, load_message=load_msg, on_save=on_save)


def _report_run_python_open_failed(ctx: Any, reason: str, *, detail: str | None = None, exc: BaseException | None = None) -> None:
    from plugin.chatbot.dialogs import msgbox_with_report
    from plugin.scripting.editor_errors import failure_message

    message = failure_message(reason, detail=detail, exc=exc)
    msgbox_with_report(ctx, _("Error"), message, box_type=3, reportable=True, report_title="Run Python Script failed to open", report_extra=message if exc is None else exception_traceback(exc))


def run_python_dialog(uno_ctx: Any = None) -> None:
    """Entry point for the 'Run Python Script...' menu command."""
    if uno_ctx is None:
        uno_ctx = get_ctx()

    exe, monaco_expected = monaco_editor_available(uno_ctx)

    try:
        desktop = get_desktop(uno_ctx)
        doc = desktop.getCurrentComponent()

        from plugin.scripting.document_scripts import get_user_scripts, resolve_run_script_selection

        last_name, initial_code, _merged_scripts = resolve_run_script_selection(uno_ctx, doc, get_user_scripts())

        user_alerted = False
        if monaco_expected and exe:
            monaco_launch_ok = False
            try:
                # launch_monaco_editor confirms or flushes a dirty buffer
                # (calc cell, this script, init script, or LaTeX) before it
                # replaces the window. Cancel and a queued save both return
                # True so the native dialog stays closed.
                monaco_launch_ok = _run_python_monaco(uno_ctx, doc, initial_code=initial_code, selected_script_name=last_name, exe=exe)
            except Exception as exc:
                log.exception("run_python_dialog: editor path raised; trying native dialog")
                _report_run_python_open_failed(uno_ctx, _("Run Python Script failed to open the editor."), exc=exc)
                user_alerted = True
            else:
                if monaco_launch_ok:
                    return
                # launch_monaco_editor already reported spawn/ready/IPC failures.
                user_alerted = True

        opened, native_detail = show_python_input_dialog(uno_ctx, doc=doc)
        if opened:
            return

        log.error("run_python_dialog: native script dialog failed to open")
        if not user_alerted:
            _report_run_python_open_failed(uno_ctx, _("Could not open the built-in script dialog."), detail=native_detail)
    except Exception as exc:
        # What was wrong: Unexpected dialog open failures on the native-only path were silently swallowed without user feedback.
        # How it happened: The outer except block guarded the error dialog with if monaco_expected, skipping it when Monaco was disabled.
        # Why this change fixes it: Always call _report_run_python_open_failed so early setup errors are shown regardless of Monaco availability.
        log.exception("run_python_dialog failed")
        _report_run_python_open_failed(uno_ctx, _("An unexpected error occurred while opening Run Python Script."), exc=exc)
