# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""UI Dialog logic for 'Run Python Script...' in Writer."""

# =========================================================================================
# WARNING: PARITY INVARIANT WITH MONACO JAVASCRIPT FRONTEND
# If you modify script actions, dropdown listeners, dialog layouts, or templates here,
# you MUST also update the corresponding JavaScript / HTML implementations:
#   - JS Script Manager:        plugin/contrib/scripting/assets/editor/scripts_manager.js
#   - Monaco HTML / Toolbar:    plugin/contrib/scripting/assets/editor/index.html
#   - UI Strings Catalog:       plugin/scripting/editor_ui_strings.py
#   - Document Scripts Data:    plugin/scripting/document_scripts.py
#   - Native Dialog Layout:     extension/Dialogs/PythonScriptDialog.xdl
#   - Native New Script Dialog: extension/Dialogs/NewScriptDialog.xdl
# =========================================================================================

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
import unohelper
from com.sun.star.awt import XActionListener, XItemListener, XTopWindowListener

if TYPE_CHECKING:
    from com.sun.star.awt import ActionEvent, ItemEvent
    from com.sun.star.lang import EventObject

from plugin.framework.config import get_config, get_config_str, set_config
from plugin.framework.i18n import _
from plugin.chatbot.dialogs import load_writeragent_dialog_detail, msgbox, set_control_text, show_approval_dialog
from plugin.chatbot.dialogs import show_new_script_dialog
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.document_scripts import (
    attach_document_script,
    build_xdl_script_picker_state,
    delete_document_script,
    delete_user_script,
    get_user_scripts,
    resolve_script_picker_entry,
    save_document_script,
    save_user_script,
)
from plugin.scripting.domain_registry import SCRIPT_ORIGIN_DOCUMENT, SCRIPT_ORIGIN_USER
from plugin.scripting.venv_worker import warm_venv_worker

log = logging.getLogger("writeragent.scripting")


def native_run_script_modeless_enabled(ctx: Any) -> bool:
    """When True, the plain-text Run Python Script dialog floats (document stays editable)."""
    return bool(get_config("scripting.native_run_script_modeless"))


def _picker_selected_name(select_ctrl: Any) -> str:
    """Return the selected script name from ScriptSelect (listbox or combobox)."""
    if hasattr(select_ctrl, "getSelectedItemPos"):
        pos = select_ctrl.getSelectedItemPos()
        items = select_ctrl.getItems()
        if pos >= 0 and pos < len(items):
            return str(items[pos])
    if hasattr(select_ctrl, "getText"):
        return str(select_ctrl.getText() or "").strip()
    return ""


