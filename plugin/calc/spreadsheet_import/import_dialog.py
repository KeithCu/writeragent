# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Programmatic XDL-free Calc Spreadsheet to Python Import options dialog."""

from __future__ import annotations

import logging
from typing import Any
import unohelper
from com.sun.star.awt import XActionListener

from plugin.framework.uno_context import get_active_document, get_desktop
from plugin.framework.i18n import _
from plugin.chatbot.dialogs import (
    add_dialog_button,
    add_dialog_label,
    msgbox,
)
from plugin.calc.address_utils import parse_range_string, parse_address
from plugin.calc.spreadsheet_import.ingest import ingest_sheet
from plugin.calc.spreadsheet_import.emit import build_converted_output_model
from plugin.calc.spreadsheet_import.preserve import apply_output_to_sheet
from plugin.calc.spreadsheet_import.verify import verify_converted_cells

log = logging.getLogger("writeragent.calc.import_dialog")


def add_dialog_radio_button(
    dlg_model: Any,
    name: str,
    label: str,
    x: int,
    y: int,
    width: int,
    height: int,
    group_name: str,
    state: int = 0,
) -> Any:
    rb = dlg_model.createInstance("com.sun.star.awt.UnoControlRadioButtonModel")
    rb.Name = name
    rb.PositionX = x
    rb.PositionY = y
    rb.Width = width
    rb.Height = height
    rb.Label = _(label)
    rb.GroupName = group_name
    rb.State = state
    dlg_model.insertByName(name, rb)
    return rb


def add_dialog_checkbox(
    dlg_model: Any,
    name: str,
    label: str,
    x: int,
    y: int,
    width: int,
    height: int,
    state: int = 0,
) -> Any:
    cb = dlg_model.createInstance("com.sun.star.awt.UnoControlCheckBoxModel")
    cb.Name = name
    cb.PositionX = x
    cb.PositionY = y
    cb.Width = width
    cb.Height = height
    cb.Label = _(label)
    cb.State = state
    dlg_model.insertByName(name, cb)
    return cb


def get_radio_state(ctrl: Any) -> bool:
    if not ctrl:
        return False
    try:
        if hasattr(ctrl, "getState"):
            return ctrl.getState() == 1
        return ctrl.getModel().State == 1
    except Exception:
        return False


def get_checkbox_state(ctrl: Any) -> bool:
    if not ctrl:
        return False
    try:
        if hasattr(ctrl, "getState"):
            return ctrl.getState() == 1
        return ctrl.getModel().State == 1
    except Exception:
        return False


def fetch_actual_values(sheet: Any, addresses: list[str]) -> dict[str, Any]:
    actual_values = {}
    for addr in addresses:
        try:
            col, row = parse_address(addr)
            cell = sheet.getCellByPosition(col, row)
            t = cell.getType()

            import sys
            CCT = sys.modules.get("com.sun.star.table", None)
            if CCT is not None and hasattr(CCT, "CellContentType"):
                CCT = CCT.CellContentType
            else:
                from com.sun.star.table import CellContentType as CCT

            if t == CCT.EMPTY:
                actual_values[addr] = None
            elif t == CCT.VALUE:
                actual_values[addr] = cell.getValue()
            elif t == CCT.TEXT:
                actual_values[addr] = cell.getString()
            elif t == CCT.FORMULA:
                err = cell.getError()
                if err != 0:
                    actual_values[addr] = f"Err:{err}"
                else:
                    s = cell.getString()
                    try:
                        actual_values[addr] = float(s)
                    except ValueError:
                        actual_values[addr] = s
            else:
                actual_values[addr] = cell.getValue()
        except Exception:
            actual_values[addr] = None
    return actual_values


