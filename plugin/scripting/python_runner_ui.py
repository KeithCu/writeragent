# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Plain-text Run Python Script dialog (Writer, Calc, and Draw/Impress)."""

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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal
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
    # The class name is how tests find a button listener (_RunListener, _SaveListener,
    # and the other _{Button}Listener names from btn_name[3:]).
    # tests/scripting/test_python_runner_ui.py and tests/scripting/test_python_runner_monaco.py
    # look up type(listener).__name__. Do not collapse these into _ActionListener.
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
    show_new_script_dialog,
)
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.document_scripts import (
    DocumentScriptErrorCode,
    attach_document_script,
    build_xdl_script_picker_state,
    delete_document_script,
    delete_user_script,
    get_user_scripts,
    read_only_helper_message,
    readonly_fallback_name_taken_message,
    readonly_fallback_saved_message,
    resolve_script_picker_entry,
    save_document_script,
    save_user_script,
    script_origin_is_writable,
)
from plugin.scripting.domain_registry import SCRIPT_ORIGIN_DOCUMENT, SCRIPT_ORIGIN_USER
from plugin.scripting.venv_worker import warm_venv_worker

log = logging.getLogger("writeragent.scripting")


@dataclass(frozen=True)
class _ScriptSaveResult:
    text: str | None
    channel: Literal["none", "ok", "builtin", "modal"]


def _builtin_helpers_readonly_text() -> str:
    # Monaco and the native dialog share this sentence. Save As, with Attach
    # unchecked, copies a helper into My Scripts. Do not add a Copy button and
    # do not edit the XDL.
    return read_only_helper_message()


