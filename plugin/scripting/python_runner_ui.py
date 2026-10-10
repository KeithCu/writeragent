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
from typing import TYPE_CHECKING, Any, Callable
import unohelper
from com.sun.star.awt import XActionListener, XItemListener, XTopWindowListener

if TYPE_CHECKING:
    from com.sun.star.awt import ActionEvent, ItemEvent
    from com.sun.star.lang import EventObject


class _ActionListener(unohelper.Base, XActionListener):
    _action_fn: Callable[[], Any]

    def __init__(self, action_fn: Callable[[], Any]) -> None:
        self._action_fn = action_fn

    def actionPerformed(self, rEvent: ActionEvent) -> None:
        try:
            self._action_fn()
        except Exception:
            log.exception("Action listener failed")

    def disposing(self, Source: EventObject) -> None:
        pass


def _action(fn: Callable[[], Any], name: str = "") -> XActionListener:
    cls = type(name, (_ActionListener,), {}) if name else _ActionListener
    return cls(fn)

from plugin.framework.config import get_config, get_config_str, set_config
from plugin.framework.i18n import _
from plugin.chatbot.dialogs import (
    load_writeragent_dialog_detail,
    msgbox,
    set_control_enabled,
    set_control_text,
    show_approval_dialog,
)
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
    script_origin_is_library,
)
from plugin.scripting.domain_registry import SCRIPT_ORIGIN_DOCUMENT, SCRIPT_ORIGIN_USER
from plugin.scripting.venv_worker import warm_venv_worker

log = logging.getLogger("writeragent.scripting")


