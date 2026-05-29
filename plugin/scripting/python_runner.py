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
from com.sun.star.awt import XActionListener, XItemListener, XTopWindowListener

from plugin.framework.uno_context import get_ctx, get_desktop
from plugin.framework.config import get_config, get_config_str, set_config
from plugin.framework.i18n import _
from plugin.chatbot.dialogs import add_dialog_label, add_dialog_edit, add_dialog_button, msgbox, show_approval_dialog
from plugin.chatbot.dialogs import show_text_input_dialog
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.editor_ipc import exception_traceback
from plugin.scripting.document_scripts import (
    SCRIPT_ORIGIN_DOCUMENT,
    SCRIPT_ORIGIN_USER,
    attach_document_script,
    build_xdl_script_picker_state,
    delete_document_script,
    resolve_script_picker_entry,
    save_document_script,
)
from plugin.scripting.editor_host import launch_monaco_editor, monaco_editor_available
from plugin.scripting.venv_worker import run_code_in_user_venv, warm_venv_worker
from plugin.writer.format import insert_content_at_position
from plugin.doc.document_helpers import is_calc, is_writer, is_draw
from plugin.calc.bridge import CalcBridge
from plugin.calc.manipulator import CellManipulator
from plugin.calc.address_utils import index_to_column

log = logging.getLogger("writeragent.scripting")


def native_run_script_modeless_enabled(ctx: Any) -> bool:
    """When True, the plain-text Run Python Script dialog floats (document stays editable)."""
    return bool(get_config(ctx, "scripting.native_run_script_modeless"))


def add_dialog_listbox(dlg_model: Any, name: str, items: list[str], x: int, y: int, width: int, height: int) -> Any:
    lb = dlg_model.createInstance("com.sun.star.awt.UnoControlListBoxModel")
    lb.Name = name
    lb.PositionX = x
    lb.PositionY = y
    lb.Width = width
    lb.Height = height
    lb.Dropdown = True
    lb.MultiSelection = False
    lb.StringItemList = tuple(items)
    dlg_model.insertByName(name, lb)
    return lb


