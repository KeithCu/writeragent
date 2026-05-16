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
from plugin.scripting.run_venv_code import run_code_in_user_venv
from plugin.writer.format import insert_content_at_position

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
        # Priority keys to show at the top without a bold label if they are strings
        priority_keys = ("summary", "summary_text", "message", "text", "result")
        
        # Sort keys: priority first, then others alphabetically. Skip underscores.
        sorted_keys = sorted(
            [k for k in result.keys() if not str(k).startswith("_")],
            key=lambda k: (k not in priority_keys, str(k).lower())
        )

        for key in sorted_keys:
            val = result[key]
            if isinstance(val, list) and val:
                table = _format_list_to_table(val)
                if table:
                    html_parts.append(f"<h3>{key}</h3>")
                    html_parts.append(table)
            elif isinstance(val, str):
                escaped = val.replace("\n", "<br>")
                if str(key).lower() in priority_keys:
                    html_parts.append(f"<p>{escaped}</p>")
                else:
                    html_parts.append(f"<p><b>{key}:</b> {escaped}</p>")
            elif isinstance(val, dict) and val:
                # Recurse one level for nested dicts? For now just stringify.
                html_parts.append(f"<p><b>{key}:</b> {val}</p>")
            else:
                html_parts.append(f"<p><b>{key}:</b> {val}</p>")
        
        return "\n".join(html_parts)

    # Fallback to string
    return str(result)


def run_python_dialog(uno_ctx: Any = None) -> None:
    """Entry point for the 'Run Python Script...' menu command."""
    if uno_ctx is None:
        uno_ctx = get_ctx()
    
    # Load last script from config
    initial_code = get_config_str(uno_ctx, "last_python_script")
    
    code = show_python_input_dialog(uno_ctx, initial_text=initial_code)
    if not code:
        return
        
    # Save the script to config for next time
    set_config(uno_ctx, "last_python_script", code)

    # Run the code
    try:
        # Note: run_code_in_user_venv handles venv resolution and worker management
        response = run_code_in_user_venv(uno_ctx, code)
        
        if response.get("status") == "ok":
            result_data = response.get("result")
            formatted = format_result_for_writer(result_data)
            
            if not formatted and not response.get("stdout"):
                msgbox(uno_ctx, _("Success"), _("Script executed successfully, but returned no result and produced no output."))
                return

            # If there was stdout but no result, maybe show stdout?
            # Or just insert result if present.
            if formatted:
                desktop = get_desktop(uno_ctx)
                doc = desktop.getCurrentComponent()
                if hasattr(doc, "getText"):
                    insert_content_at_position(doc, uno_ctx, formatted, "selection")
            
            if response.get("stdout"):
                log.info("Python script stdout: %s", response.get("stdout"))
                # Optionally show stdout in a message box if there's no result?
                if not formatted:
                    msgbox(uno_ctx, _("Output"), response.get("stdout"))
        else:
            error_msg = response.get("message", _("Unknown error"))
            log.error("Python script failed: %s", error_msg)
            msgbox(uno_ctx, _("Execution Error"), error_msg)
            
    except Exception as e:
        log.exception("run_python_dialog execution failed")
        msgbox(uno_ctx, _("Error"), str(e))
