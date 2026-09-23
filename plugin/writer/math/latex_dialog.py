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
from plugin.framework.config import get_config, get_config_str, set_config
from plugin.chatbot.dialogs import load_writeragent_dialog, msgbox, msgbox_with_report
from plugin.framework.i18n import _
from plugin.doc.doc_type import is_writer
from plugin.writer.math.math_mml_convert import (
    convert_latex_to_starmath,
    insert_writer_math_formula,
    replace_writer_math_formula,
)
from plugin.scripting.editor_host import launch_monaco_editor, monaco_editor_available

log = logging.getLogger("writeragent.writer")


def show_latex_input_dialog(
    ctx: Any,
    initial_text: str = "",
    initial_display: bool = False,
    *,
    update: bool = False,
) -> tuple[str, bool] | None:
    """Show a modal multiline dialog for entering LaTeX code and checkbox choice.

    Returns tuple (latex_string, display_block) if Insert/Update is clicked, else None.
    """
    try:
        dlg = load_writeragent_dialog("LatexInputDialog", ctx)
        if dlg is None:
            return None

        # Populate initial values
        edit = dlg.getControl("LatexEdit")
        if edit is not None:
            edit.setText(initial_text)
            # Use a monospaced font
            fd = cast("Any", uno.createUnoStruct("com.sun.star.awt.FontDescriptor"))
            fd.Name = "Courier New"
            edit.getModel().FontDescriptor = fd

        cbc = dlg.getControl("DisplayBlockCheck")
        if cbc is not None:
            cbc.getModel().State = 1 if initial_display else 0

        _outcome: list[tuple[str, bool] | None] | None = None

        class _InsertListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: Any) -> None:
                nonlocal _outcome
                try:
                    ec = dlg.getControl("LatexEdit")
                    t = (ec.getModel().Text or "").strip()
                except Exception:
                    t = ""

                try:
                    cb = dlg.getControl("DisplayBlockCheck")
                    db = (cb.getModel().State == 1)
                except Exception:
                    db = False

                _outcome = [(t, db)]
                dlg.endDialog(1)

            def disposing(self, Source: Any) -> None:
                pass

        class _CancelListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: Any) -> None:
                nonlocal _outcome
                _outcome = [None]
                dlg.endDialog(0)

            def disposing(self, Source: Any) -> None:
                pass

        btn_insert = dlg.getControl("BtnInsert")
        if btn_insert is not None:
            try:
                btn_insert.getModel().Label = _("Update") if update else _("Insert")
            except Exception:
                log.debug("LatexInputDialog: could not set BtnInsert label", exc_info=True)
            btn_insert.addActionListener(_InsertListener())
        btn_cancel = dlg.getControl("BtnCancel")
        if btn_cancel is not None:
            btn_cancel.addActionListener(_CancelListener())

        # Set focus to the edit control
        if edit is not None:
            edit.setFocus()

        dlg.execute()
        dlg.dispose()

        if _outcome is None:
            return None
        return _outcome[0]
    except Exception:
        log.exception("show_latex_input_dialog failed")
        return None


def _selection_latex_prefill(
    ctx: Any, doc: Any, last_latex: str, last_display: bool
) -> tuple[str | None, str, bool, bool]:
    """Inspect selection: abort on multiple Math objects; prefill TeX for one.

    Returns ``(embed_name_or_none, latex, display, abort)``.
    """
    from plugin.writer.math.math_mml_export import (
        convert_starmath_to_latex,
        resolve_math_embed_name,
        selected_math_embeds,
    )

    try:
        hits = selected_math_embeds(doc)
    except Exception:
        log.debug("latex dialog: selected_math_embeds failed", exc_info=True)
        return None, last_latex, last_display, False

    if len(hits) > 1:
        msgbox(
            ctx,
            _("Edit LaTeX Math"),
            _("Select a single formula to update. Multiple formulas are selected."),
        )
        return None, last_latex, last_display, True

    if len(hits) != 1:
        return None, last_latex, last_display, False

    embed = hits[0]
    name = resolve_math_embed_name(doc, embed)
    if not name:
        msgbox(
            ctx,
            _("Edit LaTeX Math"),
            _("Could not identify the selected formula. Click in the text, then insert a new formula."),
        )
        return None, last_latex, last_display, True
    try:
        inner = embed.getEmbeddedObject() if hasattr(embed, "getEmbeddedObject") else embed
        starmath = str(getattr(inner, "Formula", "") or "").strip()
        if starmath:
            exported = convert_starmath_to_latex(ctx, starmath)
            if exported.ok and exported.latex:
                last_latex = exported.latex
    except Exception:
        log.debug("latex dialog: selection Math → LaTeX prefill failed", exc_info=True)
    return name, last_latex, last_display, False


