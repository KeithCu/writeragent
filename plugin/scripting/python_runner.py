# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Dialog and execution logic for 'Run Python Script...' in Writer."""

import logging
from typing import Any, cast
import uno
import unohelper
from com.sun.star.awt import XActionListener

from plugin.framework.uno_context import get_ctx, get_desktop
from plugin.framework.config import get_config_str, set_config
from plugin.framework.i18n import _
from plugin.chatbot.dialogs import add_dialog_label, add_dialog_edit, add_dialog_button, msgbox
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.editor_ipc import exception_traceback
from plugin.scripting.editor_host import launch_monaco_editor, monaco_editor_available
from plugin.scripting.venv_worker import run_code_in_user_venv, warm_venv_worker
from plugin.writer.format import insert_content_at_position
from plugin.doc.document_helpers import is_calc, is_writer, is_draw
from plugin.calc.bridge import CalcBridge
from plugin.calc.manipulator import CellManipulator
from plugin.calc.address_utils import index_to_column

log = logging.getLogger("writeragent.scripting")


def show_python_input_dialog(ctx: Any, initial_text: str = "") -> str | None:
    """Show a modal multiline dialog for entering Python code.
    
    Returns the code string if OK is clicked, else None.
    """
    try:
        desktop = get_desktop(ctx)
        frame = desktop.getCurrentFrame()
        if frame is None:
            return None
        parent_window = frame.getContainerWindow()
        if parent_window is None:
            return None

        smgr = ctx.getServiceManager()
        dlg_model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
        dlg_model.Title = _("Run Python Script")
        dlg_model.Width = 350
        dlg_model.Height = 220

        add_dialog_label(dlg_model, "InstructionLbl", _("Enter Python code to execute in the user virtual environment.\nAssign the result to the 'result' variable."), 8, 8, 334, 20)
        edit = add_dialog_edit(dlg_model, "CodeEdit", initial_text, 8, 32, 334, 150)
        edit.MultiLine = True
        edit.VScroll = True
        # Use a monospaced font if possible
        fd = cast("Any", uno.createUnoStruct("com.sun.star.awt.FontDescriptor"))
        fd.Name = "Courier New"
        edit.FontDescriptor = fd

        add_dialog_button(dlg_model, "BtnRun", _("Run"), 220, 190, 60, 14)
        add_dialog_button(dlg_model, "BtnCancel", _("Cancel"), 286, 190, 56, 14)

        dlg = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
        dlg.setModel(dlg_model)
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        dlg.createPeer(toolkit, parent_window)

        _outcome: list[str | None] | None = None

        class _RunListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                nonlocal _outcome
                try:
                    ec = dlg.getControl("CodeEdit")
                    t = (ec.getModel().Text or "").strip()
                except Exception:
                    t = ""
                _outcome = [t]
                dlg.endDialog(1)

            def disposing(self, Source):
                pass

        class _CancelListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                nonlocal _outcome
                _outcome = [None]
                dlg.endDialog(0)

            def disposing(self, Source):
                pass

        dlg.getControl("BtnRun").addActionListener(_RunListener())
        dlg.getControl("BtnCancel").addActionListener(_CancelListener())
        
        # Set focus to the edit control
        dlg.getControl("CodeEdit").setFocus()
        
        dlg.execute()
        dlg.dispose()
        
        if _outcome is None:
            return None
        return _outcome[0]
    except Exception:
        log.exception("show_python_input_dialog failed")
        return None


def _format_list_to_table(data: list) -> str:
    """Internal helper to convert a list (of dicts or lists) to an HTML table."""
    if not data:
        return ""

    # Handle list of dicts (e.g. pandas records)
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


def resolve_run_script_config_key(doc: Any) -> str:
    """Return the config key for persisting Run Python Script code for *doc*."""
    if doc:
        if is_calc(doc):
            return "last_python_script_calc"
        if is_writer(doc):
            return "last_python_script_writer"
        if is_draw(doc):
            return "last_python_script_draw"
    return "last_python_script"


