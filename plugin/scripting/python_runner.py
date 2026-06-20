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
from typing import Any, cast

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

    # Explicit headers (e.g. from dataframe payload) take precedence for order and 1-col cases.
    if headers:
        html = '<table border="1"><thead><tr>'
        for h in headers:
            html += f"<th>{h}</th>"
        html += "</tr></thead><tbody>"
        # data may be list of lists (2d) or flat list (1-col series-like)
        if data and isinstance(data[0], (list, tuple)):
            for row in data:
                html += "<tr>"
                for cell in row:
                    html += f"<td>{cell}</td>"
                html += "</tr>"
        else:
            for v in data:
                html += f"<tr><td>{v}</td></tr>"
        html += "</tbody></table>"
        return html

    # Handle list of dicts (e.g. pandas records) -- legacy path
    if isinstance(data[0], dict):
        keys = list(data[0].keys())
        html = '<table border="1"><thead><tr>'
        for key in keys:
            html += f"<th>{key}</th>"
        html += "</tr></thead><tbody>"
        for row in data:
            html += "<tr>"
            for key in keys:
                val = row.get(key, "")
                html += f"<td>{val}</td>"
            html += "</tr>"
        html += "</tbody></table>"
        return html

    # Handle list of lists (table)
    if isinstance(data[0], (list, tuple)):
        html = '<table border="1">'
        for row in data:
            html += "<tr>"
            for cell in row:
                html += f"<td>{cell}</td>"
            html += "</tr>"
        html += "</table>"
        return html

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


def format_elapsed_time(seconds: float) -> str:
    if seconds >= 60.0:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    elif seconds >= 1.0:
        return f"{seconds:.2f}s"
    else:
        ms = seconds * 1000.0
        if ms < 1.0:
            return "<1 ms"
        else:
            return f"{int(ms)} ms"


def _plot_insert_ok_outcome(
    *,
    helper: str,
    title: str,
    t0: float,
    stdout: str | None,
    result: Any,
) -> dict[str, Any]:
    formatted_time = format_elapsed_time(time.perf_counter() - t0)
    status_ok = _("Plot inserted ({title}). (took {time})").format(title=title, time=formatted_time)
    if helper:
        status_ok = _("Viz '{helper}' completed. {msg}").format(
            helper=helper,
            msg=_("Plot inserted ({title}). (took {time})").format(title=title, time=formatted_time),
        )
    return {
        "ok": True,
        "status_ok_text": status_ok,
        "stdout": stdout,
        "result": result,
    }


def _symbolic_insert_ok_outcome(
    *,
    helper: str,
    latex: str,
    t0: float,
    stdout: str | None,
    result: Any,
) -> dict[str, Any]:
    formatted_time = format_elapsed_time(time.perf_counter() - t0)
    preview = latex[:80] + ("…" if len(latex) > 80 else "")
    status_ok = _("Math '{helper}' completed. Inserted: {preview} (took {time})").format(
        helper=helper,
        preview=preview,
        time=formatted_time,
    )
    return {
        "ok": True,
        "status_ok_text": status_ok,
        "stdout": stdout,
        "result": result,
    }


def _units_insert_ok_outcome(
    *,
    helper: str,
    formatted: str,
    t0: float,
    stdout: str | None,
    result: Any,
) -> dict[str, Any]:
    formatted_time = format_elapsed_time(time.perf_counter() - t0)
    preview = formatted[:80] + ("…" if len(formatted) > 80 else "")
    status_ok = _("Units '{helper}' completed. Inserted: {preview} (took {time})").format(
        helper=helper,
        preview=preview,
        time=formatted_time,
    )
    return {
        "ok": True,
        "status_ok_text": status_ok,
        "stdout": stdout,
        "result": result,
    }


