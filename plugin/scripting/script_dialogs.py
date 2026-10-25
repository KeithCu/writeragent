# WriterAgent - Scripting Dialogs
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scripting dialogs: new script / save script template dialog."""

from __future__ import annotations

import logging
from typing import Any

from plugin.chatbot.dialogs import (
    get_checkbox_state,
    get_control_text,
    load_writeragent_dialog,
    msgbox,
    set_checkbox_state,
    set_control_enabled,
    set_control_text,
    show_text_input_dialog,
)
from plugin.framework.i18n import _
from plugin.framework.uno_listeners import BaseActionListener

log = logging.getLogger(__name__)


def show_new_script_dialog(
    ctx: Any,
    doc: Any | None = None,
    default_name: str = "",
    title: str = "",
    default_attach: bool = True,
) -> tuple[str, bool] | None:
    """Modal dialog for creating or saving a Python script with name and attach checkbox.

    Returns ``(script_name, attach_to_document)`` on OK, or ``None`` on Cancel.
    """
    if not ctx:
        log.warning("show_new_script_dialog: no ctx")
        return None
    try:
        from plugin.scripting.document_scripts import (
            is_document_readonly_for_scripts,
            new_script_name_error,
        )

        can_attach = doc is not None and not is_document_readonly_for_scripts(doc)
        dialog_title = title or _("New Python Script")

        dlg = load_writeragent_dialog("NewScriptDialog", ctx)
        if dlg is None:
            name = show_text_input_dialog(ctx, _("Script name:"), dialog_title, default_name)
            if not name:
                return None
            # No checkbox was shown. Do not honor default_attach: that would
            # write a document script the user did not confirm. INIT is allowed
            # because this path stores a My Scripts name.
            message = new_script_name_error(name, attach=False)
            if message is not None:
                msgbox(ctx, _("Error"), message)
                return None
            return (name, False)

        try:
            dlg.getModel().Title = dialog_title

            edit = dlg.getControl("NameEdit")
            if edit is not None:
                set_control_text(edit, default_name or "")

            chk = dlg.getControl("ChkAttach")
            if chk is not None:
                initial_attach = default_attach and can_attach
                if not can_attach:
                    reason = (
                        _("Attach to this document (no document is open)")
                        if doc is None
                        else _("Attach to this document (this document is read-only)")
                    )
                    set_control_text(chk, reason)
                    chk.getModel().HelpText = reason
                set_checkbox_state(chk, 1 if initial_attach else 0)
                set_control_enabled(chk, bool(can_attach))

            outcome: tuple[str, bool] | None = None

            class _OkListener(BaseActionListener):
                def on_action_performed(self, rEvent: Any) -> None:
                    nonlocal outcome
                    ec = dlg.getControl("NameEdit")
                    t = (get_control_text(ec) or "").strip()
                    cc = dlg.getControl("ChkAttach")
                    # A disabled box can still report State 1. can_attach is the
                    # same predicate that disabled it.
                    attach = bool(can_attach and get_checkbox_state(cc) == 1)
                    # What was wrong: OK accepted INIT/Init while Attach was checked.
                    # How it happened: the dialog only called is_reserved_script_name.
                    # Those spellings are not display prefixes. attach_document_script
                    # rejects them because they are the workbook init keys.
                    # Why this works: new_script_name_error is that same predicate,
                    # so the dialog stays open on the storage sentence.
                    message = new_script_name_error(t, attach=attach)
                    if message is not None:
                        peer = None
                        try:
                            get_peer = getattr(dlg, "getPeer", None)
                            if callable(get_peer):
                                peer = get_peer()
                        except Exception:
                            log.debug(
                                "show_new_script_dialog: dialog peer unavailable",
                                exc_info=True,
                            )
                        # What was wrong: this msgbox used the document frame while
                        # execute() was still on the stack, so the box could open
                        # under the modal dialog.
                        # Why this works: parent_window is the dialog peer. None
                        # keeps msgbox's frame parent.
                        msgbox(ctx, _("Error"), message, parent_window=peer)
                        return
                    outcome = (t, attach)
                    dlg.endDialog(1)

            class _CancelListener(BaseActionListener):
                def on_action_performed(self, rEvent: Any) -> None:
                    nonlocal outcome
                    outcome = None
                    dlg.endDialog(0)

            btn_ok = dlg.getControl("BtnOK")
            if btn_ok is not None:
                btn_ok.addActionListener(_OkListener())
            btn_cancel = dlg.getControl("BtnCancel")
            if btn_cancel is not None:
                btn_cancel.addActionListener(_CancelListener())

            if edit is not None:
                try:
                    edit.setFocus()
                except Exception:
                    # What was wrong: setFocus ran before the dispose finally.
                    # A focus failure skipped execute() and leaked the dialog.
                    # Why this works: focus is best-effort; dispose still runs.
                    log.debug("show_new_script_dialog: setFocus failed", exc_info=True)
            dlg.execute()
            return outcome
        finally:
            try:
                dlg.dispose()
            except Exception:
                log.debug("show_new_script_dialog: dispose failed", exc_info=True)
    except Exception:
        log.exception("show_new_script_dialog failed")
        return None