def _picker_select_name(select_ctrl: Any, name: str, names: list[str]) -> None:
    """Select *name* in ScriptSelect (listbox or combobox)."""
    if not name:
        return
    if hasattr(select_ctrl, "selectItemPos"):
        for idx, nm in enumerate(names):
            if nm == name:
                select_ctrl.selectItemPos(idx, True)
                return
    if hasattr(select_ctrl, "setText"):
        select_ctrl.setText(name)


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
        initial_doc: Any | None,
        modeless: bool,
    ) -> None:
        self._ctx = ctx
        self._doc = initial_doc
        self._modeless = modeless
        self._dlg: Any | None = None
        self._select_ctrl: Any | None = None
        self._current_scripts: dict[str, str] = {}
        self._script_origin_map: dict[str, str] = {}
        self._closed = False
        self._top_listener: Any | None = None
        self._open_failure_detail: str | None = None
        self._opened = self._open()

    @classmethod
    def show(
        cls,
        ctx: Any,
        *,
        doc: Any | None,
        modeless: bool,
    ) -> tuple[bool, str | None]:
        inst = cls(
            ctx,
            initial_doc=doc,
            modeless=modeless,
        )
        if inst._opened:
            return True, None
        return False, inst._open_failure_detail

    def close(self, *, toolkit_teardown: bool = False) -> None:
        """Hide/dispose the dialog.

        Esc / title-bar X on a closeable modeless XDL already tears the window
        down in LibreOffice. ``windowClosing`` must not ``dispose()`` again
        (native crash, no Python traceback). Close-button uses the default
        path and disposes once.
        """
        if self._closed:
            return
        self._closed = True
        dlg = self._dlg
        self._dlg = None
        if dlg is None:
            return
        if toolkit_teardown:
            log.debug("native script dialog: windowClosing (no dispose)")
            try:
                dlg.setVisible(False)
            except Exception:
                log.debug("native script dialog: hide after windowClosing failed", exc_info=True)
            return
        log.debug("native script dialog: close dispose")
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
        names, merged, origin_map = build_xdl_script_picker_state(self._ctx, self._doc, get_user_scripts())
        self._current_scripts = merged
        self._script_origin_map = origin_map
        select_ctrl.removeItems(0, select_ctrl.getItemCount())
        select_ctrl.addItems(tuple(names), 0)

        selected_name = ""
        if select_display and select_display in names:
            selected_name = select_display
        else:
            from plugin.scripting.python_runner import resolve_run_script_name_config_key
            name_config_key = resolve_run_script_name_config_key(self._doc)
            last_name = get_config_str(name_config_key)
            if last_name and last_name in names:
                selected_name = last_name
        if not selected_name and names:
            selected_name = names[0]

        if selected_name:
            _picker_select_name(select_ctrl, selected_name, names)
            from plugin.scripting.python_runner import resolve_run_script_name_config_key
            name_config_key = resolve_run_script_name_config_key(self._doc)
            set_config(name_config_key, selected_name)
            if self._dlg is not None:
                try:
                    code_ctrl = self._dlg.getControl("CodeEdit")
                    if code_ctrl is not None:
                        code_ctrl.setText(merged.get(selected_name, ""))
                except Exception:
                    pass


    def _open(self) -> bool:
        ctx = self._ctx
        try:
            dlg, load_detail = load_writeragent_dialog_detail("PythonScriptDialog", ctx)
            if dlg is None:
                log.error(
                    "NativePythonScriptDialog: PythonScriptDialog XDL load failed:\n%s",
                    load_detail or "(no load detail captured)",
                )
                self._open_failure_detail = load_detail or _("PythonScriptDialog could not be loaded from the extension.")
                self.close()
                return False
            self._dlg = dlg

            # Trigger background pre-warming of the venv subprocess for the native fallback case as well
            run_in_background(warm_venv_worker, ctx, name="warm-venv-worker")

            select_ctrl = dlg.getControl("ScriptSelect")
            self._select_ctrl = select_ctrl

            doc = self._doc
            _script_names, merged_scripts, origin_map = build_xdl_script_picker_state(ctx, doc, get_user_scripts())

            self._current_scripts = dict(merged_scripts)
            self._script_origin_map = dict(origin_map)

            # Re-initialize picker items and selection cleanly
            self._refresh_script_dropdown()
            self._wire_listeners(dlg, select_ctrl)

            code_ctrl = dlg.getControl("CodeEdit")
            if code_ctrl is not None:
                code_ctrl.setFocus()

            if self._modeless:
                owner = self

                class _TopWindowListener(unohelper.Base, XTopWindowListener):
                    def windowClosing(self, e: Any):
                        owner.close(toolkit_teardown=True)

                    def windowClosed(self, e: Any):
                        pass

                    def windowOpened(self, e: Any):
                        pass

                    def windowMinimized(self, e: Any):
                        pass

                    def windowNormalized(self, e: Any):
                        pass

                    def windowActivated(self, e: Any):
                        pass

                    def windowDeactivated(self, e: Any):
                        pass

                    def disposing(self, Source: EventObject):
                        pass

                self._top_listener = _TopWindowListener()
                dlg.addTopWindowListener(self._top_listener)
                dlg.setVisible(True)
                return True
            dlg.execute()
            self._closed = True
            self._dlg = None
            try:
                dlg.dispose()
            except Exception:
                log.debug("native script dialog: modal dispose after execute", exc_info=True)
            return True
        except Exception as exc:
            from plugin.scripting.editor_ipc import exception_traceback

            log.exception("NativePythonScriptDialog._open failed")
            self._open_failure_detail = exception_traceback(exc)
            self.close()
            return False

    def _save_current_script(self, t: str) -> str | None:
        select_ctrl = self._select_ctrl
        if select_ctrl is None:
            return None
        display_name = _picker_selected_name(select_ctrl)
        if display_name:
            real_name, origin = resolve_script_picker_entry(display_name, self._script_origin_map)
            self._current_scripts[display_name] = t
            if origin == SCRIPT_ORIGIN_DOCUMENT:
                if self._doc is None:
                    return _("No document is open to save scripts.")
                err = save_document_script(self._doc, real_name, t)
                if err:
                    save_user_script(real_name, t)
                    return _("%s Saved to My Scripts instead.") % err
                return _("Script '%s' saved to this document.") % real_name
            else:
                save_user_script(real_name, t)
                return _("Script '%s' saved successfully.") % real_name
        return None

    def _wire_listeners(self, dlg: Any, select_ctrl: Any) -> None:
        ctx = self._ctx
        owner = self
        doc = owner._doc

        class _ScriptSelectListener(unohelper.Base, XItemListener):
            def itemStateChanged(self, rEvent: ItemEvent):
                try:
                    name = _picker_selected_name(select_ctrl)
                    if name:
                        code_ctrl = dlg.getControl("CodeEdit")
                        # Save the selected name to config
                        from plugin.scripting.python_runner import resolve_run_script_name_config_key
                        name_config_key = resolve_run_script_name_config_key(owner._doc)
                        set_config(name_config_key, name)
                        t = owner._current_scripts.get(name, "")
                        code_ctrl.setText(t)
                except Exception:
                    log.exception("Failed to change script selection")

            def disposing(self, Source: EventObject):
                pass

        class _RunListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: ActionEvent):
                try:
                    ec = dlg.getControl("CodeEdit")
                    t = (ec.getModel().Text or "").strip()
                    lbl = dlg.getControl("InstructionLbl")
                    owner._save_current_script(t)
                    from plugin.scripting.python_runner import execute_and_insert_result

                    outcome = execute_and_insert_result(ctx, doc, t)
                    _report_run_outcome(ctx, lbl, outcome)
                except Exception as e:
                    log.exception("Run failed in dialog")
                    msgbox(ctx, _("Error"), str(e))

            def disposing(self, Source: EventObject):
                pass

        # WARNING: If you change Save logic, also update btn-save listener in:
        # plugin/contrib/scripting/assets/editor/scripts_manager.js
        class _SaveListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: ActionEvent):
                try:
                    ec = dlg.getControl("CodeEdit")
                    t = (ec.getModel().Text or "").strip()
                    lbl = dlg.getControl("InstructionLbl")
                    res = owner._save_current_script(t)
                    if res:
                        set_control_text(lbl, res)
                except Exception:
                    log.exception("Save failed in dialog")

            def disposing(self, Source: EventObject):
                pass

        # WARNING: If you change Save As logic, also update onSaveAs in:
        # plugin/contrib/scripting/assets/editor/scripts_manager.js
        class _SaveAsListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: ActionEvent):
                try:
                    ec = dlg.getControl("CodeEdit")
                    t = (ec.getModel().Text or "").strip()

                    curr_display = _picker_selected_name(select_ctrl)
                    real_curr, curr_origin = (
                        resolve_script_picker_entry(curr_display, owner._script_origin_map)
                        if curr_display
                        else ("", SCRIPT_ORIGIN_USER)
                    )

                    res = show_new_script_dialog(
                        ctx,
                        doc=doc,
                        default_name=real_curr,
                        title=_("Save Script As"),
                        default_attach=(curr_origin == SCRIPT_ORIGIN_DOCUMENT),
                    )
                    if not res:
                        return
                    name, attach_to_document = res
                    name = name.strip()
                    if not name:
                        return

                    lbl = dlg.getControl("InstructionLbl")
                    if attach_to_document and doc is not None:
                        from plugin.scripting.document_scripts import document_script_display_name, get_document_scripts

                        overwrite = name in get_document_scripts(doc)
                        if overwrite and not show_approval_dialog(
                            ctx,
                            _("A script named '{0}' already exists in this document. Overwrite?").format(name),
                            _("Save Script As"),
                        ):
                            return
                        err = attach_document_script(doc, name, t, overwrite=True)
                        if err:
                            set_control_text(lbl, err)
                            return
                        owner._refresh_script_dropdown(document_script_display_name(name))
                        set_control_text(lbl, _("Script '%s' saved to this document.") % name)
                    else:
                        if name in get_user_scripts() and not show_approval_dialog(
                            ctx,
                            _("A script named '{0}' already exists in My Scripts. Overwrite?").format(name),
                            _("Save Script As"),
                        ):
                            return
                        save_user_script(name, t)
                        owner._refresh_script_dropdown(name)
                        set_control_text(lbl, _("Script '%s' saved to My Scripts.") % name)
                except Exception:
                    log.exception("Save As failed in dialog")

            def disposing(self, Source: EventObject):
                pass

        # WARNING: If you change Delete logic, also update onDeleteScript in:
        # plugin/contrib/scripting/assets/editor/scripts_manager.js
        class _DeleteListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: ActionEvent):
                try:
                    display_name = _picker_selected_name(select_ctrl)
                    if not display_name:
                        return

                    lbl = dlg.getControl("InstructionLbl")

                    real_name, origin = resolve_script_picker_entry(display_name, owner._script_origin_map)
                    if show_approval_dialog(
                        ctx,
                        _("Are you sure you want to delete script '%s'?") % real_name,
                        _("Delete Script"),
                    ):
                        if origin == SCRIPT_ORIGIN_DOCUMENT:
                            if doc is None:
                                set_control_text(lbl, _("No document is open."))
                                return
                            delete_document_script(doc, real_name)
                        else:
                            delete_user_script(real_name)
                        owner._refresh_script_dropdown()
                        set_control_text(lbl, _("Script '%s' deleted.") % real_name)
                except Exception:
                    log.exception("Delete failed in dialog")

            def disposing(self, Source: EventObject):
                pass

        # WARNING: If you change New script creation logic, also update onCreateNewScript in:
        # plugin/contrib/scripting/assets/editor/scripts_manager.js
        # and dialog layout in extension/Dialogs/NewScriptDialog.xdl
        class _NewListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: ActionEvent):
                try:
                    res = show_new_script_dialog(ctx, doc=doc)
                    if not res:
                        return
                    name, attach_to_document = res
                    name = name.strip()
                    if not name:
                        return

                    lbl = dlg.getControl("InstructionLbl")
                    ec = dlg.getControl("CodeEdit")
                    starter_code = '# A simple script\nresult = "Hello from Python!"\n'

                    if attach_to_document and doc is not None:
                        from plugin.scripting.document_scripts import document_script_display_name, get_document_scripts

                        overwrite = name in get_document_scripts(doc)
                        if overwrite and not show_approval_dialog(
                            ctx,
                            _("A script named '{0}' already exists in this document. Overwrite?").format(name),
                            _("New Script"),
                        ):
                            return
                        err = attach_document_script(doc, name, starter_code, overwrite=True)
                        if err:
                            set_control_text(lbl, err)
                            return
                        if ec is not None:
                            set_control_text(ec, starter_code)
                        owner._refresh_script_dropdown(document_script_display_name(name))
                        set_control_text(lbl, _("Script '%s' created in this document.") % name)
                    else:
                        if name in get_user_scripts() and not show_approval_dialog(
                            ctx,
                            _("A script named '{0}' already exists in My Scripts. Overwrite?").format(name),
                            _("New Script"),
                        ):
                            return
                        save_user_script(name, starter_code)
                        if ec is not None:
                            set_control_text(ec, starter_code)
                        owner._refresh_script_dropdown(name)
                        set_control_text(lbl, _("Script '%s' created in My Scripts.") % name)
                except Exception:
                    log.exception("New script failed in dialog")

            def disposing(self, Source: EventObject):
                pass

        class _CancelListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: ActionEvent):
                log.debug("native script dialog: BtnCancel")
                if owner._modeless:
                    owner.close()
                else:
                    dlg.endDialog(0)

            def disposing(self, Source: EventObject):
                pass

        select_ctrl.addItemListener(_ScriptSelectListener())
        dlg.getControl("BtnRun").addActionListener(_RunListener())
        dlg.getControl("BtnSave").addActionListener(_SaveListener())
        btn_new = dlg.getControl("BtnNew")
        if btn_new is not None:
            btn_new.addActionListener(_NewListener())
        dlg.getControl("BtnSaveAs").addActionListener(_SaveAsListener())
        dlg.getControl("BtnDelete").addActionListener(_DeleteListener())
        dlg.getControl("BtnCancel").addActionListener(_CancelListener())


