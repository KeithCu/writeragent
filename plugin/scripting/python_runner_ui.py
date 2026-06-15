# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""UI Dialog logic for 'Run Python Script...' in Writer."""

import logging
from typing import Any
import unohelper
from com.sun.star.awt import XActionListener, XItemListener, XTopWindowListener

from plugin.framework.config import get_config, get_config_str, set_config
from plugin.framework.i18n import _
from plugin.chatbot.dialogs import load_writeragent_dialog, msgbox, show_approval_dialog
from plugin.chatbot.dialogs import show_text_input_dialog
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.document_scripts import (
    SCRIPT_ORIGIN_DOCUMENT,
    SCRIPT_ORIGIN_USER,
    attach_document_script,
    build_xdl_script_picker_state,
    delete_document_script,
    resolve_script_picker_entry,
    save_document_script,
)
from plugin.scripting.venv_worker import warm_venv_worker

log = logging.getLogger("writeragent.scripting")


def native_run_script_modeless_enabled(ctx: Any) -> bool:
    """When True, the plain-text Run Python Script dialog floats (document stays editable)."""
    return bool(get_config(ctx, "scripting.native_run_script_modeless"))


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

        selected_name = ""
        if select_display and select_display in names:
            selected_name = select_display
        else:
            from plugin.scripting.python_runner import resolve_run_script_name_config_key
            name_config_key = resolve_run_script_name_config_key(self._doc)
            last_name = get_config_str(self._ctx, name_config_key)
            if last_name and last_name in names:
                selected_name = last_name
        if not selected_name and names:
            selected_name = names[0]

        if selected_name:
            for idx, nm in enumerate(names):
                if nm == selected_name:
                    select_ctrl.selectItemPos(idx, True)
                    from plugin.scripting.python_runner import resolve_run_script_name_config_key
                    name_config_key = resolve_run_script_name_config_key(self._doc)
                    set_config(self._ctx, name_config_key, selected_name)
                    if self._dlg is not None:
                        try:
                            code_ctrl = self._dlg.getControl("CodeEdit")
                            if code_ctrl is not None:
                                code_ctrl.setText(merged.get(selected_name, ""))
                        except Exception:
                            pass
                    break


    def _open(self, initial_text: str) -> None:
        ctx = self._ctx
        try:
            dlg = load_writeragent_dialog("PythonScriptDialog", ctx)
            if dlg is None:
                self.close()
                return
            self._dlg = dlg

            # Trigger background pre-warming of the venv subprocess for the native fallback case as well
            run_in_background(warm_venv_worker, ctx, name="warm-venv-worker")

            select_ctrl = dlg.getControl("ScriptSelect")
            self._select_ctrl = select_ctrl

            saved_scripts = get_config(ctx, "saved_python_scripts")
            if not isinstance(saved_scripts, dict):
                saved_scripts = {}
            doc = self._doc
            script_names, merged_scripts, origin_map = build_xdl_script_picker_state(ctx, doc, saved_scripts)

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

    def _save_current_script(self, t: str) -> str | None:
        select_ctrl = self._select_ctrl
        if select_ctrl is None:
            return None
        pos = select_ctrl.getSelectedItemPos()
        items = select_ctrl.getItems()
        if pos >= 0 and pos < len(items):
            display_name = items[pos]
            real_name, origin = resolve_script_picker_entry(display_name, self._script_origin_map)
            self._current_scripts[display_name] = t
            if origin == SCRIPT_ORIGIN_DOCUMENT:
                if self._doc is None:
                    return _("No document is open to save scripts.")
                err = save_document_script(self._doc, real_name, t)
                if err:
                    user_scripts = get_config(self._ctx, "saved_python_scripts")
                    if not isinstance(user_scripts, dict):
                        user_scripts = {}
                    user_scripts[real_name] = t
                    set_config(self._ctx, "saved_python_scripts", user_scripts)
                    return _("%s Saved to My Scripts instead.") % err
                return _("Script '%s' saved to this document.") % real_name
            else:
                user_scripts = get_config(self._ctx, "saved_python_scripts")
                if not isinstance(user_scripts, dict):
                    user_scripts = {}
                user_scripts[real_name] = t
                set_config(self._ctx, "saved_python_scripts", user_scripts)
                return _("Script '%s' saved successfully.") % real_name
        return None

    def _wire_listeners(self, dlg: Any, select_ctrl: Any) -> None:
        ctx = self._ctx
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
                        # Save the selected name to config
                        from plugin.scripting.python_runner import resolve_run_script_name_config_key
                        name_config_key = resolve_run_script_name_config_key(owner._doc)
                        set_config(ctx, name_config_key, name)
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
                    owner._save_current_script(t)
                    from plugin.scripting.python_runner import execute_and_insert_result

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
                    lbl = dlg.getControl("InstructionLbl")
                    res = owner._save_current_script(t)
                    if res:
                        lbl.getModel().Label = res
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
    config_key: str = "last_python_script_writer",
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
        lbl.getModel().Label = status_text
