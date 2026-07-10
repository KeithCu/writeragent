# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Dialog and execution logic for 'Run Python Script...' in Writer."""

import logging
import time
from typing import Any

from plugin.framework.uno_context import get_ctx, get_desktop
from plugin.framework.config import get_config, get_config_str, set_config
from plugin.framework.i18n import _
from plugin.chatbot.dialogs import msgbox
from plugin.scripting.editor_ipc import exception_traceback
from plugin.scripting.editor_host import launch_monaco_editor, monaco_editor_available, terminate_persistent_editor
from plugin.scripting.venv_worker import run_code_in_user_venv
from plugin.scripting.python_runner_ui import show_python_input_dialog
from plugin.writer.format import insert_content_at_position
from plugin.doc.document_helpers import is_calc, is_writer, is_draw
from plugin.calc.bridge import CalcBridge
from plugin.calc.manipulator import CellManipulator
from plugin.calc.address_utils import index_to_column
from plugin.scripting.payload_codec import is_dataframe_payload

log = logging.getLogger("writeragent.scripting")


def _format_list_to_table(data: list, *, headers: list | None = None) -> str:
    """Internal helper to convert a list (of dicts or lists) to an HTML table.
    If *headers* is provided, they are used for the thead (for dataframe egress).
    """
    if not data:
        return ""

    parts = []

    # Explicit headers (e.g. from dataframe payload) take precedence for order and 1-col cases.
    if headers:
        parts.append('<table border="1"><thead><tr>')
        for h in headers:
            parts.append(f"<th>{h}</th>")
        parts.append("</tr></thead><tbody>")
        # data may be list of lists (2d) or flat list (1-col series-like)
        if data and isinstance(data[0], (list, tuple)):
            for row in data:
                parts.append("<tr>")
                for cell in row:
                    parts.append(f"<td>{cell}</td>")
                parts.append("</tr>")
        else:
            for v in data:
                parts.append(f"<tr><td>{v}</td></tr>")
        parts.append("</tbody></table>")
        return "".join(parts)

    # Handle list of dicts (e.g. pandas records) -- legacy path
    if isinstance(data[0], dict):
        keys = list(data[0].keys())
        parts.append('<table border="1"><thead><tr>')
        for key in keys:
            parts.append(f"<th>{key}</th>")
        parts.append("</tr></thead><tbody>")
        for row in data:
            parts.append("<tr>")
            for key in keys:
                val = row.get(key, "")
                parts.append(f"<td>{val}</td>")
            parts.append("</tr>")
        parts.append("</tbody></table>")
        return "".join(parts)

    # Handle list of lists (table)
    if isinstance(data[0], (list, tuple)):
        parts.append('<table border="1">')
        for row in data:
            parts.append("<tr>")
            for cell in row:
                parts.append(f"<td>{cell}</td>")
            parts.append("</tr>")
        parts.append("</table>")
        return "".join(parts)

    # Fallback: list of primitives
    return "<br>".join(str(x) for x in data)



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

    if is_dataframe_payload(result):
        d = result if isinstance(result, dict) else {}
        cols = d.get("columns") or []
        data = d.get("data") or []
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
                    html_parts.append(f"<h3>{key}</h3>")
                    html_parts.append(table)
            else:
                escaped = str(val).replace("\n", "<br>")
                lower_key = str(key).lower()
                if lower_key in priority_keys:
                    html_parts.append(f"<p><b>{escaped}</b></p>")
                else:
                    html_parts.append(f"<p><b>{key}:</b> {escaped}</p>")
        
        return "\n".join(html_parts)

    # Fallback to string
    return str(result)