class NativePythonScriptDialog:
    """Plain-text Run Python Script dialog (modal or optional modeless).

    Each menu open creates its own instance, bound to the document that was active
    at open time. Multiple modeless dialogs may be open at once (one per document/window).

    Future: re-resolve the target document on each action when the user switches
    focus between LO windows (getCurrentComponent() did not track that in manual testing).
    """

    def __init__(
        self,
        ctx: Any,
        *,
        initial_text: str,
        config_key: str,
        initial_doc: Any | None,
        modeless: bool,
    ) -> None:
        self._ctx = ctx
        self._config_key = config_key
        self._doc = initial_doc
        self._modeless = modeless
        self._dlg: Any | None = None
        self._select_ctrl: Any | None = None
        self._current_scripts: dict[str, str] = {}
        self._script_origin_map: dict[str, str] = {}
        self._closed = False
        self._top_listener: Any | None = None
        self._open(initial_text)

    @classmethod
    def show(
        cls,
        ctx: Any,
        *,
        initial_text: str,
        config_key: str,
        doc: Any | None,
        modeless: bool,
    ) -> None:
        cls(
            ctx,
            initial_text=initial_text,
            config_key=config_key,
            initial_doc=doc,
            modeless=modeless,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        dlg = self._dlg
        self._dlg = None
        if dlg is None:
            return
        try:
            dlg.setVisible(False)
        except Exception:
            log.exception("Failed to hide native script dialog")
        try:
            dlg.dispose()
        except Exception:
            log.exception("Failed to dispose native script dialog")

    def _refresh_script_dropdown(self, select_display: str | None = None) -> None:
        select_ctrl = self._select_ctrl
        if select_ctrl is None:
            return
        saved = get_config(self._ctx, "saved_python_scripts")
        if not isinstance(saved, dict):
            saved = {}
        names, merged, origin_map = build_xdl_script_picker_state(self._ctx, self._doc, saved)
        self._current_scripts = merged
        self._script_origin_map = origin_map
        select_ctrl.removeItems(0, select_ctrl.getItemCount())
        select_ctrl.addItems(tuple(names), 0)
        if select_display and select_display in names:
            for idx, nm in enumerate(names):
                if nm == select_display:
                    select_ctrl.selectItemPos(idx, True)
                    break
        else:
            select_ctrl.selectItemPos(0, True)

    def _open(self, initial_text: str) -> None:
        ctx = self._ctx
        try:
            desktop = get_desktop(ctx)
            frame = desktop.getCurrentFrame()
            if frame is None:
                self.close()
                return
            parent_window = frame.getContainerWindow()
            if parent_window is None:
                self.close()
                return

            smgr = ctx.getServiceManager()
            dlg_model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
            dlg_model.Title = _("Run Python Script")
            dlg_model.Width = 400
            dlg_model.Height = 220
            dlg_model.Moveable = True
            dlg_model.Closeable = True

            add_dialog_button(dlg_model, "BtnRun", _("Run"), 8, 8, 50, 14)
            add_dialog_button(dlg_model, "BtnSave", _("Save"), 62, 8, 50, 14)
            add_dialog_button(dlg_model, "BtnCancel", _("Close"), 116, 8, 50, 14)

            saved_scripts = get_config(ctx, "saved_python_scripts")
            if not isinstance(saved_scripts, dict):
                saved_scripts = {}
            doc = self._doc
            script_names, merged_scripts, origin_map = build_xdl_script_picker_state(ctx, doc, saved_scripts)

            add_dialog_label(dlg_model, "ScriptLbl", _("Script:"), 172, 10, 22, 10, multiline=False)
            add_dialog_listbox(dlg_model, "ScriptSelect", script_names, 196, 8, 60, 14)
            add_dialog_button(dlg_model, "BtnAttach", _("Attach"), 260, 8, 44, 14)
            add_dialog_button(dlg_model, "BtnSaveAs", _("Save As..."), 308, 8, 44, 14)
            add_dialog_button(dlg_model, "BtnDelete", _("Delete"), 356, 8, 34, 14)

            add_dialog_label(
                dlg_model,
                "InstructionLbl",
                _("Enter Python code to execute in the user virtual environment.\nAssign the result to the 'result' variable."),
                8,
                26,
                334,
                20,
            )

            edit = add_dialog_edit(dlg_model, "CodeEdit", initial_text, 8, 48, 334, 164)
            edit.MultiLine = True
            edit.VScroll = True
            fd = cast("Any", uno.createUnoStruct("com.sun.star.awt.FontDescriptor"))
            fd.Name = "Courier New"
            edit.FontDescriptor = fd

            dlg = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
            dlg.setModel(dlg_model)
            toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
            dlg.createPeer(toolkit, parent_window)
            self._dlg = dlg

            select_ctrl = dlg.getControl("ScriptSelect")
            self._select_ctrl = select_ctrl
            if select_ctrl:
                select_ctrl.selectItemPos(0, True)

            self._current_scripts = dict(merged_scripts)
            self._script_origin_map = dict(origin_map)
            self._wire_listeners(dlg, select_ctrl)

            code_ctrl = dlg.getControl("CodeEdit")
            if code_ctrl is not None:
                code_ctrl.setFocus()

            if self._modeless:
                owner = self

                class _TopWindowListener(unohelper.Base, XTopWindowListener):
                    def windowClosing(self, e):
                        owner.close()

                    def windowClosed(self, e):
                        pass

                    def windowOpened(self, e):
                        pass

                    def windowMinimized(self, e):
                        pass

                    def windowNormalized(self, e):
                        pass

                    def windowActivated(self, e):
                        pass

                    def windowDeactivated(self, e):
                        pass

                    def disposing(self, Source):
                        pass

                self._top_listener = _TopWindowListener()
                dlg.addTopWindowListener(self._top_listener)
                dlg.setVisible(True)
            else:
                dlg.execute()
                dlg.dispose()
                self._dlg = None
        except Exception:
            log.exception("NativePythonScriptDialog._open failed")
            self.close()

    def _wire_listeners(self, dlg: Any, select_ctrl: Any) -> None:
        ctx = self._ctx
        config_key = self._config_key
        owner = self
        doc = owner._doc

        class _ScriptSelectListener(unohelper.Base, XItemListener):
            def itemStateChanged(self, rEvent):
                try:
                    pos = select_ctrl.getSelectedItemPos()
                    items = select_ctrl.getItems()
                    if pos >= 0 and pos < len(items):
                        name = items[pos]
                        code_ctrl = dlg.getControl("CodeEdit")
                        if name == "Sample":
                            t = get_config_str(ctx, config_key)
                            code_ctrl.setText(t)
                        else:
                            t = owner._current_scripts.get(name, "")
                            code_ctrl.setText(t)
                except Exception:
                    log.exception("Failed to change script selection")

            def disposing(self, Source):
                pass

        class _RunListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                try:
                    ec = dlg.getControl("CodeEdit")
                    t = (ec.getModel().Text or "").strip()
                    lbl = dlg.getControl("InstructionLbl")
                    set_config(ctx, config_key, t)
                    outcome = execute_and_insert_result(ctx, doc, t)
                    _report_run_outcome(ctx, lbl, outcome)
                except Exception as e:
                    log.exception("Run failed in dialog")
                    msgbox(ctx, _("Error"), str(e))

            def disposing(self, Source):
                pass

        class _SaveListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                try:
                    ec = dlg.getControl("CodeEdit")
                    t = (ec.getModel().Text or "").strip()

                    pos = select_ctrl.getSelectedItemPos()
                    items = select_ctrl.getItems()
                    lbl = dlg.getControl("InstructionLbl")

                    if pos >= 0 and pos < len(items) and items[pos] != "Sample":
                        display_name = items[pos]
                        real_name, origin = resolve_script_picker_entry(display_name, owner._script_origin_map)
                        owner._current_scripts[display_name] = t
                        if origin == SCRIPT_ORIGIN_DOCUMENT:
                            if doc is None:
                                lbl.getModel().Label = _("No document is open to save scripts.")
                                return
                            err = save_document_script(doc, real_name, t)
                            if err:
                                user_scripts = get_config(ctx, "saved_python_scripts")
                                if not isinstance(user_scripts, dict):
                                    user_scripts = {}
                                user_scripts[real_name] = t
                                set_config(ctx, "saved_python_scripts", user_scripts)
                                lbl.getModel().Label = _("%s Saved to My Scripts instead.") % err
                                return
                            lbl.getModel().Label = _("Script '%s' saved to this document.") % real_name
                        else:
                            user_scripts = get_config(ctx, "saved_python_scripts")
                            if not isinstance(user_scripts, dict):
                                user_scripts = {}
                            user_scripts[real_name] = t
                            set_config(ctx, "saved_python_scripts", user_scripts)
                            lbl.getModel().Label = _("Script '%s' saved successfully.") % real_name
                    else:
                        set_config(ctx, config_key, t)
                        lbl.getModel().Label = _("Sample scratchpad saved successfully.")
                except Exception:
                    log.exception("Save failed in dialog")

            def disposing(self, Source):
                pass

        class _AttachListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                try:
                    lbl = dlg.getControl("InstructionLbl")
                    if doc is None:
                        lbl.getModel().Label = _("No document is open to attach scripts.")
                        return
                    ec = dlg.getControl("CodeEdit")
                    t = (ec.getModel().Text or "").strip()
                    pos = select_ctrl.getSelectedItemPos()
                    items = select_ctrl.getItems()
                    curr = items[pos] if (pos >= 0 and pos < len(items) and items[pos] != "Sample") else ""
                    real_curr, _curr_origin = resolve_script_picker_entry(curr, owner._script_origin_map) if curr else ("", SCRIPT_ORIGIN_USER)
                    name = show_text_input_dialog(ctx, _("Enter script name:"), _("Attach to Document"), real_curr)
                    if not name:
                        return
                    name = name.strip()
                    if not name:
                        return
                    from plugin.scripting.document_scripts import document_script_display_name, get_document_scripts

                    overwrite = name in get_document_scripts(doc)
                    if overwrite and not show_approval_dialog(
                        ctx,
                        _("A script named '{0}' already exists in this document. Overwrite?").format(name),
                        _("Attach Script"),
                    ):
                        return
                    err = attach_document_script(doc, name, t, overwrite=True)
                    if err:
                        lbl.getModel().Label = err
                        return
                    owner._refresh_script_dropdown(document_script_display_name(name))
                    lbl.getModel().Label = _("Script '%s' attached to this document.") % name
                except Exception:
                    log.exception("Attach failed in dialog")

            def disposing(self, Source):
                pass

        class _SaveAsListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                try:
                    ec = dlg.getControl("CodeEdit")
                    t = (ec.getModel().Text or "").strip()

                    pos = select_ctrl.getSelectedItemPos()
                    items = select_ctrl.getItems()
                    curr_display = items[pos] if (pos >= 0 and pos < len(items) and items[pos] != "Sample") else ""
                    real_curr, curr_origin = (
                        resolve_script_picker_entry(curr_display, owner._script_origin_map)
                        if curr_display
                        else ("", SCRIPT_ORIGIN_USER)
                    )

                    name = show_text_input_dialog(ctx, _("Enter script name:"), _("Save Script"), real_curr)
                    if not name:
                        return
                    name = name.strip()
                    if not name:
                        return

                    lbl = dlg.getControl("InstructionLbl")
                    save_to_document = curr_origin == SCRIPT_ORIGIN_DOCUMENT
                    if doc is not None and not save_to_document:
                        save_to_document = show_approval_dialog(
                            ctx,
                            _("Save script '{0}' to this document?").format(name),
                            _("Save Script"),
                        )
                    if doc is not None and save_to_document:
                        from plugin.scripting.document_scripts import document_script_display_name

                        err = save_document_script(doc, name, t)
                        if err:
                            user_scripts = get_config(ctx, "saved_python_scripts")
                            if not isinstance(user_scripts, dict):
                                user_scripts = {}
                            user_scripts[name] = t
                            set_config(ctx, "saved_python_scripts", user_scripts)
                            lbl.getModel().Label = _("%s Saved to My Scripts instead.") % err
                        else:
                            lbl.getModel().Label = _("Script '%s' saved to this document.") % name
                        owner._refresh_script_dropdown(document_script_display_name(name))
                        return

                    user_scripts = get_config(ctx, "saved_python_scripts")
                    if not isinstance(user_scripts, dict):
                        user_scripts = {}
                    user_scripts[name] = t
                    set_config(ctx, "saved_python_scripts", user_scripts)
                    owner._refresh_script_dropdown(name)
                    lbl.getModel().Label = _("Script '%s' saved successfully.") % name
                except Exception:
                    log.exception("Save As failed in dialog")

            def disposing(self, Source):
                pass

        class _DeleteListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                try:
                    pos = select_ctrl.getSelectedItemPos()
                    items = select_ctrl.getItems()
                    if pos < 0 or pos >= len(items):
                        return

                    display_name = items[pos]
                    lbl = dlg.getControl("InstructionLbl")

                    if display_name == "Sample":
                        if show_approval_dialog(ctx, _("Are you sure you want to clear the Sample scratchpad?"), _("Clear Script")):
                            dlg.getControl("CodeEdit").setText("")
                            set_config(ctx, config_key, "")
                            lbl.getModel().Label = _("Sample scratchpad cleared.")
                    else:
                        real_name, origin = resolve_script_picker_entry(display_name, owner._script_origin_map)
                        if show_approval_dialog(
                            ctx,
                            _("Are you sure you want to delete script '%s'?") % real_name,
                            _("Delete Script"),
                        ):
                            if origin == SCRIPT_ORIGIN_DOCUMENT:
                                if doc is None:
                                    lbl.getModel().Label = _("No document is open.")
                                    return
                                delete_document_script(doc, real_name)
                            else:
                                user_scripts = get_config(ctx, "saved_python_scripts")
                                if not isinstance(user_scripts, dict):
                                    user_scripts = {}
                                user_scripts.pop(real_name, None)
                                set_config(ctx, "saved_python_scripts", user_scripts)
                            owner._refresh_script_dropdown()
                            select_ctrl.selectItemPos(0, True)
                            dlg.getControl("CodeEdit").setText(get_config_str(ctx, config_key))
                            lbl.getModel().Label = _("Script '%s' deleted.") % real_name
                except Exception:
                    log.exception("Delete failed in dialog")

            def disposing(self, Source):
                pass

        class _CancelListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                if owner._modeless:
                    owner.close()
                else:
                    dlg.endDialog(0)

            def disposing(self, Source):
                pass

        select_ctrl.addItemListener(_ScriptSelectListener())
        dlg.getControl("BtnRun").addActionListener(_RunListener())
        dlg.getControl("BtnSave").addActionListener(_SaveListener())
        dlg.getControl("BtnAttach").addActionListener(_AttachListener())
        dlg.getControl("BtnSaveAs").addActionListener(_SaveAsListener())
        dlg.getControl("BtnDelete").addActionListener(_DeleteListener())
        dlg.getControl("BtnCancel").addActionListener(_CancelListener())


def show_python_input_dialog(
    ctx: Any,
    initial_text: str = "",
    config_key: str = "last_python_script",
    doc: Any | None = None,
) -> None:
    """Show the plain-text Run Python Script dialog (modeless when configured)."""
    try:
        modeless = native_run_script_modeless_enabled(ctx)
        NativePythonScriptDialog.show(
            ctx,
            initial_text=initial_text,
            config_key=config_key,
            doc=doc,
            modeless=modeless,
        )
    except Exception:
        log.exception("show_python_input_dialog failed")


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


def _report_run_outcome(ctx: Any, lbl: Any | None, outcome: dict[str, Any]) -> None:
    """Update native dialog status / msgboxes after Run."""
    if not outcome.get("ok"):
        msgbox(ctx, _("Execution Error"), outcome.get("message", _("Unknown error")))
        return
    status_text = outcome.get("status_ok_text", _("Script executed successfully."))
    if status_text == _(
        "Script executed successfully, but returned no result and produced no output."
    ):
        msgbox(ctx, _("Success"), status_text)
    elif outcome.get("stdout") and outcome.get("result") is None:
        msgbox(ctx, _("Output"), outcome.get("stdout"))
    if lbl is not None:
        lbl.getModel().Label = status_text


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
        "run_script_doc": doc,
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

    show_python_input_dialog(uno_ctx, initial_text=initial_code, config_key=config_key, doc=doc)
