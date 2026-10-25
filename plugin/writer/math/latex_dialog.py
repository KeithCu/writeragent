# WriterAgent - LaTeX Math Insertion Dialog
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Provides a modal dialog for inserting LaTeX equations converted locally to StarMath."""

from __future__ import annotations

import logging
from typing import Any, Callable, NamedTuple, cast
import uno

from plugin.framework.uno_listeners import BaseActionListener
from plugin.framework.uno_context import get_active_document
from plugin.framework.config import get_config, get_config_str, set_config
from plugin.framework.errors import best_effort, resolve_exception_message
from plugin.chatbot.dialogs import (
    get_checkbox_state,
    get_control_text,
    load_writeragent_dialog,
    msgbox,
    msgbox_with_report,
)
from plugin.framework.i18n import _
from plugin.doc.doc_type import is_writer
from plugin.writer.math.math_mml_convert import (
    convert_latex_to_starmath,
    insert_writer_math_formula,
    replace_writer_math_formula,
)
# What was wrong: Importing individual helper functions from math_mml_export into latex_dialog
#   bound them locally at module load time. UNO and unit tests patch plugin.writer.math.math_mml_export.*,
#   which failed to affect latex_dialog's calls to the unpatched local references.
# How it happened: cf457aa16 (#1984) replaced module imports with direct function imports.
# Why this change: Keep math_mml_export imported as a module and access helpers as module attributes
#   (math_mml_export.selected_math_embeds, etc.) so test patches and dynamic overrides take effect.
from plugin.writer.math import math_mml_export
from plugin.scripting.editor_host import launch_monaco_editor, monaco_editor_available

log = logging.getLogger("writeragent.writer")


class _ClickListener(BaseActionListener):
    _callback: Callable[[], None]

    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self._callback = callback

    def actionPerformed(self, rEvent: Any) -> None:
        self._callback()


class _SelectionLatexPrefill(NamedTuple):
    name: str | None
    latex: str
    display: bool
    abort: bool


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
    # What was wrong: dlg.dispose() ran only on the success path, leaking dialog resources on
    #   exceptions during control setup or execute, while dialog load/runtime errors silently returned
    #   None, appearing to the caller as user cancellation.
    # How it happened: Dialog initialization, execution, and disposal were kept in a basic try/except
    #   block without a finally clause or error reporting.
    # Why this change: Dispose dlg safely in a finally block, and report load or runtime failures
    #   via msgbox so internal errors are visible instead of silently mimicking a cancel.
    dlg: Any = None
    try:
        dlg = load_writeragent_dialog("LatexInputDialog", ctx)
        if dlg is None:
            msgbox(ctx, _("Error"), _("Could not open the LaTeX input dialog."))
            return None

        # Populate initial values
        edit = dlg.getControl("LatexEdit")
        edit.setText(initial_text)
        # Use a monospaced font
        fd = cast("Any", uno.createUnoStruct("com.sun.star.awt.FontDescriptor"))
        fd.Name = "Courier New"
        edit.getModel().FontDescriptor = fd

        cbc = dlg.getControl("DisplayBlockCheck")
        cbc.getModel().State = 1 if initial_display else 0
        if update:
            cbc.getModel().Enabled = False
            best_effort(lambda: cbc.setEnable(False), "Latex dialog: DisplayBlockCheck.setEnable(False) failed")

        outcome: tuple[str, bool] | None = None

        def on_insert() -> None:
            nonlocal outcome
            t = get_control_text(dlg.getControl("LatexEdit")).strip()
            db = get_checkbox_state(dlg.getControl("DisplayBlockCheck")) == 1
            outcome = (t, db)
            dlg.endDialog(1)

        def on_cancel() -> None:
            dlg.endDialog(0)

        btn_insert = dlg.getControl("BtnInsert")
        btn_insert.getModel().Label = _("Update") if update else _("Insert")
        btn_insert.addActionListener(_ClickListener(on_insert))
        btn_cancel = dlg.getControl("BtnCancel")
        btn_cancel.addActionListener(_ClickListener(on_cancel))

        # Set focus to the edit control
        edit.setFocus()

        dlg.execute()

        return outcome
    except Exception:
        log.exception("show_latex_input_dialog failed")
        msgbox(ctx, _("Error"), _("Could not open the LaTeX input dialog."))
        return None
    finally:
        if dlg is not None:
            best_effort(dlg.dispose, "LaTeX dialog: dispose failed")


def _selection_latex_prefill(
    ctx: Any, doc: Any, last_latex: str, last_display: bool
) -> _SelectionLatexPrefill:
    """Inspect selection: abort on multiple Math objects; prefill TeX for one.

    Returns ``(embed_name_or_none, latex, display, abort)``.
    """
    hits = math_mml_export.selected_math_embeds(doc)

    if len(hits) > 1:
        msgbox(
            ctx,
            _("Edit LaTeX Math"),
            _("Select a single formula to update. Multiple formulas are selected."),
        )
        return _SelectionLatexPrefill(None, last_latex, last_display, True)

    if len(hits) != 1:
        return _SelectionLatexPrefill(None, last_latex, last_display, False)

    embed = hits[0]
    name = math_mml_export.resolve_math_embed_name(doc, embed)
    if not name:
        msgbox(
            ctx,
            _("Edit LaTeX Math"),
            _("Could not identify the selected formula. Click in the text, then insert a new formula."),
        )
        return _SelectionLatexPrefill(None, last_latex, last_display, True)
    try:
        # What was wrong: Hand-rolled getEmbeddedObject() unwrap and Formula extraction duplicated math_mml_export.read_starmath.
        # How it happened: Early prototype wrote embedded object introspection inline before read_starmath was factored out.
        # Why this change: Call math_mml_export.read_starmath to reuse the shared unwrapping and fallback logic.
        starmath = math_mml_export.read_starmath(embed)
        if starmath:
            exported = math_mml_export.convert_starmath_to_latex(ctx, starmath)
            if exported.ok and exported.latex:
                return _SelectionLatexPrefill(name, exported.latex, last_display, False)
    except Exception:
        log.debug("latex dialog: selection Math → LaTeX prefill failed", exc_info=True)
    msgbox(
        ctx,
        _("Edit LaTeX Math"),
        _("Could not convert formula to LaTeX. Enter formula manually."),
    )
    return _SelectionLatexPrefill(name, "", last_display, False)


def _apply_latex_to_writer(
    ctx: Any, doc: Any, latex: str, display_block: bool, target_name: str | None
) -> tuple[bool, str]:
    """Convert LaTeX and either replace the named embed or insert a new one."""
    conv_res = convert_latex_to_starmath(ctx, latex, display_block=display_block)
    if not conv_res.ok:
        error_msg = conv_res.error_message or _("Unknown conversion error")
        return False, _("Failed to convert LaTeX to StarMath:\n\n{0}").format(error_msg)

    starmath = conv_res.starmath or ""

    if target_name:
        embed = math_mml_export.lookup_math_embed(doc, target_name)
        if embed is None:
            return False, _("The selected formula is no longer in the document.")
        try:
            replace_writer_math_formula(embed, starmath)
        except Exception as exc:
            log.exception("replace_writer_math_formula failed")
            # What was wrong: str(exc) on PyUNO exceptions is often empty (""), producing an empty error dialog.
            # How it happened: PyUNO exceptions store details in .Message rather than __str__.
            # Why this change: Use resolve_exception_message to extract .Message, fallback to str, or exception type name.
            return False, resolve_exception_message(exc)
        set_config("last_latex_input", latex)
        return True, _("Formula updated.")

    controller = doc.getCurrentController()
    view_cursor = controller.getViewCursor()
    insert_writer_math_formula(doc, view_cursor, starmath, display_block=display_block)
    set_config("last_latex_input", latex)
    set_config("last_latex_display_block", display_block)
    return True, _("Formula inserted.")


def insert_latex_math_dialog(ctx: Any) -> None:
    """Entry point for inserting LaTeX Math into Writer via a dialog."""
    try:
        doc = get_active_document(ctx)
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
                "show_plain_text": not bool(target_name),
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
        msgbox_with_report(
            ctx,
            _("Error"),
            _("An unexpected error occurred during LaTeX insertion."),
            box_type=3,
            reportable=True,
            report_title="LaTeX insertion failed",
        )