def show_python_input_dialog(
    ctx: Any,
    doc: Any | None = None,
) -> tuple[bool, str | None]:
    """Show the plain-text Run Python Script dialog (modeless when configured).

    Returns (opened, failure_detail). failure_detail is set when opened is False.
    Selection and editor text come from ``last_python_script_name_*`` and the picker.
    """
    try:
        modeless = native_run_script_modeless_enabled(ctx)
        return NativePythonScriptDialog.show(
            ctx,
            doc=doc,
            modeless=modeless,
        )
    except Exception as exc:
        from plugin.scripting.editor_ipc import exception_traceback

        log.exception("show_python_input_dialog failed")
        return False, exception_traceback(exc)


def _report_run_outcome(ctx: Any, lbl: Any | None, outcome: dict[str, Any]) -> None:
    """Update native dialog status / msgboxes after Run."""
    if not outcome.get("ok"):
        msgbox(ctx, _("Execution Error"), outcome.get("message", _("Unknown error")))
        return
    status_text = outcome.get("status_ok_text", _("Script executed successfully."))
    if status_text.startswith(_(
        "Script executed successfully, but returned no result and produced no output."
    )):
        msgbox(ctx, _("Success"), status_text)
    elif outcome.get("stdout") and outcome.get("result") is None:
        msgbox(ctx, _("Output"), outcome.get("stdout"))
    if lbl is not None:
        set_control_text(lbl, status_text)