def insert_result_into_calc(doc: Any, uno_ctx: Any, result: Any) -> None:
    """Insert the result of a Python script into a Calc document."""
    try:
        bridge = CalcBridge(doc)
        manipulator = CellManipulator(bridge)
        
        # Determine anchor cell from selection
        controller = doc.getCurrentController()
        selection = controller.getSelection()
        
        start_col = 0
        start_row = 0
        if selection and hasattr(selection, "getRangeAddress"):
            addr = selection.getRangeAddress()
            start_col = addr.StartColumn
            start_row = addr.StartRow
        
        def write_at(col_offset, row_offset, val):
            addr = f"{index_to_column(start_col + col_offset)}{start_row + row_offset + 1}"
            manipulator.write_formula_range(addr, val)

        # Handle different result types
        current_row = 0
        
        # 1. Handle specialized dictionary results
        if isinstance(result, dict):
            # Prioritize title/summary
            title = result.get("title") or result.get("summary_text") or result.get("message")
            if title:
                write_at(0, current_row, str(title))
                current_row += 1 # Immediately below

            # Look for lists to insert as tables
            for k, v in result.items():
                if k in ("title", "summary_text", "message", "status", "result"):
                    continue
                if isinstance(v, list) and v:
                    # Convert list of dicts to list of lists if needed
                    table_data = v
                    if isinstance(v[0], dict):
                        headers = list(v[0].keys())
                        rows = [[row.get(h, "") for h in headers] for row in v]
                        table_data = [headers] + rows
                    
                    write_at(0, current_row, table_data)
                    current_row += len(table_data) # Immediately below

            # If result["result"] exists and hasn't been handled
            res_val = result.get("result")
            if res_val is not None:
                write_at(0, current_row, res_val)
        
        # 2. Handle simple lists (1D or 2D)
        elif isinstance(result, list) and result:
            table_data = result
            # write_formula_range handles 1D and 2D lists
            write_at(0, 0, table_data)
            
        # 3. Handle primitives
        else:
            write_at(0, 0, str(result))

    except Exception as e:
        log.exception("Failed to insert result into Calc")
        msgbox(uno_ctx, _("Error"), _("Failed to insert result into Calc: %s") % str(e))


def insert_result_into_draw(doc: Any, uno_ctx: Any, result: Any) -> None:
    """Insert the result of a Python script into a Draw/Impress document."""
    msgbox(uno_ctx, _("Info"), _("Result insertion into Draw/Impress is not yet supported. PRs welcome!"))
    return

    # The code below is experimental and currently disabled.
    """
    try:
        from plugin.draw.bridge import DrawBridge
        bridge = DrawBridge(doc)
        log.debug(f"insert_result_into_draw: doc={doc!r}")
        
        page = bridge.get_active_page()
        log.debug(f"insert_result_into_draw: active_page={page!r}")
        
        if page is None:
            # Try to get first page directly if bridge failed
            if hasattr(doc, "getDrawPages"):
                pages = doc.getDrawPages()
                if pages and pages.getCount() > 0:
                    page = pages.getByIndex(0)
                    log.debug(f"insert_result_into_draw: fallback to first page={page!r}")

        if page is None:
            log.error(f"insert_result_into_draw: No page found. doc services: {getattr(doc, 'getAvailableServiceNames', lambda: [])()!r}")
            msgbox(uno_ctx, _("Error"), _("No active page found in Draw/Impress."))
            return

        # Determine if we should insert a Table or a Text box
        table_data = None
        if isinstance(result, list) and result and isinstance(result[0], (list, tuple, dict)):
            table_data = result
        elif isinstance(result, dict):
            # Look for the first list of dicts/lists to use as a table
            for v in result.values():
                if isinstance(v, list) and v and isinstance(v[0], (list, tuple, dict)):
                    table_data = v
                    break

        if table_data:
            # Prepare data (headers + rows)
            if isinstance(table_data[0], dict):
                headers = list(table_data[0].keys())
                rows = [[str(row.get(h, "")) for h in headers] for row in table_data]
                final_data = [headers] + rows
            else:
                final_data = [[str(c) for c in r] for r in table_data]

            num_rows = len(final_data)
            num_cols = len(final_data[0])

            # 1. Insert as TableShape
            # We set the dimensions via properties immediately after creation
            shape = doc.createInstance("com.sun.star.drawing.TableShape")
            
            # These properties are key to setting dimensions correctly during/immediately after creation
            for name, val in [("Rows", num_rows), ("Columns", num_cols)]:
                try:
                    shape.setPropertyValue(name, val)
                except Exception:
                    pass

            page.add(shape)

            # Set a default size (15cm x 10cm) - units are 100ths of mm
            from com.sun.star.awt import Size, Point
            shape.setSize(Size(15000, 10000))
            shape.setPosition(Point(1000, 1000))
            
            # Model access (XTable)
            table = None
            if hasattr(shape, "Model"):
                table = shape.Model
            elif hasattr(shape, "Table"):
                table = shape.Table
            
            if table:
                # We assume setPropertyValue set the correct dimensions.
                for r_idx, row in enumerate(final_data):
                    for c_idx, val in enumerate(row):
                        try:
                            cell = table.getCellByPosition(c_idx, r_idx)
                            cell.getText().setString(val)
                        except Exception as e:
                            log.error(f"Error filling table cell ({r_idx}, {c_idx}): {e}")
            else:
                # Fallback to text if table model is inaccessible
                shape.setString(str(result))
        else:
            # 2. Insert as TextShape
            shape = doc.createInstance("com.sun.star.drawing.TextShape")
            page.add(shape)
            from com.sun.star.awt import Size, Point
            shape.setSize(Size(10000, 5000))
            shape.setPosition(Point(1000, 1000))
            
            # Format result as text
            if isinstance(result, (dict, list)):
                import json
                text_val = json.dumps(result, indent=2)
            else:
                text_val = str(result)
            
            shape.setString(text_val)

    except Exception as e:
        log.exception("Failed to insert result into Draw")
        msgbox(uno_ctx, _("Error"), _("Failed to insert result into Draw: %s") % str(e))
    """