def execute_and_insert_result(
    ctx: Any,
    doc: Any,
    code: str,
    *,
    data_range: str | None = None,
) -> dict[str, Any]:
    """Run *code* in the user venv and insert the result into *doc* when possible."""
    from plugin.calc.analysis_egress import insert_analysis_result_into_calc, is_analysis_result
    from plugin.calc.analysis_runner import calc_selection_to_a1, calc_tool_context, run_trusted_analysis
    from plugin.calc.python_formula_edit import parse_data_binding_text
    from plugin.calc.venv_python import _resolve_python_data
    from plugin.framework.errors import ToolExecutionError
    from plugin.scripting.analysis import parse_analysis_script_header
    from plugin.vision.vision_egress import insert_vision_result, is_vision_result
    from plugin.vision.vision_runner import run_trusted_vision, supports_vision_manual
    from plugin.vision.vision_templates import parse_vision_script_header
    from plugin.scripting.viz import insert_viz_result_into_doc, is_viz_result, try_insert_plot_result, run_trusted_viz, supports_viz_manual, parse_viz_script_header
    from plugin.scripting.symbolic import insert_symbolic_result_into_doc, is_symbolic_result, run_trusted_symbolic, supports_symbolic_manual, parse_math_script_header
    from plugin.scripting.units import (
        insert_units_result_into_doc,
        is_units_result,
        run_trusted_units,
        supports_units_manual,
        parse_units_script_header,
        split_helper_params,
    )
    from plugin.scripting.text_analytics import (
        insert_text_analytics_result_into_doc,
        is_text_analytics_result,
        run_trusted_text_analytics,
        supports_text_analytics_manual,
        parse_text_analytics_script_header,
    )
    from plugin.calc.quant_egress import insert_quant_result_into_calc, is_quant_result
    from plugin.scripting.quant import run_trusted_quant, parse_quant_script_header
    from plugin.scripting.optimize import insert_optimize_result_into_calc, is_optimize_result, run_trusted_optimize, parse_optimize_script_header

    t0 = time.perf_counter()
    vision_meta = parse_vision_script_header(code)
    viz_meta = parse_viz_script_header(code)
    math_meta = parse_math_script_header(code)
    units_meta = parse_units_script_header(code)
    text_meta = parse_text_analytics_script_header(code)
    meta = parse_analysis_script_header(code)
    quant_meta = parse_quant_script_header(code)
    optimize_meta = parse_optimize_script_header(code)

    def _resolve_data_range() -> str | None:
        binding = str(data_range).strip() if data_range else ""
        if binding:
            ranges = parse_data_binding_text(binding)
            if ranges:
                return ranges[0]
            return binding
        return calc_selection_to_a1(doc)

    if vision_meta is not None:
        if not supports_vision_manual(doc):
            return {
                "ok": False,
                "message": _("Vision helpers require a Writer or Calc document."),
            }
        try:
            result = run_trusted_vision(ctx, doc, helper=vision_meta.helper, params=vision_meta.params)
        except ToolExecutionError as exc:
            elapsed = time.perf_counter() - t0
            err_msg = str(exc)
            formatted_time = format_elapsed_time(elapsed)
            if not ("timed out" in err_msg.lower() or "timeout" in err_msg.lower()):
                err_msg = f"{err_msg} (took {formatted_time})"
            return {"ok": False, "message": err_msg}
        except Exception as e:
            elapsed = time.perf_counter() - t0
            log.exception("execute_and_insert_result vision fast path failed")
            err_msg = str(e)
            formatted_time = format_elapsed_time(elapsed)
            if not ("timed out" in err_msg.lower() or "timeout" in err_msg.lower()):
                err_msg = f"{err_msg} (took {formatted_time})"
            return {"ok": False, "message": err_msg, "traceback": exception_traceback(e)}

        if result.get("status") == "error":
            elapsed = time.perf_counter() - t0
            formatted_time = format_elapsed_time(elapsed)
            message = str(result.get("message") or _("Vision helper failed."))
            return {"ok": False, "message": f"{message} (took {formatted_time})"}

        try:
            # Reviewable-edit recording for the Writer path lives inside
            # insert_vision_result_into_writer (vision_egress).
            insert_vision_result(ctx, doc, result, params=vision_meta.params)
        except Exception as e:
            elapsed_total = time.perf_counter() - t0
            formatted_time_total = format_elapsed_time(elapsed_total)
            return {"ok": False, "message": _("Failed to insert result: {error} (took {time})").format(error=str(e), time=formatted_time_total)}

        metrics_raw = result.get("metrics")
        metrics: dict[str, Any] = metrics_raw if isinstance(metrics_raw, dict) else {}
        line_count = metrics.get("line_count")
        if line_count is None and vision_meta.helper == "extract_structure":
            line_count = metrics.get("block_count")
        if line_count is None:
            html = str(result.get("html") or "")
            line_count = html.count("<p") + html.count("<h") + html.count("<table")
        formatted_time = format_elapsed_time(time.perf_counter() - t0)
        if vision_meta.helper == "extract_structure":
            table_count = metrics.get("table_count", 0)
            status_ok = _("Vision '{helper}' completed. Inserted HTML ({blocks} blocks, {tables} tables). (took {time})").format(
                helper=vision_meta.helper,
                blocks=line_count,
                tables=table_count,
                time=formatted_time,
            )
        else:
            status_ok = _("Vision '{helper}' completed. Inserted formatted HTML. (took {time})").format(
                helper=vision_meta.helper,
                time=formatted_time,
            )
        return {
            "ok": True,
            "status_ok_text": status_ok,
            "result": result,
        }

    if viz_meta is not None:
        if not supports_viz_manual(doc):
            return {
                "ok": False,
                "message": _("Viz helpers require a Writer or Calc document."),
            }
        dr = _resolve_data_range() if is_calc(doc) else None
        if is_calc(doc) and not dr:
            return {
                "ok": False,
                "message": _("Viz helper requires a data range. Select cells or enter a range in the Data field."),
            }
        try:
            result = run_trusted_viz(
                ctx,
                doc,
                helper=viz_meta.helper,
                params=viz_meta.params,
                data_range=dr,
            )
        except ToolExecutionError as exc:
            elapsed = time.perf_counter() - t0
            err_msg = str(exc)
            formatted_time = format_elapsed_time(elapsed)
            if not ("timed out" in err_msg.lower() or "timeout" in err_msg.lower()):
                err_msg = f"{err_msg} (took {formatted_time})"
            return {"ok": False, "message": err_msg}
        except Exception as e:
            elapsed = time.perf_counter() - t0
            log.exception("execute_and_insert_result viz fast path failed")
            err_msg = str(e)
            formatted_time = format_elapsed_time(elapsed)
            return {"ok": False, "message": f"{err_msg} (took {formatted_time})", "traceback": exception_traceback(e)}

        if result.get("status") == "error":
            elapsed = time.perf_counter() - t0
            formatted_time = format_elapsed_time(elapsed)
            message = str(result.get("message") or _("Viz helper failed."))
            return {"ok": False, "message": f"{message} (took {formatted_time})"}

        try:
            insert_viz_result_into_doc(ctx, doc, result)
        except Exception as e:
            elapsed_total = time.perf_counter() - t0
            formatted_time_total = format_elapsed_time(elapsed_total)
            return {"ok": False, "message": _("Failed to insert result: {error} (took {time})").format(error=str(e), time=formatted_time_total)}

        title = str(result.get("title") or viz_meta.helper)
        return _plot_insert_ok_outcome(
            helper=viz_meta.helper,
            title=title,
            t0=t0,
            stdout=None,
            result=result,
        )

    if math_meta is not None:
        if not supports_symbolic_manual(doc):
            return {
                "ok": False,
                "message": _("Math helpers require a Writer or Calc document."),
            }
        try:
            result = run_trusted_symbolic(
                ctx,
                doc,
                helper=math_meta.helper,
                params=math_meta.params,
            )
        except ToolExecutionError as exc:
            elapsed = time.perf_counter() - t0
            err_msg = str(exc)
            formatted_time = format_elapsed_time(elapsed)
            if not ("timed out" in err_msg.lower() or "timeout" in err_msg.lower()):
                err_msg = f"{err_msg} (took {formatted_time})"
            return {"ok": False, "message": err_msg}
        except Exception as e:
            elapsed = time.perf_counter() - t0
            log.exception("execute_and_insert_result math fast path failed")
            err_msg = str(e)
            formatted_time = format_elapsed_time(elapsed)
            return {"ok": False, "message": f"{err_msg} (took {formatted_time})", "traceback": exception_traceback(e)}

        if result.get("status") == "error":
            elapsed = time.perf_counter() - t0
            formatted_time = format_elapsed_time(elapsed)
            message = str(result.get("message") or _("Math helper failed."))
            return {"ok": False, "message": f"{message} (took {formatted_time})"}

        try:
            insert_symbolic_result_into_doc(ctx, doc, result)
        except Exception as e:
            elapsed_total = time.perf_counter() - t0
            formatted_time_total = format_elapsed_time(elapsed_total)
            return {"ok": False, "message": _("Failed to insert result: {error} (took {time})").format(error=str(e), time=formatted_time_total)}

        latex = str(result.get("latex") or result.get("text") or math_meta.helper)
        return _symbolic_insert_ok_outcome(
            helper=math_meta.helper,
            latex=latex,
            t0=t0,
            stdout=None,
            result=result,
        )

    if units_meta is not None:
        if not supports_units_manual(doc):
            return {
                "ok": False,
                "message": _("Units helpers require a Writer or Calc document."),
            }
        units_params, units_output_style = split_helper_params(units_meta.params)
        try:
            result = run_trusted_units(
                ctx,
                doc,
                helper=units_meta.helper,
                params=units_params,
            )
        except ToolExecutionError as exc:
            elapsed = time.perf_counter() - t0
            err_msg = str(exc)
            formatted_time = format_elapsed_time(elapsed)
            if not ("timed out" in err_msg.lower() or "timeout" in err_msg.lower()):
                err_msg = f"{err_msg} (took {formatted_time})"
            return {"ok": False, "message": err_msg}
        except Exception as e:
            elapsed = time.perf_counter() - t0
            log.exception("execute_and_insert_result units fast path failed")
            err_msg = str(e)
            formatted_time = format_elapsed_time(elapsed)
            return {"ok": False, "message": f"{err_msg} (took {formatted_time})", "traceback": exception_traceback(e)}

        if result.get("status") == "error":
            elapsed = time.perf_counter() - t0
            formatted_time = format_elapsed_time(elapsed)
            message = str(result.get("message") or _("Units helper failed."))
            return {"ok": False, "message": f"{message} (took {formatted_time})"}

        try:
            insert_units_result_into_doc(ctx, doc, result, output_style=units_output_style)
        except Exception as e:
            elapsed_total = time.perf_counter() - t0
            formatted_time_total = format_elapsed_time(elapsed_total)
            return {"ok": False, "message": _("Failed to insert result: {error} (took {time})").format(error=str(e), time=formatted_time_total)}

        formatted = str(result.get("formatted") or result.get("text") or units_meta.helper)
        return _units_insert_ok_outcome(
            helper=units_meta.helper,
            formatted=formatted,
            t0=t0,
            stdout=None,
            result=result,
        )

    if text_meta is not None:
        if not supports_text_analytics_manual(doc):
            return {
                "ok": False,
                "message": _("Text analytics helpers require a Writer document."),
            }
        try:
            result = run_trusted_text_analytics(
                ctx,
                doc,
                helper=text_meta.helper,
                params=text_meta.params,
            )
        except ToolExecutionError as exc:
            elapsed = time.perf_counter() - t0
            err_msg = str(exc)
            formatted_time = format_elapsed_time(elapsed)
            if not ("timed out" in err_msg.lower() or "timeout" in err_msg.lower()):
                err_msg = f"{err_msg} (took {formatted_time})"
            return {"ok": False, "message": err_msg}
        except Exception as e:
            elapsed = time.perf_counter() - t0
            log.exception("execute_and_insert_result text_analytics fast path failed")
            err_msg = str(e)
            formatted_time = format_elapsed_time(elapsed)
            return {"ok": False, "message": f"{err_msg} (took {formatted_time})", "traceback": exception_traceback(e)}

        if result.get("status") == "error":
            elapsed = time.perf_counter() - t0
            formatted_time = format_elapsed_time(elapsed)
            message = str(result.get("message") or _("Text analytics helper failed."))
            return {"ok": False, "message": f"{message} (took {formatted_time})"}

        try:
            insert_text_analytics_result_into_doc(ctx, doc, result)
        except Exception as e:
            elapsed_total = time.perf_counter() - t0
            formatted_time_total = format_elapsed_time(elapsed_total)
            return {"ok": False, "message": _("Failed to insert result: {error} (took {time})").format(error=str(e), time=formatted_time_total)}

        # Use a short title for status
        title = text_meta.helper
        formatted_time = format_elapsed_time(time.perf_counter() - t0)
        return {
            "ok": True,
            "status_ok_text": _("Text analytics '{helper}' completed. (took {time})").format(
                helper=title, time=formatted_time
            ),
            "result": result,
        }

    if quant_meta is not None and is_calc(doc):
        dr = _resolve_data_range()
        if not dr and quant_meta.helper != "fetch_historical_data":
            return {
                "ok": False,
                "message": _("Quant helper requires a data range. Select cells or enter a range in the Data field."),
            }
        try:
            result = run_trusted_quant(ctx, doc, helper=quant_meta.helper, params=quant_meta.params, data_range=dr)
        except ToolExecutionError as exc:
            elapsed = time.perf_counter() - t0
            err_msg = str(exc)
            formatted_time = format_elapsed_time(elapsed)
            if not ("timed out" in err_msg.lower() or "timeout" in err_msg.lower()):
                err_msg = f"{err_msg} (took {formatted_time})"
            return {"ok": False, "message": err_msg}
        except Exception as e:
            elapsed = time.perf_counter() - t0
            log.exception("execute_and_insert_result quant fast path failed")
            err_msg = str(e)
            formatted_time = format_elapsed_time(elapsed)
            return {"ok": False, "message": f"{err_msg} (took {formatted_time})", "traceback": exception_traceback(e)}

        if result.get("status") == "error":
            elapsed = time.perf_counter() - t0
            formatted_time = format_elapsed_time(elapsed)
            message = str(result.get("message") or _("Quant failed."))
            return {"ok": False, "message": f"{message} (took {formatted_time})"}

        try:
            row_count = insert_quant_result_into_calc(doc, ctx, result)
        except Exception as e:
            elapsed_total = time.perf_counter() - t0
            formatted_time_total = format_elapsed_time(elapsed_total)
            return {"ok": False, "message": _("Failed to insert result: {error} (took {time})").format(error=str(e), time=formatted_time_total)}

        formatted_time = format_elapsed_time(time.perf_counter() - t0)
        return {
            "ok": True,
            "status_ok_text": _("Quant '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                helper=quant_meta.helper,
                rows=row_count,
                time=formatted_time,
            ),
            "result": result,
        }

    if optimize_meta is not None and is_calc(doc):
        dr = _resolve_data_range()
        if not dr:
            return {
                "ok": False,
                "message": _("Optimization helper requires a data range. Select cells or enter a range in the Data field."),
            }
        try:
            result = run_trusted_optimize(ctx, doc, helper=optimize_meta.helper, params=optimize_meta.params, data_range=dr)
        except ToolExecutionError as exc:
            elapsed = time.perf_counter() - t0
            err_msg = str(exc)
            formatted_time = format_elapsed_time(elapsed)
            if not ("timed out" in err_msg.lower() or "timeout" in err_msg.lower()):
                err_msg = f"{err_msg} (took {formatted_time})"
            return {"ok": False, "message": err_msg}
        except Exception as e:
            elapsed = time.perf_counter() - t0
            log.exception("execute_and_insert_result optimize fast path failed")
            err_msg = str(e)
            formatted_time = format_elapsed_time(elapsed)
            return {"ok": False, "message": f"{err_msg} (took {formatted_time})", "traceback": exception_traceback(e)}

        if result.get("status") == "error":
            elapsed = time.perf_counter() - t0
            formatted_time = format_elapsed_time(elapsed)
            message = str(result.get("message") or _("Optimization failed."))
            return {"ok": False, "message": f"{message} (took {formatted_time})"}

        try:
            row_count = insert_optimize_result_into_calc(doc, ctx, result)
        except Exception as e:
            elapsed_total = time.perf_counter() - t0
            formatted_time_total = format_elapsed_time(elapsed_total)
            return {"ok": False, "message": _("Failed to insert result: {error} (took {time})").format(error=str(e), time=formatted_time_total)}

        formatted_time = format_elapsed_time(time.perf_counter() - t0)
        return {
            "ok": True,
            "status_ok_text": _("Optimize '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                helper=optimize_meta.helper,
                rows=row_count,
                time=formatted_time,
            ),
            "result": result,
        }

    if meta is not None and is_calc(doc):
        dr = _resolve_data_range()
        if not dr:
            return {
                "ok": False,
                "message": _("Analysis helper requires a data range. Select cells or enter a range in the Data field."),
            }
        try:
            result = run_trusted_analysis(ctx, doc, helper=meta.helper, params=meta.params, data_range=dr)
        except ToolExecutionError as exc:
            elapsed = time.perf_counter() - t0
            err_msg = str(exc)
            formatted_time = format_elapsed_time(elapsed)
            if not ("timed out" in err_msg.lower() or "timeout" in err_msg.lower()):
                err_msg = f"{err_msg} (took {formatted_time})"
            return {"ok": False, "message": err_msg}
        except Exception as e:
            elapsed = time.perf_counter() - t0
            log.exception("execute_and_insert_result analysis fast path failed")
            err_msg = str(e)
            formatted_time = format_elapsed_time(elapsed)
            return {"ok": False, "message": f"{err_msg} (took {formatted_time})", "traceback": exception_traceback(e)}

        if result.get("status") == "error":
            elapsed = time.perf_counter() - t0
            formatted_time = format_elapsed_time(elapsed)
            message = str(result.get("message") or _("Analysis failed."))
            return {"ok": False, "message": f"{message} (took {formatted_time})"}

        try:
            row_count = insert_analysis_result_into_calc(doc, ctx, result)
        except Exception as e:
            elapsed_total = time.perf_counter() - t0
            formatted_time_total = format_elapsed_time(elapsed_total)
            return {"ok": False, "message": _("Failed to insert result: {error} (took {time})").format(error=str(e), time=formatted_time_total)}

        formatted_time = format_elapsed_time(time.perf_counter() - t0)
        return {
            "ok": True,
            "status_ok_text": _("Analysis '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                helper=meta.helper,
                rows=row_count,
                time=formatted_time,
            ),
            "result": result,
        }

    py_data = None
    if is_calc(doc):
        dr = _resolve_data_range()
        if dr:
            tool_ctx = calc_tool_context(ctx, doc)
            py_data, err = _resolve_python_data(tool_ctx, data_range=dr, data=None)
            if err:
                return {"ok": False, "message": err}

    try:
        response = run_code_in_user_venv(ctx, code, data=py_data)
        elapsed = time.perf_counter() - t0
    except Exception as e:
        elapsed = time.perf_counter() - t0
        log.exception("execute_and_insert_result failed")
        err_msg = str(e)
        formatted_time = format_elapsed_time(elapsed)
        if not ("timed out" in err_msg.lower() or "timeout" in err_msg.lower()):
            err_msg = f"{err_msg} (took {formatted_time})"
        return {"ok": False, "message": err_msg, "traceback": exception_traceback(e)}

    formatted_time = format_elapsed_time(elapsed)

    if response.get("status") != "ok":
        error_msg = response.get("message", _("Unknown error"))
        log.error("Python script failed: %s", error_msg)
        if not ("timed out" in error_msg.lower() or "timeout" in error_msg.lower()):
            error_msg = f"{error_msg} (took {formatted_time})"
        return {"ok": False, "message": error_msg}

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
            if is_symbolic_result(result_data):
                sym_result = cast("dict[str, Any]", result_data)
                insert_symbolic_result_into_doc(ctx, doc, sym_result)
                latex = str(sym_result.get("latex") or sym_result.get("text") or sym_result.get("helper") or "")
                helper = str(sym_result.get("helper") or "")
                return _symbolic_insert_ok_outcome(
                    helper=helper,
                    latex=latex,
                    t0=t0,
                    stdout=stdout,
                    result=result_data,
                )
            if is_units_result(result_data):
                units_result = cast("dict[str, Any]", result_data)
                insert_units_result_into_doc(ctx, doc, units_result)
                formatted = str(units_result.get("formatted") or units_result.get("text") or units_result.get("helper") or "")
                helper = str(units_result.get("helper") or "")
                return _units_insert_ok_outcome(
                    helper=helper,
                    formatted=formatted,
                    t0=t0,
                    stdout=stdout,
                    result=result_data,
                )
            if is_text_analytics_result(result_data):
                ta_result = cast("dict[str, Any]", result_data)
                insert_text_analytics_result_into_doc(ctx, doc, ta_result)
                formatted_time = format_elapsed_time(time.perf_counter() - t0)
                helper = str(ta_result.get("helper") or ta_result.get("result", {}).get("meta", {}).get("model") or "text")
                return {
                    "ok": True,
                    "status_ok_text": _("Text analytics '{helper}' completed. (took {time})").format(
                        helper=helper, time=formatted_time
                    ),
                    "stdout": stdout,
                    "result": result_data,
                }
            if is_viz_result(result_data):
                viz_result = cast("dict[str, Any]", result_data)
                insert_viz_result_into_doc(ctx, doc, viz_result)
                title = str(viz_result.get("title") or viz_result.get("helper") or "Plot")
                helper = str(viz_result.get("helper") or "")
                return _plot_insert_ok_outcome(
                    helper=helper,
                    title=title,
                    t0=t0,
                    stdout=stdout,
                    result=result_data,
                )
            if try_insert_plot_result(ctx, doc, result_data):
                return _plot_insert_ok_outcome(
                    helper="",
                    title="Plot",
                    t0=t0,
                    stdout=stdout,
                    result=result_data,
                )
            if isinstance(result_data, dict) and is_vision_result(result_data):
                insert_vision_result(ctx, doc, result_data)
                formatted_time = format_elapsed_time(time.perf_counter() - t0)
                helper = str(result_data.get("helper") or "vision")
                return {
                    "ok": True,
                    "status_ok_text": _("Vision '{helper}' completed. Inserted formatted HTML. (took {time})").format(
                        helper=helper,
                        time=formatted_time,
                    ),
                    "stdout": stdout,
                    "result": result_data,
                }
            if is_calc(doc):
                if isinstance(result_data, dict) and is_analysis_result(result_data):
                    row_count = insert_analysis_result_into_calc(doc, ctx, result_data)
                    formatted_time = format_elapsed_time(time.perf_counter() - t0)
                    helper = str(result_data.get("helper") or "analysis")
                    return {
                        "ok": True,
                        "status_ok_text": _("Analysis '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                            helper=helper,
                            rows=row_count,
                            time=formatted_time,
                        ),
                        "stdout": stdout,
                        "result": result_data,
                    }
                if isinstance(result_data, dict) and is_quant_result(result_data):
                    row_count = insert_quant_result_into_calc(doc, ctx, result_data)
                    formatted_time = format_elapsed_time(time.perf_counter() - t0)
                    helper = str(result_data.get("helper") or "quant")
                    return {
                        "ok": True,
                        "status_ok_text": _("Quant '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                            helper=helper,
                            rows=row_count,
                            time=formatted_time,
                        ),
                        "stdout": stdout,
                        "result": result_data,
                    }
                if isinstance(result_data, dict) and is_optimize_result(result_data):
                    row_count = insert_optimize_result_into_calc(doc, ctx, result_data)
                    formatted_time = format_elapsed_time(time.perf_counter() - t0)
                    helper = str(result_data.get("helper") or "optimize")
                    return {
                        "ok": True,
                        "status_ok_text": _("Optimize '{helper}' completed. Wrote {rows} rows. (took {time})").format(
                            helper=helper,
                            rows=row_count,
                            time=formatted_time,
                        ),
                        "stdout": stdout,
                        "result": result_data,
                    }
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
            elapsed_total = time.perf_counter() - t0
            formatted_time_total = format_elapsed_time(elapsed_total)
            return {"ok": False, "message": _("Failed to insert result: {error} (took {time})").format(error=str(e), time=formatted_time_total)}

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
    from plugin.scripting.analysis import parse_analysis_script_header
    from plugin.scripting.viz import parse_viz_script_header
    from plugin.scripting.quant import parse_quant_script_header
    from plugin.scripting.optimize import parse_optimize_script_header

    run_ok_text = _("Script executed successfully.")
    save_ok_text = _("Script saved.")
    initial_binding = calc_selection_to_a1(doc) if is_calc(doc) else ""
    show_binding = False
    if is_calc(doc):
        show_binding = bool(parse_analysis_script_header(initial_code) or parse_viz_script_header(initial_code) or parse_quant_script_header(initial_code) or parse_optimize_script_header(initial_code))

    def on_save(
        code: str,
        _save_as_plain: bool,
        data_binding: str | None = None,
        action: str = "run",
    ) -> dict[str, Any]:
        # Save the edited code back to the currently selected script
        from plugin.scripting.python_runner import resolve_run_script_name_config_key
        name_config_key = resolve_run_script_name_config_key(doc)
        last_name = get_config_str(ctx, name_config_key)
        if last_name:
            from plugin.framework.config import get_config
            saved_scripts = get_config(ctx, "saved_python_scripts")
            if not isinstance(saved_scripts, dict):
                saved_scripts = {}
            if last_name in saved_scripts:
                saved_scripts[last_name] = code
                set_config(ctx, "saved_python_scripts", saved_scripts)
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
    saved_scripts = get_config(uno_ctx, "saved_python_scripts")
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