def _report_script_dialog_error(ctx: Any, dlg: Any, exc: BaseException, where: str) -> None:
    """Show Save / Save As / Delete / New failures. Those buttons used to only log."""
    log.exception("%s failed in dialog", where)
    from plugin.framework.errors import is_disposed_exception

    text = _("The document is no longer open.") if is_disposed_exception(exc) else str(exc)
    lbl = None
    try:
        lbl = dlg.getControl("InstructionLbl")
    except Exception:
        lbl = None
    if lbl is not None:
        try:
            set_control_text(lbl, text)
            return
        except Exception:
            log.exception("Could not update the script dialog status")
    msgbox(ctx, _("Error"), text)


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
    thread.
    """
    from plugin.framework.queue_executor import execute_on_main_thread, post_to_main_thread
    from plugin.scripting.editor_ipc import exception_traceback
    from plugin.scripting.helper_domain import rps_error_outcome
    from plugin.scripting.python_runner import (
        _finish_rps_execution,
        _prepare_rps_execution,
        _run_prepared_rps,
    )

    def _deliver(outcome: dict[str, Any]) -> None:
        def _apply() -> None:
            on_complete(outcome)

        post_to_main_thread(_apply)

    def _native_script_run_worker() -> None:
        try:
            prepared = execute_on_main_thread(
                _prepare_rps_execution, ctx, doc, code, data_range=data_range
            )
            early = prepared.get("early_outcome")
            if early is not None:
                _deliver(early)
                return
            try:
                response = _run_prepared_rps(prepared)
            except Exception as exc:
                from plugin.scripting.session_manager import release_script_document

                release_script_document(prepared.get("script_session_id"))
                log.exception("native script run failed")
                _deliver(rps_error_outcome(str(exc), t0=prepared["t0"], traceback=exception_traceback(exc)))
                return
            outcome = execute_on_main_thread(_finish_rps_execution, prepared, response)
            _deliver(outcome)
        except Exception as exc:
            log.exception("native script run failed")
            _deliver({"ok": False, "message": str(exc)})

    try:
        run_in_background(_native_script_run_worker, name="native-run-python-script", dedicated=True)
    except Exception as exc:
        log.exception("start_native_script_run: failed to schedule background worker")
        _deliver({"ok": False, "message": str(exc), "traceback": exception_traceback(exc)})
        raise


def native_run_script_modeless_enabled(ctx: Any = None) -> bool:
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

    _ctx: Any
    _doc: Any | None
    _modeless: bool
    _closed: bool
    _open_failure_detail: str | None
    _name_config_key: str

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
        self._open_failure_detail = None
        from plugin.scripting.python_runner import resolve_run_script_name_config_key

        self._name_config_key = resolve_run_script_name_config_key(initial_doc)

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
        if inst._open():
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
        names, merged, origin_map = build_xdl_script_picker_state(self._doc, get_user_scripts())
        self._current_scripts = merged
        self._script_origin_map = origin_map
        select_ctrl.removeItems(0, select_ctrl.getItemCount())
        select_ctrl.addItems(tuple(names), 0)

        config_key = getattr(self, "_name_config_key", None)
        if not config_key:
            from plugin.scripting.python_runner import resolve_run_script_name_config_key

            config_key = resolve_run_script_name_config_key(self._doc)
            self._name_config_key = config_key

        selected_name = ""
        if select_display and select_display in names:
            selected_name = select_display
        else:
            last_name = get_config_str(config_key)
            if last_name and last_name in names:
                selected_name = last_name
        if not selected_name and names:
            selected_name = names[0]

        dlg = getattr(self, "_dlg", None)
        if selected_name:
            _picker_select_name(select_ctrl, selected_name, names)
            set_config(config_key, selected_name)
            if dlg is not None:
                try:
                    code_ctrl = dlg.getControl("CodeEdit")
                    if code_ctrl is not None:
                        code_ctrl.setText(merged.get(selected_name, ""))
                except Exception as exc:
                    log.debug("Failed to set code edit text on refresh: %s", exc)
        else:
            # Clear editor when no scripts remain
            if dlg is not None:
                try:
                    code_ctrl = dlg.getControl("CodeEdit")
                    if code_ctrl is not None:
                        code_ctrl.setText("")
                except Exception as exc:
                    log.debug("Failed to clear code edit text on refresh: %s", exc)

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

            # Finite pre-warm. The shared daemon pool is for this. dedicated=True
            # is for servers, pipe drains, and jobs another thread joins.
            run_in_background(warm_venv_worker, ctx, name="warm-venv-worker", dedicated=False)

            select_ctrl = dlg.getControl("ScriptSelect")
            self._select_ctrl = select_ctrl

            # Re-initialize picker items and selection cleanly
            self._refresh_script_dropdown()
            self._wire_listeners(dlg, select_ctrl)

            code_ctrl = dlg.getControl("CodeEdit")
            if code_ctrl is not None:
                code_ctrl.setFocus()

            if self._modeless:
                owner = self

                class _TopWindowListener(unohelper.Base, XTopWindowListener):
                    def windowClosing(self, e: Any) -> None:
                        owner.close(toolkit_teardown=True)

                    def windowClosed(self, e: Any) -> None:
                        pass

                    def windowOpened(self, e: Any) -> None:
                        pass

                    def windowMinimized(self, e: Any) -> None:
                        pass

                    def windowNormalized(self, e: Any) -> None:
                        pass

                    def windowActivated(self, e: Any) -> None:
                        pass

                    def windowDeactivated(self, e: Any) -> None:
                        pass

                    def disposing(self, Source: EventObject) -> None:
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
            if not script_origin_is_library(origin):
                return _("Built-in helpers are read-only. Use Copy to My Scripts to customize.")
            if origin == SCRIPT_ORIGIN_DOCUMENT:
                if self._doc is None:
                    return _("No document is open to save scripts.")
                err = save_document_script(self._doc, real_name, t)
                if err:
                    if real_name in get_user_scripts():
                        return _("Cannot save to My Scripts: a script named '%s' already exists.") % real_name
                    save_user_script(real_name, t)
                    # After falling back to My Scripts, refresh the dropdown and
                    # select the saved user script. Leaving the [Doc] entry
                    # selected disagrees with where the script was stored.
                    self._refresh_script_dropdown(select_display=real_name)
                    return _("%s Saved to My Scripts instead.") % err
                return _("Script '%s' saved to this document.") % real_name
            else:
                save_user_script(real_name, t)
                return _("Script '%s' saved successfully.") % real_name
        return None

    def _store_script(
        self,
        name: str,
        code: str,
        *,
        attach_to_document: bool,
        dialog_title: str,
        doc_success_msg: str,
        user_success_msg: str,
    ) -> bool:
        """Shared storage and UI refresh helper for New and Save As."""
        ctx = self._ctx
        doc = self._doc
        dlg = self._dlg
        lbl = dlg.getControl("InstructionLbl") if dlg is not None else None

        if attach_to_document and doc is not None:
            from plugin.scripting.document_scripts import (
                document_script_display_name,
                get_document_scripts,
            )

            overwrite = name in get_document_scripts(doc)
            if overwrite and not show_approval_dialog(
                ctx,
                _("A script named '{0}' already exists in this document. Overwrite?").format(name),
                dialog_title,
            ):
                return False
            err = attach_document_script(doc, name, code, overwrite=True)
            if err:
                set_control_text(lbl, err)
                return False
            self._refresh_script_dropdown(document_script_display_name(name))
            set_control_text(lbl, doc_success_msg % name)
            return True
        else:
            if name in get_user_scripts() and not show_approval_dialog(
                ctx,
                _("A script named '{0}' already exists in My Scripts. Overwrite?").format(name),
                dialog_title,
            ):
                return False
            save_user_script(name, code)
            self._refresh_script_dropdown(name)
            set_control_text(lbl, user_success_msg % name)
            return True

    def _wire_listeners(self, dlg: Any, select_ctrl: Any) -> None:
        ctx = self._ctx
        owner = self
        doc = owner._doc

        class _ScriptSelectListener(unohelper.Base, XItemListener):
            def itemStateChanged(self, rEvent: ItemEvent) -> None:
                try:
                    name = _picker_selected_name(select_ctrl)
                    if name:
                        code_ctrl = dlg.getControl("CodeEdit")
                        set_config(owner._name_config_key, name)
                        t = owner._current_scripts.get(name, "")
                        code_ctrl.setText(t)
                except Exception:
                    log.exception("Failed to change script selection")

            def disposing(self, Source: EventObject) -> None:
                pass

        def _on_run() -> None:
            btn_run = None
            try:
                ec = dlg.getControl("CodeEdit")
                t = (ec.getModel().Text or "").rstrip()
                lbl = dlg.getControl("InstructionLbl")
                # Show the save message. Discarding it hid save warnings and a
                # fallback to My Scripts.
                save_msg = owner._save_current_script(t)
                if save_msg:
                    set_control_text(lbl, save_msg)
                btn_run = dlg.getControl("BtnRun")
                set_control_enabled(btn_run, False)
                set_control_text(lbl, _("Running..."))

                def _on_complete(outcome: dict[str, Any]) -> None:
                    try:
                        _report_run_outcome(ctx, lbl, outcome)
                        if not outcome.get("ok"):
                            set_control_text(lbl, str(outcome.get("message") or _("Execution Error")))
                    finally:
                        set_control_enabled(btn_run, True)

                start_native_script_run(ctx, doc, t, on_complete=_on_complete)
            except Exception as e:
                if btn_run is not None:
                    set_control_enabled(btn_run, True)
                log.exception("Run failed in dialog")
                msgbox(ctx, _("Error"), str(e))

        def _on_save() -> None:
            try:
                ec = dlg.getControl("CodeEdit")
                t = (ec.getModel().Text or "").rstrip()
                lbl = dlg.getControl("InstructionLbl")
                res = owner._save_current_script(t)
                if res:
                    set_control_text(lbl, res)
            except Exception as exc:
                _report_script_dialog_error(ctx, dlg, exc, "Save")

        def _on_save_as() -> None:
            try:
                ec = dlg.getControl("CodeEdit")
                t = (ec.getModel().Text or "").rstrip()

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

                owner._store_script(
                    name,
                    t,
                    attach_to_document=attach_to_document,
                    dialog_title=_("Save Script As"),
                    doc_success_msg=_("Script '%s' saved to this document."),
                    user_success_msg=_("Script '%s' saved to My Scripts."),
                )
            except Exception as exc:
                _report_script_dialog_error(ctx, dlg, exc, "Save As")

        def _on_delete() -> None:
            try:
                display_name = _picker_selected_name(select_ctrl)
                if not display_name:
                    return

                lbl = dlg.getControl("InstructionLbl")
                real_name, origin = resolve_script_picker_entry(display_name, owner._script_origin_map)
                if not script_origin_is_library(origin):
                    set_control_text(
                        lbl,
                        _("Built-in helpers are read-only. Use Copy to My Scripts to customize."),
                    )
                    return
                if show_approval_dialog(
                    ctx,
                    _("Are you sure you want to delete script '%s'?") % real_name,
                    _("Delete Script"),
                ):
                    if origin == SCRIPT_ORIGIN_DOCUMENT:
                        if doc is None:
                            set_control_text(lbl, _("No document is open."))
                            return
                        err = delete_document_script(doc, real_name)
                        if err:
                            set_control_text(lbl, err)
                            return
                    else:
                        delete_user_script(real_name)
                    owner._refresh_script_dropdown()
                    set_control_text(lbl, _("Script '%s' deleted.") % real_name)
            except Exception as exc:
                _report_script_dialog_error(ctx, dlg, exc, "Delete")

        def _on_new() -> None:
            try:
                res = show_new_script_dialog(ctx, doc=doc)
                if not res:
                    return
                name, attach_to_document = res
                name = name.strip()
                if not name:
                    return

                starter_code = '# A simple script\nresult = "Hello from Python!"\n'
                if owner._store_script(
                    name,
                    starter_code,
                    attach_to_document=attach_to_document,
                    dialog_title=_("New Script"),
                    doc_success_msg=_("Script '%s' created in this document."),
                    user_success_msg=_("Script '%s' created in My Scripts."),
                ):
                    ec = dlg.getControl("CodeEdit")
                    if ec is not None:
                        set_control_text(ec, starter_code)
            except Exception as exc:
                _report_script_dialog_error(ctx, dlg, exc, "New script")

        def _on_cancel() -> None:
            log.debug("native script dialog: BtnCancel")
            if owner._modeless:
                owner.close()
            else:
                dlg.endDialog(0)

        select_ctrl.addItemListener(_ScriptSelectListener())
        for btn_name, handler in (
            ("BtnRun", _on_run),
            ("BtnSave", _on_save),
            ("BtnNew", _on_new),
            ("BtnSaveAs", _on_save_as),
            ("BtnDelete", _on_delete),
            ("BtnCancel", _on_cancel),
        ):
            btn = dlg.getControl(btn_name)
            if btn is not None:
                btn.addActionListener(_action(handler, f"_{btn_name[3:]}Listener"))


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
    if outcome.get("no_output"):
        msgbox(ctx, _("Success"), status_text)
    elif outcome.get("stdout"):
        # Printed text belongs in this box even when the script also returned
        # a value. The result is inserted into the document; requiring
        # result is None hid that stdout.
        msgbox(ctx, _("Output"), outcome.get("stdout"))
    if lbl is not None:
        set_control_text(lbl, status_text)