def run_sheet_conversion(
    ctx: Any,
    doc: Any,
    source_sheet: Any,
    *,
    scope: str = "sheet",
    output_mode: str = "new_sheet",
    vectorize: bool = True,
    verify: bool = True,
) -> dict[str, Any]:
    """Execute the Conversion Pipeline using ingest, emit, apply, and verify."""
    range_addr = None
    if scope == "selection":
        controller = doc.getCurrentController()
        selection = controller.getSelection()
        if hasattr(selection, "getRangeAddress"):
            range_addr = selection.getRangeAddress()

    # 1. Ingest Sheet
    model = ingest_sheet(source_sheet, range_addr=range_addr)
    from plugin.calc.spreadsheet_import.preserve import enrich_number_formats
    enrich_number_formats(source_sheet, model)

    # 2. Translate and Build Converted Output Model
    output, report = build_converted_output_model(model, vectorize=vectorize)

    # 3. Resolve Target Sheet
    target_sheet = None
    if output_mode == "new_sheet":
        target_name = "PythonImport"
        sheets = doc.getSheets()
        if sheets.hasByName(target_name):
            sheets.remove(sheets.getByName(target_name))
        sheets.insertNewByName(target_name, sheets.getCount())
        target_sheet = sheets.getByName(target_name)
    else:
        target_sheet = source_sheet
        (sc, sr), (ec, er) = parse_range_string(output.used_range)
        cell_range = target_sheet.getCellRangeByPosition(sc, sr, ec, er)
        cell_range.clearContents(23)

    # 4. Apply Output
    apply_output_to_sheet(target_sheet, output)

    # 5. Verify Recalc
    failed_verifications = []
    if verify:
        doc.calculateAll()
        actual_values = fetch_actual_values(target_sheet, report.converted)
        verify_res = verify_converted_cells(model, output, report, actual_values=actual_values)
        failed_verifications = [item.to_dict() for item in verify_res.failed]

    return {
        "report": report.to_dict(),
        "summary": report.summary(),
        "failed_verifications": failed_verifications,
    }


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

        controller = doc.getCurrentController()
        source_sheet = controller.getActiveSheet()
        if source_sheet is None:
            return

        parent_window = doc.getCurrentController().getFrame().getContainerWindow()
        smgr = ctx.getServiceManager()

        # Build Dialog Model
        dlg_model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
        dlg_model.Title = _("Convert Sheet to Python")
        dlg_model.Width = 260
        dlg_model.Height = 180

        # Scope Group
        add_dialog_label(dlg_model, "ScopeLbl", "Scope", 10, 10, 240, 10, multiline=False)
        add_dialog_radio_button(dlg_model, "ScopeActive", "Active Sheet", 20, 25, 100, 12, "ScopeGroup", state=1)
        add_dialog_radio_button(dlg_model, "ScopeSelection", "Selection", 130, 25, 100, 12, "ScopeGroup")

        # Output Mode Group
        add_dialog_label(dlg_model, "ModeLbl", "Output Mode", 10, 45, 240, 10, multiline=False)
        add_dialog_radio_button(dlg_model, "ModeNew", "New Sheet (PythonImport)", 20, 60, 200, 12, "ModeGroup", state=1)
        add_dialog_radio_button(dlg_model, "ModeReplace", "In-place replacement", 20, 75, 200, 12, "ModeGroup")

        # Options Group
        add_dialog_label(dlg_model, "OptLbl", "Options", 10, 95, 240, 10, multiline=False)
        add_dialog_checkbox(dlg_model, "OptFormats", "Preserve number formats", 20, 110, 200, 12, state=1)
        add_dialog_checkbox(dlg_model, "OptVerify", "Verify recalc (oracle diff)", 20, 125, 200, 12, state=1)
        add_dialog_checkbox(dlg_model, "OptVector", "Vectorize columns when safe", 20, 140, 200, 12, state=1)

        # Action Buttons
        add_dialog_button(dlg_model, "BtnOK", "OK", 140, 160, 50, 14)
        add_dialog_button(dlg_model, "BtnCancel", "Cancel", 195, 160, 55, 14)

        dlg = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
        dlg.setModel(dlg_model)
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        dlg.createPeer(toolkit, parent_window)

        _outcome: dict[str, Any] | None = None

        class _OkListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: Any) -> None:
                nonlocal _outcome
                try:
                    scope = "selection" if get_radio_state(dlg.getControl("ScopeSelection")) else "sheet"
                    output_mode = "in_place" if get_radio_state(dlg.getControl("ModeReplace")) else "new_sheet"
                    vectorize = get_checkbox_state(dlg.getControl("OptVector"))
                    verify = get_checkbox_state(dlg.getControl("OptVerify"))

                    res = run_sheet_conversion(
                        ctx,
                        doc,
                        source_sheet,
                        scope=scope,
                        output_mode=output_mode,
                        vectorize=vectorize,
                        verify=verify,
                    )
                    _outcome = res
                    dlg.endDialog(1)
                except Exception as e:
                    log.exception("Sheet conversion failed inside dialog")
                    msgbox(ctx, "Error", f"Conversion failed: {e}")
                    dlg.endDialog(0)

            def disposing(self, Source: Any) -> None:
                pass

        class _CancelListener(unohelper.Base, XActionListener):
            def actionPerformed(self, rEvent: Any) -> None:
                dlg.endDialog(0)

            def disposing(self, Source: Any) -> None:
                pass

        dlg.getControl("BtnOK").addActionListener(_OkListener())
        dlg.getControl("BtnCancel").addActionListener(_CancelListener())

        dlg.execute()
        dlg.dispose()

        if _outcome:
            msgbox(ctx, "Success", f"Conversion completed successfully!\n\n{_outcome['summary']}")

    except Exception as e:
        log.exception("show_import_dialog failed")
        msgbox(ctx, "Error", f"Failed to open import dialog: {e}")