def resolve_run_script_name_config_key(doc: Any) -> str:
    """Return the config key for persisting the last selected Run Python Script name for *doc*."""
    if doc:
        if is_calc(doc):
            return "last_python_script_name_calc"
        if is_writer(doc):
            return "last_python_script_name_writer"
        if is_draw(doc):
            return "last_python_script_name_draw"
    return "last_python_script_name_writer"


from plugin.scripting.helper_domain import (
    format_elapsed_time,
    plot_insert_ok_outcome,
    rps_error_outcome,
    rps_insert_failed_outcome,
)


# Re-export under legacy private names for any external imports/tests.
_plot_insert_ok_outcome = plot_insert_ok_outcome


def execute_and_insert_result(
    ctx: Any,
    doc: Any,
    code: str,
    *,
    data_range: str | None = None,
) -> dict[str, Any]:
    """Run *code* in the user venv and insert the result into *doc* when possible."""
    from plugin.calc.analysis_runner import calc_selection_to_a1, calc_tool_context
    from plugin.calc.python.formula_edit import parse_data_binding_text
    from plugin.calc.python.venv import _resolve_python_data
    from plugin.scripting.domain_registry import get_post_venv_domains, get_rps_domains, try_rps_fast_path, try_rps_post_venv
    from plugin.scripting.viz import try_insert_plot_result

    t0 = time.perf_counter()

    def _resolve_data_range() -> str | None:
        binding = str(data_range).strip() if data_range else ""
        if binding:
            ranges = parse_data_binding_text(binding)
            if ranges:
                return ranges[0]
            return binding
        return calc_selection_to_a1(doc)

    # Trusted-helper header fast paths (ordered domain registry).
    for spec in get_rps_domains():
        outcome = try_rps_fast_path(
            spec,
            ctx=ctx,
            doc=doc,
            code=code,
            t0=t0,
            resolve_data_range=_resolve_data_range,
        )
        if outcome is not None:
            return outcome

    py_data = None
    if is_calc(doc):
        dr = _resolve_data_range()
        if dr:
            tool_ctx = calc_tool_context(ctx, doc)
            py_data, err = _resolve_python_data(tool_ctx, data_range=dr, data=None)
            if err:
                return {"ok": False, "message": err}

    exec_code = code
    if is_writer(doc):
        from plugin.scripting.helper_domain import parse_run_import_call_spec, prepend_run_import_document_bindings, script_uses_run_import
        from plugin.scripting.text_analytics import resolve_text_analytics_document_inputs

        if script_uses_run_import(code, run_name="run_text_analytics"):
            call_spec = parse_run_import_call_spec(code, run_name="run_text_analytics") or {}
            helper = str(call_spec.get("helper") or "full")
            text, document_context = resolve_text_analytics_document_inputs(doc, helper)
            exec_code = prepend_run_import_document_bindings(
                code,
                bindings={"text": text, "document_context": document_context},
            )

    try:
        response = run_code_in_user_venv(ctx, exec_code, data=py_data)
        elapsed = time.perf_counter() - t0
    except Exception as e:
        log.exception("execute_and_insert_result failed")
        return rps_error_outcome(str(e), t0=t0, traceback=exception_traceback(e))

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
                        return plot_insert_ok_outcome(
                            helper="",
                            title="Plot",
                            t0=t0,
                            stdout=stdout,
                            result=result_data,
                        )
                    continue
                post = try_rps_post_venv(spec, ctx=ctx, doc=doc, result_data=result_data, t0=t0, stdout=stdout, code=code)
                if post is not None:
                    return post

            if is_calc(doc):
                insert_result_into_calc(doc, ctx, result_data)
            elif is_writer(doc):
                formatted = format_result_for_writer(result_data)
                if formatted:
                    # Review mode: record this agent-driven insertion as a reviewable tracked change.
                    from plugin.writer.edit_review import EditReviewSession, review_recording_enabled

                    review = EditReviewSession(doc, ctx, enabled=review_recording_enabled(ctx))
                    try:
                        with review:
                            review.record_mutation(lambda: insert_content_at_position(doc, ctx, formatted, "selection"))
                    finally:
                        review.cleanup()
            elif is_draw(doc):
                insert_result_into_draw(doc, ctx, result_data)
            else:
                return {"ok": False, "message": _("Unsupported document type for result insertion. (took {time})").format(time=formatted_time)}
        except Exception as e:
            return rps_insert_failed_outcome(e, t0=t0)

    if stdout:
        log.info("Python script stdout: %s", stdout)

    return {
        "ok": True,
        "status_ok_text": _("Script executed successfully. (took {time})").format(time=formatted_time),
        "stdout": stdout,
        "result": result_data,
    }


