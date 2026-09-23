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
"""

import html as html_mod
import logging
import time
from typing import Any

from plugin.framework.uno_context import get_ctx, get_desktop
from plugin.framework.config import get_config_str
from plugin.framework.i18n import _
from plugin.chatbot.dialogs import msgbox
from plugin.scripting.editor_ipc import exception_traceback
from plugin.scripting.editor_host import launch_monaco_editor, monaco_open_expected
from plugin.scripting.venv_worker import run_code_in_user_venv
from plugin.scripting.python_runner_ui import show_python_input_dialog
from plugin.writer.format import insert_content_at_position
from plugin.doc.doc_type import is_calc, is_writer, is_draw
from plugin.calc.address_utils import index_to_column
from plugin.scripting.payload_codec import is_dataframe_payload
from plugin.scripting.helper_domain import (
    format_elapsed_time,
    plot_insert_ok_outcome,
    rps_error_outcome,
    rps_insert_failed_outcome,
    rps_ok_outcome,
)

log = logging.getLogger("writeragent.scripting")


def _html_insert_text(value: Any) -> str:
    """Escape a script result so it stays text after Writer/Calc HTML insert.

    ``insert_content_at_position`` and ``insert_cell_html_rich`` call
    ``html.unescape`` before the StarWriter HTML filter, so a single
    ``html.escape`` is undone and ``<`` becomes markup again. Double-escape
    so a literal ``<`` survives as text (``&amp;lt;`` → ``&lt;`` → ``<``).
    """
    return html_mod.escape(html_mod.escape(str(value)))


def _format_list_to_table(data: list[Any], *, headers: list[Any] | None = None) -> str:
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
    if isinstance(data[0], dict):
        keys = list(data[0].keys())
        parts.append('<table border="1"><thead><tr>')
        for key in keys:
            parts.append(f"<th>{_html_insert_text(key)}</th>")
        parts.append("</tr></thead><tbody>")
        for row in data:
            parts.append("<tr>")
            for key in keys:
                val = row.get(key, "")
                parts.append(f"<td>{_html_insert_text(val)}</td>")
            parts.append("</tr>")
        parts.append("</tbody></table>")
        return "".join(parts)

    # Handle list of lists (table)
    if isinstance(data[0], (list, tuple)):
        parts.append('<table border="1">')
        for row in data:
            parts.append("<tr>")
            for cell in row:
                parts.append(f"<td>{_html_insert_text(cell)}</td>")
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
    try:
        if result is None:
            return
        if is_shape_tool_status_result(result):
            log.debug(
                "Skipping Calc result insert for shape tool status dict (keys=%s)",
                sorted(result.keys()) if isinstance(result, dict) else type(result).__name__,
            )
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
                from plugin.draw.tables import _ensure_table_dims, fill_table_cells

                try:
                    _ensure_table_dims(table, num_rows, num_cols)
                    fill_table_cells(table, final_data)
                except Exception:
                    log.exception("Error filling table cells")
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
    from plugin.calc.calc_addin_data import _resolve_python_data
    from plugin.scripting.domain_registry import get_post_venv_domains, try_rps_post_venv
    from plugin.scripting.viz import try_insert_plot_result

    t0 = time.perf_counter()

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
    if is_calc(doc):
        drs = _resolve_data_ranges()
        if drs:
            tool_ctx = calc_tool_context(ctx, doc)
            # Pass the full address list so multi Data: bindings become data / ranges.
            py_data, err = _resolve_python_data(tool_ctx, data_range=drs, data=None)
            if err:
                return {"ok": False, "message": err}

    exec_code = code
    bindings: dict[str, Any] | None = None
    from plugin.scripting.helper_domain import parse_run_import_call_spec, script_uses_run_import

    if is_writer(doc) and (script_uses_run_import(code, run_name="run_text_analytics") or "writeragent.scripting.text_analytics" in code):
        from plugin.scripting.helper_domain import prepend_run_import_document_bindings
        from plugin.scripting.text_analytics import resolve_text_analytics_document_inputs

        call_spec = parse_run_import_call_spec(code, run_name="run_text_analytics") or {}
        helper = str(call_spec.get("helper") or "full")
        text, document_context = resolve_text_analytics_document_inputs(doc, helper)
        exec_code = prepend_run_import_document_bindings(
            code,
            bindings={"text": str(text), "document_context": document_context if isinstance(document_context, dict) else {}},
        )

    if "run_vision" in code and script_uses_run_import(code, run_name="run_vision"):
        from plugin.framework.errors import ToolExecutionError
        from plugin.vision.vision_common import merge_vision_params
        from plugin.vision.vision_runner import resolve_vision_image_bytes, run_and_insert_vision_for_selection, supports_vision_manual

        if not supports_vision_manual(doc):
            return {"ok": False, "message": _("Vision helpers require a Writer or Calc document.")}
        call_spec = parse_run_import_call_spec(code, run_name="run_vision") or {}
        raw_params = call_spec.get("params") if isinstance(call_spec.get("params"), dict) else None
        params = merge_vision_params(ctx, raw_params)
        image_name = str(params.get("image_name") or "").strip() or None
        helper_name = str(call_spec.get("helper") or "extract_text").strip() or "extract_text"

        # Writer selection with discovered graphic(s): host OCR+insert by name.
        # Covers multi-select and text ranges (even one image) — selection export cannot.
        if not image_name and is_writer(doc):
            from plugin.doc.visual_helpers import graphic_objects_in_selection

            discovered = graphic_objects_in_selection(doc)
            if discovered:
                try:
                    result = run_and_insert_vision_for_selection(
                        ctx,
                        doc,
                        helper=helper_name,
                        params=params,
                        insert_into_document=True,
                    )
                except ToolExecutionError as exc:
                    return rps_error_outcome(str(exc), t0=t0)
                if result.get("status") == "error":
                    return rps_error_outcome(str(result.get("message") or _("Vision helper failed.")), t0=t0)
                formatted_time = format_elapsed_time(time.perf_counter() - t0)
                count = int(result.get("images_processed") or len(discovered))
                if count > 1:
                    status_ok = _(
                        "Vision '{helper}' completed. Inserted formatted HTML for {count} images. (took {time})"
                    ).format(helper=helper_name, count=count, time=formatted_time)
                else:
                    status_ok = _("Vision '{helper}' completed. Inserted formatted HTML. (took {time})").format(
                        helper=helper_name,
                        time=formatted_time,
                    )
                return rps_ok_outcome(status_ok, result=result, stdout=None)

        try:
            bindings = {"image": resolve_vision_image_bytes(ctx, doc, image_name=image_name)}
        except ToolExecutionError as exc:
            return rps_error_outcome(str(exc), t0=t0)

    try:
        from plugin.scripting.session_manager import rps_session_id

        response = run_code_in_user_venv(
            ctx,
            exec_code,
            data=py_data,
            bindings=bindings,
            session_id=rps_session_id(ctx, doc),
        )
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
                if is_shape_tool_status_result(result_data):
                    log.debug(
                        "Skipping Calc result insert for shape tool status dict (keys=%s)",
                        sorted(result_data.keys()) if isinstance(result_data, dict) else type(result_data).__name__,
                    )
                else:
                    insert_result_into_calc(doc, ctx, result_data)
            elif is_writer(doc):
                if is_shape_tool_status_result(result_data):
                    log.debug(
                        "Skipping Writer result insert for shape tool status dict (keys=%s)",
                        sorted(result_data.keys()) if isinstance(result_data, dict) else type(result_data).__name__,
                    )
                else:
                    formatted = format_result_for_writer(result_data)
                    if formatted:
                        from plugin.writer.format import run_writer_mutation_with_optional_review

                        run_writer_mutation_with_optional_review(
                            doc,
                            ctx,
                            lambda: insert_content_at_position(doc, ctx, formatted, "selection"),
                        )
            elif is_draw(doc):
                insert_result_into_draw(doc, ctx, result_data)
            else:
                return {"ok": False, "message": _("Unsupported document type for result insertion. (took {time})").format(time=formatted_time)}
        except Exception as e:
            # Logging (type/str/repr + traceback) lives in rps_insert_failed_outcome —
            # previously this catch painted the RPS dialog with no debug-log line.
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
    from plugin.scripting.domain_registry import script_header_needs_data_binding

    run_ok_text = _("Script executed successfully.")
    save_ok_text = _("Script saved.")
    initial_binding = ""
    if is_calc(doc):
        from plugin.calc.analysis_runner import calc_selection_to_a1

        initial_binding = calc_selection_to_a1(doc) or ""
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
            from plugin.scripting.document_scripts import (
                get_document_scripts,
                get_user_scripts,
                parse_document_script_display_name,
                save_document_script,
                save_user_script,
            )

            if last_name in get_user_scripts():
                save_user_script(last_name, code)
            else:
                doc_scripts = get_document_scripts(doc)
                real_doc_name = parse_document_script_display_name(last_name) or last_name
                if real_doc_name in doc_scripts:
                    save_document_script(doc, real_doc_name, code)
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
        "script_name": selected_script_name,
        "doc_url": "",
        "resource": "run_script",
    }
    try:
        from plugin.scripting.document_scripts import document_scripts_identity

        load_msg["doc_url"] = document_scripts_identity(doc)
    except Exception:
        log.debug("python_runner: doc_url for session target failed", exc_info=True)
    return launch_monaco_editor(ctx, exe=exe, load_message=load_msg, on_save=on_save)


def _report_run_python_open_failed(
    ctx: Any,
    reason: str,
    *,
    detail: str | None = None,
    exc: BaseException | None = None,
) -> None:
    from plugin.chatbot.dialogs import msgbox_with_report
    from plugin.scripting.editor_ipc import exception_traceback, failure_message

    full_detail = "\n\n".join(filter(None, [(detail or "").strip(), exception_traceback(exc).rstrip() if exc is not None else ""]))
    message = failure_message(reason, detail=full_detail or None)
    msgbox_with_report(
        ctx,
        _("Error"),
        message,
        box_type=3,
        reportable=True,
        report_title="Run Python Script failed to open",
        report_extra=message if exc is None else exception_traceback(exc),
    )


def run_python_dialog(uno_ctx: Any = None) -> None:
    """Entry point for the 'Run Python Script...' menu command."""
    if uno_ctx is None:
        uno_ctx = get_ctx()

    exe, monaco_expected = monaco_open_expected(uno_ctx)

    try:
        desktop = get_desktop(uno_ctx)
        doc = desktop.getCurrentComponent()

        from plugin.scripting.document_scripts import get_user_scripts, resolve_run_script_selection

        last_name, initial_code, _merged_scripts = resolve_run_script_selection(uno_ctx, doc, get_user_scripts())

        user_alerted = False
        if monaco_expected and exe:
            monaco_launch_ok = False
            try:
                monaco_launch_ok = _run_python_monaco(
                    uno_ctx,
                    doc,
                    initial_code=initial_code,
                    selected_script_name=last_name,
                    exe=exe,
                )
            except Exception as exc:
                log.exception("run_python_dialog: Monaco path raised; trying native dialog")
                _report_run_python_open_failed(
                    uno_ctx,
                    _("Run Python Script failed to open the Monaco editor."),
                    exc=exc,
                )
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
            _report_run_python_open_failed(
                uno_ctx,
                _("Could not open the built-in script dialog."),
                detail=native_detail,
            )
    except Exception as exc:
        log.exception("run_python_dialog failed")
        if monaco_expected:
            _report_run_python_open_failed(
                uno_ctx,
                _("An unexpected error occurred while opening Run Python Script."),
                exc=exc,
            )