def execute_and_insert_result(ctx: Any, doc: Any, code: str) -> dict[str, Any]:
    """Run *code* in the user venv and insert the result into *doc* when possible."""
    try:
        response = run_code_in_user_venv(ctx, code)
    except Exception as e:
        log.exception("execute_and_insert_result failed")
        return {"ok": False, "message": str(e), "traceback": exception_traceback(e)}

    if response.get("status") != "ok":
        error_msg = response.get("message", _("Unknown error"))
        log.error("Python script failed: %s", error_msg)
        return {"ok": False, "message": error_msg}

    result_data = response.get("result")
    stdout = response.get("stdout")

    if result_data is None and not stdout:
        return {
            "ok": True,
            "status_ok_text": _("Script executed successfully, but returned no result and produced no output."),
            "stdout": stdout,
            "result": result_data,
        }

    if doc:
        if is_calc(doc):
            insert_result_into_calc(doc, ctx, result_data)
        elif is_writer(doc):
            formatted = format_result_for_writer(result_data)
            if formatted:
                insert_content_at_position(doc, ctx, formatted, "selection")
        elif is_draw(doc):
            insert_result_into_draw(doc, ctx, result_data)
        else:
            return {"ok": False, "message": _("Unsupported document type for result insertion.")}

    if stdout:
        log.info("Python script stdout: %s", stdout)

    return {
        "ok": True,
        "status_ok_text": _("Script executed successfully."),
        "stdout": stdout,
        "result": result_data,
    }


def _run_python_monaco(ctx: Any, doc: Any, *, config_key: str, initial_code: str, exe: str) -> bool:
    """Open Monaco for Run Python Script. Return True when the editor session started."""
    run_ok_text = _("Script executed successfully.")
    save_ok_text = _("Script saved.")

    def on_save(
        code: str,
        _save_as_plain: bool,
        _data_binding: str | None = None,
        action: str = "run",
    ) -> dict[str, Any]:
        set_config(ctx, config_key, code)
        if action == "save":
            return {"type": "saved", "ok": True, "status_ok_text": save_ok_text}
        outcome = execute_and_insert_result(ctx, doc, code)
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
        "title": _("Run Python Script"),
        "run_label": _("Run"),
        "save_label": _("Save"),
        "close_label": _("Close"),
        "show_plain_text": False,
        "show_data_binding": False,
        "status_ok_text": run_ok_text,
        "saved_ok_text": save_ok_text,
    }
    return launch_monaco_editor(ctx, exe=exe, load_message=load_msg, on_save=on_save)


def run_python_dialog(uno_ctx: Any = None) -> None:
    """Entry point for the 'Run Python Script...' menu command."""
    if uno_ctx is None:
        uno_ctx = get_ctx()
    
    desktop = get_desktop(uno_ctx)
    doc = desktop.getCurrentComponent()

    config_key = resolve_run_script_config_key(doc)

    # Load last script from config
    initial_code = get_config_str(uno_ctx, config_key)

    _exe, monaco_ok = monaco_editor_available(uno_ctx)
    if monaco_ok and _exe:
        run_in_background(warm_venv_worker, uno_ctx, name="warm-venv-worker")
        if _run_python_monaco(uno_ctx, doc, config_key=config_key, initial_code=initial_code, exe=_exe):
            return

    code = show_python_input_dialog(uno_ctx, initial_text=initial_code)
    if not code:
        return

    # Save the script to config for next time
    set_config(uno_ctx, config_key, code)

    try:
        outcome = execute_and_insert_result(uno_ctx, doc, code)
        if not outcome.get("ok"):
            msgbox(uno_ctx, _("Execution Error"), outcome.get("message", _("Unknown error")))
            return
        if outcome.get("status_ok_text") == _(
            "Script executed successfully, but returned no result and produced no output."
        ):
            msgbox(uno_ctx, _("Success"), outcome["status_ok_text"])
            return
        if outcome.get("stdout") and outcome.get("result") is None:
            msgbox(uno_ctx, _("Output"), outcome.get("stdout"))
    except Exception as e:
        log.exception("run_python_dialog execution failed")
        msgbox(uno_ctx, _("Error"), str(e))