def _run_python_monaco(
    ctx: Any,
    doc: Any,
    *,
    initial_code: str,
    selected_script_name: str,
    exe: str,
) -> bool:
    """Open Monaco for Run Python Script. Return True when the editor session started."""
    from plugin.calc.analysis_runner import calc_selection_to_a1
    from plugin.scripting.domain_registry import script_header_needs_data_binding

    run_ok_text = _("Script executed successfully.")
    save_ok_text = _("Script saved.")
    initial_binding = calc_selection_to_a1(doc) if is_calc(doc) else ""
    show_binding = is_calc(doc) and script_header_needs_data_binding(initial_code, doc=doc)

    def on_save(
        code: str,
        _save_as_plain: bool,
        data_binding: str | None = None,
        action: str = "run",
    ) -> dict[str, Any]:
        # Save the edited code back to the currently selected script
        from plugin.scripting.python_runner import resolve_run_script_name_config_key
        name_config_key = resolve_run_script_name_config_key(doc)
        last_name = get_config_str(name_config_key)
        if last_name:
            from plugin.framework.config import get_config
            saved_scripts = get_config("saved_python_scripts")
            if not isinstance(saved_scripts, dict):
                saved_scripts = {}
            if last_name in saved_scripts:
                saved_scripts[last_name] = code
                set_config("saved_python_scripts", saved_scripts)
            else:
                from plugin.scripting.document_scripts import save_document_script, get_document_scripts
                doc_scripts = get_document_scripts(doc)
                if last_name in doc_scripts:
                    save_document_script(doc, last_name, code)
        if action == "save":
            return {"type": "saved", "ok": True, "status_ok_text": save_ok_text}
        outcome = execute_and_insert_result(ctx, doc, code, data_range=data_binding)
        if not outcome.get("ok"):
            return {
                "type": "error",
                "message": outcome.get("message", _("Unknown error")),
                "traceback": outcome.get("traceback"),
            }
        return {
            "type": "saved",
            "ok": True,
            "status_ok_text": outcome.get("status_ok_text", run_ok_text),
        }

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
    }
    # Ensure a fresh Monaco editor UI for each run to avoid stale button state.
    terminate_persistent_editor()
    return launch_monaco_editor(ctx, exe=exe, load_message=load_msg, on_save=on_save)


def run_python_dialog(uno_ctx: Any = None) -> None:
    """Entry point for the 'Run Python Script...' menu command."""
    if uno_ctx is None:
        uno_ctx = get_ctx()
    
    desktop = get_desktop(uno_ctx)
    doc = desktop.getCurrentComponent()

    from plugin.scripting.document_scripts import resolve_run_script_selection
    from plugin.scripting.python_runner import resolve_run_script_name_config_key

    name_config_key = resolve_run_script_name_config_key(doc)
    saved_scripts = get_config("saved_python_scripts")
    if not isinstance(saved_scripts, dict):
        saved_scripts = {}
    last_name, initial_code, _merged_scripts = resolve_run_script_selection(uno_ctx, doc, saved_scripts)

    _exe, monaco_ok = monaco_editor_available(uno_ctx)
    if monaco_ok and _exe:
        if _run_python_monaco(
            uno_ctx,
            doc,
            initial_code=initial_code,
            selected_script_name=last_name,
            exe=_exe,
        ):
            return

    show_python_input_dialog(uno_ctx, initial_text=initial_code, config_key=name_config_key, doc=doc)
