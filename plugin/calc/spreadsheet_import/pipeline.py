# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the sheet → ``=PY()`` import stages in order (listed in the package docstring)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from plugin.calc.address_utils import parse_address, parse_range_string
from plugin.calc.bridge import used_range_address
from plugin.calc.spreadsheet_import.emit import build_converted_output_model
from plugin.calc.spreadsheet_import.ingest import ingest_sheet
from plugin.calc.spreadsheet_import.preserve import apply_output_to_sheet, enrich_number_formats
from plugin.calc.spreadsheet_import.verify import verify_converted_cells
from plugin.framework.errors import best_effort

# com.sun.star.sheet.CellFlags: VALUE(1) | DATETIME(2) | STRING(4) | FORMULA(16).
_CLEAR_CONTENTS_FLAGS = 1 | 2 | 4 | 16


def fetch_actual_values(sheet: Any, addresses: list[str]) -> dict[str, Any]:
    """Bulk-read recalculated results; values match what ingest saw from getDataArray()."""
    if not addresses:
        return {}
    coords = [parse_address(addr) for addr in addresses]
    min_col = min(col for col, _row in coords)
    max_col = max(col for col, _row in coords)
    min_row = min(row for _col, row in coords)
    max_row = max(row for _col, row in coords)
    # Same representation ingest classified: float / str / "" / None (error).
    grid = sheet.getCellRangeByPosition(min_col, min_row, max_col, max_row).getDataArray()
    return {addr: grid[row - min_row][col - min_col] for addr, (col, row) in zip(addresses, coords)}


def _unique_sheet_name(sheets: Any, base: str) -> str:
    """Return ``base``, or ``base 2``/``base 3``/... when the name is taken."""
    if not sheets.hasByName(base):
        return base
    suffix = 2
    while sheets.hasByName(f"{base} {suffix}"):
        suffix += 1
    return f"{base} {suffix}"


def selection_range_address(selection: Any) -> Any:
    """Return the one selected range address; reject shapes and multi-range selections."""
    if hasattr(selection, "getRangeAddress"):
        return selection.getRangeAddress()
    if hasattr(selection, "getRangeAddresses"):
        addresses = selection.getRangeAddresses()
        if len(addresses) == 1:
            return addresses[0]
        raise ValueError("Select one contiguous range of cells to convert.")
    raise ValueError("Select a range of cells to convert, not a shape or object.")


def run_sheet_conversion(
    doc: Any,
    source_sheet: Any,
    *,
    scope: str = "sheet",
    output_mode: str = "new_sheet",
    vectorize: bool = True,
    verify: bool = True,
    preserve_formats: bool = True,
) -> dict[str, Any]:
    """Execute the Conversion Pipeline using ingest, emit, apply, and verify."""
    range_addr = None
    if scope == "selection":
        range_addr = selection_range_address(doc.getCurrentController().getSelection())

    # 1. Ingest Sheet
    model = ingest_sheet(source_sheet, range_addr=range_addr)
    if preserve_formats:
        enrich_number_formats(source_sheet, model)

    sheet_bounds: dict[str, tuple[int, int]] = {}

    def _collect_bounds() -> None:
        sheets = doc.getSheets()
        for i in range(sheets.getCount()):
            sh = sheets.getByIndex(i)
            addr = used_range_address(sh)
            sheet_bounds[sh.getName().upper()] = (addr.EndColumn, addr.EndRow)

    best_effort(_collect_bounds, "collect workbook sheet bounds")

    # 2. Translate and Build Converted Output Model
    output, report = build_converted_output_model(model, vectorize=vectorize, sheet_bounds=sheet_bounds)

    # 3. Resolve Target Sheet
    if output_mode == "new_sheet":
        sheets = doc.getSheets()
        target_name = _unique_sheet_name(sheets, "PythonImport")
        sheets.insertNewByName(target_name, sheets.getCount())
        target_sheet = sheets.getByName(target_name)
    else:
        target_sheet = source_sheet
        (sc, sr), (ec, er) = parse_range_string(output.used_range)
        cell_range = target_sheet.getCellRangeByPosition(sc, sr, ec, er)
        cell_range.clearContents(_CLEAR_CONTENTS_FLAGS)

    # 4. Apply Output
    apply_output_to_sheet(target_sheet, output)

    # 5. Verify Recalc
    failed_verifications = []
    if verify:
        doc.calculateAll()
        actual_values = fetch_actual_values(target_sheet, report.converted)
        failed_verifications = [asdict(item) for item in verify_converted_cells(model, output, report, actual_values=actual_values)]

    return {
        "status": "partial" if failed_verifications else "ok",
        "report": report.to_dict(),
        "summary": report.summary(),
        "failed_verifications": failed_verifications,
    }
