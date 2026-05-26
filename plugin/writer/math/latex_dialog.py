# WriterAgent - LaTeX Math Insertion Dialog
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Provides a modal dialog for inserting LaTeX equations converted locally to StarMath."""

from __future__ import annotations

import logging
from typing import Any, cast
import uno
import unohelper
from com.sun.star.awt import XActionListener

from plugin.framework.uno_context import get_desktop
from plugin.framework.config import get_config, set_config
from plugin.chatbot.dialogs import add_dialog_label, add_dialog_edit, add_dialog_button, msgbox
from plugin.framework.i18n import _
from plugin.doc.document_helpers import is_writer
from plugin.writer.math.math_mml_convert import convert_latex_to_starmath, insert_writer_math_formula
from plugin.scripting.editor_session_launch import launch_monaco_editor, monaco_editor_available

log = logging.getLogger("writeragent.writer")


def add_dialog_checkbox(dlg_model: Any, name: str, label: str, x: int, y: int, width: int, height: int, checked: bool = False) -> Any:
    """Add a checkbox control to a dialog model."""
    cb = dlg_model.createInstance("com.sun.star.awt.UnoControlCheckBoxModel")
    cb.Name = name
    cb.PositionX = x
    cb.PositionY = y
    cb.Width = width
    cb.Height = height
    cb.Label = _(label)
    cb.State = 1 if checked else 0
    dlg_model.insertByName(name, cb)
    return cb


def show_latex_input_dialog(ctx: Any, initial_text: str = "", initial_display: bool = False) -> tuple[str, bool] | None:
    """Show a modal multiline dialog for entering LaTeX code and checkbox choice.

    Returns tuple (latex_string, display_block) if Insert/OK is clicked, else None.
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
        dlg_model.Title = _("Insert LaTeX Math")
        dlg_model.Width = 350
        dlg_model.Height = 180

        add_dialog_label(dlg_model, "InstructionLbl", _("Enter LaTeX math code to convert and insert as formula:"), 8, 8, 334, 12, multiline=False)
        edit = add_dialog_edit(dlg_model, "LatexEdit", initial_text, 8, 24, 334, 100)
        edit.MultiLine = True
        edit.VScroll = True
        # Use a monospaced font
        fd = cast("Any", uno.createUnoStruct("com.sun.star.awt.FontDescriptor"))
        fd.Name = "Courier New"
        edit.FontDescriptor = fd

        # Add the checkbox: "Insert as display block (centered paragraph)"
        add_dialog_checkbox(dlg_model, "DisplayBlockCheck", _("Insert as display block (centered paragraph)"), 8, 130, 334, 14, checked=initial_display)

        add_dialog_button(dlg_model, "BtnInsert", _("Insert"), 220, 154, 60, 14)
        add_dialog_button(dlg_model, "BtnCancel", _("Cancel"), 286, 154, 56, 14)

        dlg = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
        dlg.setModel(dlg_model)
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        dlg.createPeer(toolkit, parent_window)

        _outcome: list[tuple[str, bool] | None] | None = None

        class _InsertListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent):
                nonlocal _outcome
                try:
                    ec = dlg.getControl("LatexEdit")
                    t = (ec.getModel().Text or "").strip()
                except Exception:
                    t = ""

                try:
                    cbc = dlg.getControl("DisplayBlockCheck")
                    db = (cbc.getModel().State == 1)
                except Exception:
                    db = False

                _outcome = [(t, db)]
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

        dlg.getControl("BtnInsert").addActionListener(_InsertListener())
        dlg.getControl("BtnCancel").addActionListener(_CancelListener())

        # Set focus to the edit control
        dlg.getControl("LatexEdit").setFocus()

        dlg.execute()
        dlg.dispose()

        if _outcome is None:
            return None
        return _outcome[0]
    except Exception:
        log.exception("show_latex_input_dialog failed")
        return None


def insert_latex_math_dialog(ctx: Any) -> None:
    """Entry point for inserting LaTeX Math into Writer via a dialog."""
    try:
        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent()
        if doc is None or not is_writer(doc):
            msgbox(ctx, _("Error"), _("This command is only available in Writer documents."))
            return

        last_latex = str(get_config(ctx, "last_latex_input") or "e = m c^2")
        last_display = bool(get_config(ctx, "last_latex_display_block"))

        # Check if Monaco editor is available
        exe, available = monaco_editor_available(ctx)
        if available and exe:
            log.info("insert_latex_math_dialog: using Monaco editor")

            def on_save(code: str, save_as_plain: bool, data_binding: str | None = None, _action: str = "cell_save") -> dict[str, Any]:
                # save_as_plain checkbox represents display_block for LaTeX editor!
                display_block = save_as_plain
                if not code:
                    return {"type": "saved", "ok": True}

                # Convert to StarMath
                conv_res = convert_latex_to_starmath(ctx, code, display_block=display_block)
                if not conv_res.ok:
                    error_msg = conv_res.error_message or _("Unknown conversion error")
                    return {"type": "error", "message": error_msg}

                # Save settings
                set_config(ctx, "last_latex_input", code)
                set_config(ctx, "last_latex_display_block", display_block)

                # Insert into document
                controller = doc.getCurrentController()
                view_cursor = controller.getViewCursor()
                insert_writer_math_formula(doc, view_cursor, conv_res.starmath or "", display_block=display_block)
                return {"type": "saved", "ok": True, "status_ok_text": _("Formula inserted.")}

            def on_closed() -> None:
                log.debug("LaTeX Monaco editor closed")

            load_msg: dict[str, Any] = {
                "type": "load",
                "mode": "latex",
                "language": "latex",
                "code": last_latex,
                "title": _("LaTeX Math Editor"),
                "plain_text_label": _("Insert as display block (centered paragraph)"),
                "save_as_plain": last_display,
                "save_label": _("Insert"),
                "show_plain_text": True,
                "show_data_binding": False,
            }
            launch_monaco_editor(
                ctx,
                exe=exe,
                load_message=load_msg,
                on_save=on_save,
                on_closed=on_closed,
            )
            return

        # Otherwise, fall back cleanly to native dialog
        res = show_latex_input_dialog(ctx, initial_text=last_latex, initial_display=last_display)
        if res is None:
            return  # Cancelled

        latex, display_block = res
        if not latex:
            return  # Empty, do nothing

        # Save settings
        set_config(ctx, "last_latex_input", latex)
        set_config(ctx, "last_latex_display_block", display_block)

        # Convert to StarMath
        conv_res = convert_latex_to_starmath(ctx, latex, display_block=display_block)
        if not conv_res.ok:
            error_msg = conv_res.error_message or _("Unknown conversion error")
            msgbox(ctx, _("LaTeX Conversion Error"), _("Failed to convert LaTeX to StarMath:\n\n{0}").format(error_msg))
            return

        # Insert into document
        controller = doc.getCurrentController()
        view_cursor = controller.getViewCursor()

        insert_writer_math_formula(doc, view_cursor, conv_res.starmath or "", display_block=display_block)

    except Exception:
        log.exception("insert_latex_math_dialog failed")
        try:
            msgbox(ctx, _("Error"), _("An unexpected error occurred during LaTeX insertion."))
        except Exception:
            pass
