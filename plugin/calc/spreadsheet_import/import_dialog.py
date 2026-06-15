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

from plugin.framework.uno_context import get_desktop
from plugin.framework.i18n import _
from plugin.chatbot.dialogs import load_writeragent_dialog, msgbox
from plugin.calc.address_utils import parse_range_string, parse_address
from plugin.calc.spreadsheet_import.ingest import ingest_sheet
from plugin.calc.spreadsheet_import.emit import build_converted_output_model
from plugin.calc.spreadsheet_import.preserve import apply_output_to_sheet
from plugin.calc.spreadsheet_import.verify import verify_converted_cells

log = logging.getLogger("writeragent.calc.import_dialog")


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
    try:
        from com.sun.star.table.CellContentType import EMPTY, VALUE, TEXT, FORMULA
    except ImportError:
        EMPTY, VALUE, TEXT, FORMULA = 0, 1, 2, 3  # type: ignore

    actual_values: dict[str, Any] = {}
    for addr in addresses:
        try:
            col, row = parse_address(addr)
            cell = sheet.getCellByPosition(col, row)
            t = cell.getType()
            log.debug("fetch_actual_values cell %s: type=%s, formula=%r, value=%r", addr, t, cell.getFormula(), cell.getValue())

            if t == EMPTY:
                actual_values[addr] = None
            elif t == VALUE:
                actual_values[addr] = cell.getValue()
            elif t == TEXT:
                actual_values[addr] = cell.getString()
            elif t == FORMULA:
                err = cell.getError()
                if err != 0:
                    actual_values[addr] = f"Err:{err}"
                else:
                    display = cell.getString()
                    if "Code execution failed" in display or display.strip().startswith("Error:"):
                        actual_values[addr] = display
                    else:
                        try:
                            v = cell.getValue()
                            # Text results (e.g. TEXT/MONTH) keep getValue()==0; use display.
                            if (
                                v == 0.0
                                and display
                                and display.strip() not in ("", "0", "0.0", "0.00")
                                and not any(ch.isdigit() for ch in display)
                            ):
                                actual_values[addr] = display.strip()
                            else:
                                actual_values[addr] = v
                        except Exception:
                            actual_values[addr] = display
            else:
                actual_values[addr] = cell.getValue()
        except Exception as e:
            log.exception("Exception in fetch_actual_values for address %s: %s", addr, e)
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

    sheet_bounds: dict[str, tuple[int, int]] = {}
    try:
        from plugin.calc.spreadsheet_import.ingest import _used_range_address

        sheets = doc.getSheets()
        for i in range(sheets.getCount()):
            sh = sheets.getByIndex(i)
            addr = _used_range_address(sh)
            sheet_bounds[sh.getName().upper()] = (addr.EndColumn, addr.EndRow)
    except Exception:
        log.exception("Failed to collect workbook sheet bounds for range clipping")

    # 2. Translate and Build Converted Output Model
    output, report = build_converted_output_model(
        model,
        vectorize=vectorize,
        sheet_bounds=sheet_bounds or None,
    )

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

        dlg = load_writeragent_dialog("SpreadsheetImportDialog", ctx)
        if dlg is None:
            return

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

        btn_ok = dlg.getControl("BtnOK")
        if btn_ok is not None:
            btn_ok.addActionListener(_OkListener())
        btn_cancel = dlg.getControl("BtnCancel")
        if btn_cancel is not None:
            btn_cancel.addActionListener(_CancelListener())

        dlg.execute()
        dlg.dispose()

        if _outcome:
            msgbox(ctx, "Success", f"Conversion completed successfully!\n\n{_outcome['summary']}")

    except Exception as e:
        log.exception("show_import_dialog failed")
        msgbox(ctx, "Error", f"Failed to open import dialog: {e}")