def _apply_latex_to_writer(
    ctx: Any, doc: Any, latex: str, display_block: bool, target_name: str | None
) -> tuple[bool, str]:
    """Convert LaTeX and either replace the named embed or insert a new one."""
    conv_res = convert_latex_to_starmath(ctx, latex, display_block=display_block)
    if not conv_res.ok:
        error_msg = conv_res.error_message or _("Unknown conversion error")
        return False, _("Failed to convert LaTeX to StarMath:\n\n{0}").format(error_msg)

    set_config("last_latex_input", latex)
    set_config("last_latex_display_block", display_block)
    starmath = conv_res.starmath or ""

    if target_name:
        from plugin.writer.math.math_mml_export import lookup_math_embed

        embed = lookup_math_embed(doc, target_name)
        if embed is None:
            return False, _("The selected formula is no longer in the document.")
        try:
            replace_writer_math_formula(embed, starmath)
        except Exception as exc:
            log.exception("replace_writer_math_formula failed")
            return False, str(exc)
        return True, _("Formula updated.")

    controller = doc.getCurrentController()
    view_cursor = controller.getViewCursor()
    insert_writer_math_formula(doc, view_cursor, starmath, display_block=display_block)
    return True, _("Formula inserted.")


def insert_latex_math_dialog(ctx: Any) -> None:
    """Entry point for inserting LaTeX Math into Writer via a dialog."""
    try:
        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent()
        if doc is None or not is_writer(doc):
            msgbox(ctx, _("Error"), _("This command is only available in Writer documents."))
            return

        last_latex = get_config_str("last_latex_input")
        last_display = bool(get_config("last_latex_display_block"))
        target_name, last_latex, last_display, abort = _selection_latex_prefill(
            ctx, doc, last_latex, last_display
        )
        if abort:
            return

        # Check if Monaco editor is available
        exe, available = monaco_editor_available(ctx)
        if available and exe:
            log.info("insert_latex_math_dialog: using Monaco editor")

            def on_save(code: str, save_as_plain: bool, data_binding: str | None = None, _action: str = "cell_save") -> dict[str, Any]:
                # save_as_plain checkbox represents display_block for LaTeX editor!
                display_block = save_as_plain
                if not code:
                    return {"type": "saved", "ok": True}

                ok, status = _apply_latex_to_writer(ctx, doc, code, display_block, target_name)
                if not ok:
                    return {"type": "error", "message": status}
                return {"type": "saved", "ok": True, "status_ok_text": status}

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
                "save_label": _("Update") if target_name else _("Insert"),
                "close_label": _("Close"),
                "show_plain_text": True,
                "show_data_binding": False,
                "resource": target_name or "insert",
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
        res = show_latex_input_dialog(
            ctx, initial_text=last_latex, initial_display=last_display, update=bool(target_name)
        )
        if res is None:
            return  # Cancelled

        latex, display_block = res
        if not latex:
            return  # Empty, do nothing

        ok, status = _apply_latex_to_writer(ctx, doc, latex, display_block, target_name)
        if not ok:
            msgbox(ctx, _("LaTeX Conversion Error"), status)

    except Exception:
        log.exception("insert_latex_math_dialog failed")
        try:
            msgbox_with_report(
                ctx,
                _("Error"),
                _("An unexpected error occurred during LaTeX insertion."),
                box_type=3,
                reportable=True,
                report_title="LaTeX insertion failed",
            )
        except Exception:
            pass
