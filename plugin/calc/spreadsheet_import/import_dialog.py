# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Options dialog for Calc's "Convert Sheet to Python" (Dialogs/SpreadsheetImportDialog.xdl); runs pipeline.run_sheet_conversion."""

from __future__ import annotations

import logging
from typing import Any

from plugin.calc.bridge import CalcBridge
from plugin.calc.spreadsheet_import.pipeline import run_sheet_conversion
from plugin.chatbot.dialogs import (
    DismissDialogListener,
    get_checkbox_state,
    load_writeragent_dialog,
    msgbox,
    run_dialog_with_listeners,
)
from plugin.framework.uno_context import get_desktop
from plugin.framework.uno_listeners import BaseActionListener

log = logging.getLogger("writeragent.calc.import_dialog")


def result_message(outcome: dict[str, Any]) -> tuple[str, str]:
    """Title and body for the result box; warns when verification found mismatches."""
    failed = outcome.get("failed_verifications") or []
    if failed:
        first = failed[0]
        return (
            "Conversion finished with mismatches",
            f"{len(failed)} cell(s) did not verify.\nFirst: {first.get('address')}: {first.get('message')}\n\n{outcome['summary']}",
        )
    return "Success", f"Conversion completed successfully!\n\n{outcome['summary']}"


def show_import_dialog(ctx: Any) -> None:
    """Show programmatic Calc to Python import dialog options view."""
    if not ctx:
        log.warning("show_import_dialog: no ctx")
        return

    try:
        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent()
        if doc is None or not hasattr(doc, "getSheets"):
            msgbox(ctx, "Error", "Active document is not a spreadsheet.")
            return

        source_sheet = CalcBridge(doc).get_active_sheet()

        dlg = load_writeragent_dialog("SpreadsheetImportDialog", ctx)
        if dlg is None:
            return

        _outcome: dict[str, Any] | None = None

        class _OkListener(BaseActionListener):
            def actionPerformed(self, rEvent: Any) -> None:
                nonlocal _outcome
                try:
                    scope = "selection" if get_checkbox_state(dlg.getControl("ScopeSelection")) == 1 else "sheet"
                    output_mode = "in_place" if get_checkbox_state(dlg.getControl("ModeReplace")) == 1 else "new_sheet"
                    vectorize = get_checkbox_state(dlg.getControl("OptVector")) == 1
                    verify = get_checkbox_state(dlg.getControl("OptVerify")) == 1
                    preserve_formats = get_checkbox_state(dlg.getControl("OptFormats")) == 1

                    _outcome = run_sheet_conversion(
                        doc,
                        source_sheet,
                        scope=scope,
                        output_mode=output_mode,
                        vectorize=vectorize,
                        verify=verify,
                        preserve_formats=preserve_formats,
                    )
                    dlg.endDialog(1)
                except Exception as e:
                    log.exception("Sheet conversion failed inside dialog")
                    msgbox(ctx, "Error", f"Conversion failed: {e}")
                    dlg.endDialog(0)

        run_dialog_with_listeners(dlg, _OkListener(), DismissDialogListener(dlg, 0))

        if _outcome:
            title, text = result_message(_outcome)
            msgbox(ctx, title, text)

    except Exception as e:
        log.exception("show_import_dialog failed")
        msgbox(ctx, "Error", f"Failed to open import dialog: {e}")