def _report_script_dialog_error(ctx: Any, dlg: Any, exc: BaseException, where: str) -> None:
    """Show Save / Save As / Delete / New failures. Those buttons used to only log."""
    log.exception("%s failed in dialog", where)
    from plugin.framework.errors import is_disposed_exception

    text = _("The document is no longer open.") if is_disposed_exception(exc) else str(exc)
    lbl = dlg.getControl("InstructionLbl")
    if lbl is not None:
        set_control_text(lbl, text)
        return
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
    thread. After the worker is scheduled, ``on_complete`` is called exactly
    once on the main thread. A non-dict from preparation is an error outcome,
    not a silent return.
    """
    from plugin.framework.queue_executor import execute_on_main_thread, post_to_main_thread
    from plugin.scripting.editor_errors import exception_traceback
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
        prepared: dict[str, Any] | None = None
        try:
            prepared = execute_on_main_thread(
                _prepare_rps_execution, ctx, doc, code, data_range=data_range
            )
            if not isinstance(prepared, dict):
                # What was wrong: a non-dict from prepare returned without on_complete, so BtnRun stayed disabled.
                # How it happened: the isinstance guard avoided calling .get on a bad value and returned before _deliver.
                # Why this change fixes it: the worker still posts one error outcome. A non-dict has no script_session_id, so nothing is released.
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
                from plugin.scripting.session_manager import release_script_document

                release_script_document(prepared.get("script_session_id"))
                log.exception("native script run failed")
                _deliver(rps_error_outcome(str(exc), t0=prepared["t0"], traceback=exception_traceback(exc)))
                return
            try:
                outcome = execute_on_main_thread(_finish_rps_execution, prepared, response)
            except Exception:
                # What was wrong: Main-thread execution failure or timeout leaked the pinned document token.
                # How it happened: When execute_on_main_thread raised (e.g. timeout), _finish_rps_execution's finally never ran.
                # Why this change fixes it: Catch exceptions around finish marshal, release the pinned document token, and re-raise.
                from plugin.scripting.session_manager import release_script_document

                release_script_document(prepared.get("script_session_id"))
                raise
            _deliver(outcome)
        except Exception as exc:
            log.exception("native script run failed")
            _deliver({"ok": False, "message": str(exc)})

    try:
        run_in_background(_native_script_run_worker, name="native-run-python-script", dedicated=True)
    except Exception:
        # What was wrong: Delivering an error outcome on scheduling failure resulted in duplicate error reporting.
        # How it happened: run_in_background raised, and the except block both delivered to on_complete and re-raised to caller.
        # Why this change fixes it: Log and re-raise without calling _deliver, leaving error reporting solely to the caller.
        log.exception("start_native_script_run: failed to schedule background worker")
        raise


def native_run_script_modeless_enabled() -> bool:
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
                # What was wrong: the combobox could show the XDL default empty label, or the previous name,
                # while the editor and the selected index were the script just chosen.
                # How it happened: ScriptSelect is a spin="true" combobox (extension/Dialogs/PythonScriptDialog.xdl,
                # dlg:value=""). selectItemPos does not reliably update that text field, and this function returned
                # before setText. set_selector_mode in plugin/chatbot/chat_sidebar_mode.py calls both for that reason.
                # test_picker_select_name_combobox_uses_set_text uses MagicMock(spec=[]), which has no selectItemPos,
                # so it never took the return.
                # Why this change fixes it: selectItemPos still runs when the name is in the list, and setText
                # still runs when the control has it.
                select_ctrl.selectItemPos(idx, True)
                break
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
    _open_failure_detail: str | None = None
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
        # Local import: python_runner imports this module at module level.
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
        dlg.setVisible(False)
        dlg.dispose()

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
            # Local import: python_runner imports this module at module level.
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
                code_ctrl = dlg.getControl("CodeEdit")
                if code_ctrl is not None:
                    code_ctrl.setText(merged.get(selected_name, ""))
        else:
            # Clear editor when no scripts remain
            if dlg is not None:
                code_ctrl = dlg.getControl("CodeEdit")
                if code_ctrl is not None:
                    code_ctrl.setText("")

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
            # What was wrong: a submit failure disposed a dialog that was already loaded.
            # How it happened: this call sits in _open's try without its own handler.
            # Why this change: pre-warm is optional, so catch and log scheduling failures.
            try:
                run_in_background(warm_venv_worker, ctx, name="warm-venv-worker", dedicated=False)
            except Exception:
                log.exception("native script dialog: venv warm-up was not scheduled")

            select_ctrl = dlg.getControl("ScriptSelect")
            self._select_ctrl = select_ctrl

            # Re-initialize picker items and selection cleanly
            self._refresh_script_dropdown()
            self._wire_listeners(dlg, select_ctrl)

            code_ctrl = dlg.getControl("CodeEdit")
            if code_ctrl is not None and not self._modeless:
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
                # What was wrong: the editor did not take focus on the floating dialog.
                # How it happened: setFocus ran before setVisible(True). Modal execute() is different:
                # it shows the dialog and blocks, so focus stays before execute().
                # Why this change fixes it: the modeless dialog focuses CodeEdit after it is visible,
                # and a focus error is logged instead of disposing the dialog.
                if code_ctrl is not None:
                    code_ctrl.setFocus()
                return True
            dlg.execute()
            self._closed = True
            self._dlg = None
            dlg.dispose()
            return True
        except Exception as exc:
            from plugin.scripting.editor_errors import exception_traceback

            log.exception("NativePythonScriptDialog._open failed")
            self._open_failure_detail = exception_traceback(exc)
            self.close()
            return False

    def _save_current_script(self, t: str) -> _ScriptSaveResult:
        select_ctrl = self._select_ctrl
        if select_ctrl is None:
            return _ScriptSaveResult(None, "none")
        display_name = _picker_selected_name(select_ctrl)
        if not display_name:
            return _ScriptSaveResult(None, "none")
        real_name, origin = resolve_script_picker_entry(display_name, self._script_origin_map)
        self._current_scripts[display_name] = t
        if not script_origin_is_writable(origin):
            return _ScriptSaveResult(_builtin_helpers_readonly_text(), "builtin")
        if origin == SCRIPT_ORIGIN_DOCUMENT:
            if self._doc is None:
                return _ScriptSaveResult(_("No document is open to save scripts."), "modal")
            err = save_document_script(self._doc, real_name, t)
            # What was wrong: any document error used to be copied into My Scripts,
            # and the label either hid the document failure or glued str(err) onto
            # a second sentence.
            # How it happened: an unconditional fallback ran for all non-None err returns.
            # Why this change: Monaco already limits the fallback to READONLY so
            # TOO_LARGE and reserved names are not skipped. This branch matches that,
            # and both READONLY sentences name the document failure.
            if err is None:
                return _ScriptSaveResult(_("Script '%s' saved to this document.") % real_name, "ok")
            if getattr(err, "code", None) != DocumentScriptErrorCode.READONLY:
                return _ScriptSaveResult(str(err), "modal")
            if real_name in get_user_scripts():
                return _ScriptSaveResult(
                    readonly_fallback_name_taken_message(real_name, err),
                    "modal",
                )
            user_err = save_user_script(real_name, t)
            if isinstance(user_err, str) and user_err:
                return _ScriptSaveResult(str(user_err), "modal")
            # After falling back to My Scripts, refresh the dropdown and
            # select the saved user script. Leaving the [Doc] entry
            # selected disagrees with where the script was stored.
            self._refresh_script_dropdown(select_display=real_name)
            return _ScriptSaveResult(
                readonly_fallback_saved_message(real_name, err),
                "modal",
            )
        user_err = save_user_script(real_name, t)
        if isinstance(user_err, str) and user_err:
            return _ScriptSaveResult(str(user_err), "modal")
        return _ScriptSaveResult(_("Script '%s' saved successfully.") % real_name, "ok")

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
            err = save_user_script(name, code)
            if isinstance(err, str) and err:
                set_control_text(lbl, err)
                return False
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
            lbl = None
            try:
                ec = dlg.getControl("CodeEdit")
                t = (ec.getModel().Text or "").rstrip()
                lbl = dlg.getControl("InstructionLbl")
                # What was wrong: the save sentence was written and then replaced by
                # Running..., and completion replaced the label again.
                # How it happened: 1682d0493 displayed save_msg and the next line
                # always set Running...
                # Why this change: builtin stays on the label through a successful
                # finish; every other warning is a Save box before Running...
                save_res = owner._save_current_script(t)
                save_channel = save_res.channel
                save_text = save_res.text

                if save_channel == "builtin":
                    if save_text:
                        set_control_text(lbl, save_text)
                elif save_channel == "modal":
                    if save_text:
                        msgbox(ctx, _("Save"), save_text)

                btn_run = dlg.getControl("BtnRun")
                set_control_enabled(btn_run, False)

                if save_channel != "builtin":
                    set_control_text(lbl, _("Running..."))

                def _on_complete(outcome: dict[str, Any]) -> None:
                    try:
                        _report_run_outcome(ctx, lbl, outcome)
                        if not outcome.get("ok"):
                            set_control_text(lbl, str(outcome.get("message") or _("Execution Error")))
                        elif save_channel == "builtin" and save_text:
                            set_control_text(lbl, save_text)
                    finally:
                        set_control_enabled(btn_run, True)

                start_native_script_run(ctx, doc, t, on_complete=_on_complete)
            except Exception as e:
                if btn_run is not None:
                    set_control_enabled(btn_run, True)
                log.exception("Run failed in dialog")
                msgbox(ctx, _("Error"), str(e))
                if lbl is not None:
                    set_control_text(lbl, str(e))

        def _on_save() -> None:
            try:
                ec = dlg.getControl("CodeEdit")
                t = (ec.getModel().Text or "").rstrip()
                lbl = dlg.getControl("InstructionLbl")
                res = owner._save_current_script(t)
                if res.text:
                    set_control_text(lbl, res.text)
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
                if not script_origin_is_writable(origin):
                    set_control_text(
                        lbl,
                        _builtin_helpers_readonly_text(),
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
                        # What was wrong: a missing My Scripts row was reported as deleted.
                        # How it happened: the NOT_FOUND return from delete_user_script was ignored.
                        # Why this change: match the document branch by checking err, displaying it,
                        # and returning before dropdown refresh and success reporting.
                        err = delete_user_script(real_name)
                        if err is not None:
                            set_control_text(lbl, err)
                            return
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
        modeless = native_run_script_modeless_enabled()
        return NativePythonScriptDialog.show(
            ctx,
            doc=doc,
            modeless=modeless,
        )
    except Exception as exc:
        from plugin.scripting.editor_errors import exception_traceback

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
