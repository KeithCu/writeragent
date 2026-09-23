# WriterAgent - Scripting Dialogs
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scripting dialogs: new script / save script template dialog."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import unohelper
from com.sun.star.awt import XActionListener

if TYPE_CHECKING:
    from com.sun.star.awt import ActionEvent
    from com.sun.star.lang import EventObject

from plugin.framework.i18n import _
from plugin.chatbot.dialogs import load_writeragent_dialog, msgbox, show_text_input_dialog

log = logging.getLogger(__name__)


def show_new_script_dialog(
    ctx: Any,
    doc: Any | None = None,
    default_name: str = "",
    title: str = "",
    default_attach: bool | None = None,
) -> tuple[str, bool] | None:
    """Modal dialog for creating or saving a Python script with name and attach checkbox.

    Returns ``(script_name, attach_to_document)`` on OK, or ``None`` on Cancel.
    """
    if not ctx:
        log.warning("show_new_script_dialog: no ctx")
        return None
    try:
        from plugin.scripting.document_scripts import is_document_readonly_for_scripts

        can_attach = doc is not None and not is_document_readonly_for_scripts(doc)
        dialog_title = title or _("New Python Script")

        dlg = load_writeragent_dialog("NewScriptDialog", ctx)
        if dlg is None:
            name = show_text_input_dialog(ctx, _("Script name:"), dialog_title, default_name)
            if not name:
                return None
            return (name, False)

        dlg.getModel().Title = dialog_title

        lbl = dlg.getControl("PromptLbl")
        if lbl is not None:
            lbl.getModel().Label = _("Script name:")

        edit = dlg.getControl("NameEdit")
        if edit is not None:
            edit.setText(default_name or "")

        chk = dlg.getControl("ChkAttach")
        if chk is not None:
            chk.getModel().Label = _("Attach to this document")
            initial_attach = can_attach if default_attach is None else (default_attach and can_attach)
            chk.getModel().State = 1 if initial_attach else 0
            chk.getModel().Enabled = bool(can_attach)

        _outcome: list[tuple[str, bool] | None] | None = None

        class _OkListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: ActionEvent):
                nonlocal _outcome
                try:
                    ec = dlg.getControl("NameEdit")
                    t = (ec.getModel().Text or "").strip() if ec and ec.getModel() else ""
                except Exception:
                    t = ""
                if not t:
                    msgbox(ctx, _("Error"), _("Script name cannot be empty."))
                    return
                attach = False
                try:
                    cc = dlg.getControl("ChkAttach")
                    attach = bool(cc.getModel().State == 1) if cc and cc.getModel() else False
                except Exception:
                    pass
                _outcome = [(t, attach)]
                dlg.endDialog(1)

            def disposing(self, Source: EventObject):
                pass

        class _CancelListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: ActionEvent):
                nonlocal _outcome
                _outcome = [None]
                dlg.endDialog(0)

            def disposing(self, Source: EventObject):
                pass

        btn_ok = dlg.getControl("BtnOK")
        if btn_ok is not None:
            btn_ok.addActionListener(_OkListener())
        btn_cancel = dlg.getControl("BtnCancel")
        if btn_cancel is not None:
            btn_cancel.addActionListener(_CancelListener())

        if edit is not None:
            edit.setFocus()
        dlg.execute()
        dlg.dispose()
        if _outcome is None:
            return None
        return _outcome[0]
    except Exception:
        log.exception("show_new_script_dialog failed")
        return None
