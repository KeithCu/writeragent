# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Insert trusted vision helper HTML and structured grids into Calc."""

from __future__ import annotations

from typing import Any

from plugin.calc.address_utils import format_address, qualify_a1
from plugin.calc.bridge import CalcBridge
from plugin.calc.manipulator import CellManipulator
from plugin.calc.rich_html import insert_cell_html_rich
from plugin.calc.tabular_egress import _append_blank, _append_table_rows, _append_truncation_note, pad_grid
from plugin.doc.visual_helpers import get_graphic_object_by_name, selected_graphic_object
from plugin.framework.errors import ToolExecutionError, reraise_if_disposed, uno_read
from plugin.framework.i18n import _


def _table_span_merges(
    table: dict[str, Any],
    *,
    header_grid_row: int,
    max_grid_row: int,
    max_width: int,
) -> list[tuple[int, int, int, int]]:
    """Return (r1, c1, r2, c2) 0-based grid coords for Docling cell spans.

    Spans are clipped to *max_grid_row* so a rowspan past the kept body
    cannot merge the truncation note that is appended after the table.
    """
    merges: list[tuple[int, int, int, int]] = []
    spans = table.get("spans")
    if not isinstance(spans, list):
        return merges
    for span in spans:
        if not isinstance(span, dict):
            continue
        try:
            row = int(span.get("row") or 0)
            col = int(span.get("col") or 0)
            rowspan = max(int(span.get("rowspan") or 1), 1)
            colspan = max(int(span.get("colspan") or 1), 1)
        except (TypeError, ValueError):
            continue
        if row < 0 or col < 0 or col >= max_width:
            continue
        r1 = header_grid_row + row
        c1 = col
        if r1 > max_grid_row:
            continue
        r2 = min(r1 + rowspan - 1, max_grid_row)
        c2 = min(c1 + colspan - 1, max_width - 1)
        if r1 == r2 and c1 == c2:
            continue
        merges.append((r1, c1, r2, c2))
    return merges


def _vision_structure_calc_layout(result: dict[str, Any]) -> tuple[list[list[Any]], list[tuple[int, int, int, int]]]:
    """Grid plus merge boxes (grid-relative) for extract_structure Calc insert."""
    rows: list[list[Any]] = []
    merges: list[tuple[int, int, int, int]] = []
    helper = str(result.get("helper") or "extract_structure")
    rows.append([helper])

    blocks = result.get("blocks")
    prose_rows = 0
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict) or str(block.get("type") or "").lower() == "table":
                continue
            text = str(block.get("text") or "").strip()
            if text:
                rows.append([text])
                prose_rows += 1
    if prose_rows:
        _append_blank(rows)

    tables = result.get("tables")
    table_count = 0
    if isinstance(tables, list):
        for table in tables:
            if not isinstance(table, dict):
                continue
            columns = table.get("columns")
            table_rows = table.get("rows")
            if not (isinstance(columns, list) and columns) and not (isinstance(table_rows, list) and table_rows):
                continue
            table_count += 1
            _append_blank(rows)
            rows.append([str(table.get("name") or f"table_{table_count}")])
            header_grid_row = len(rows)
            # OCR text stays a string here; numeric coercion happens only at write time.
            _append_table_rows(rows, columns, table_rows, lambda value: "" if value is None else str(value))
            col_width = len(columns) if isinstance(columns, list) else 0
            row_widths = [len(r) if isinstance(r, list) else 1 for r in table_rows] if isinstance(table_rows, list) else []
            width = max([col_width, *row_widths], default=0)
            merges.extend(_table_span_merges(table, header_grid_row=header_grid_row, max_grid_row=len(rows) - 1, max_width=width))
            _append_truncation_note(rows, table)

    return rows, merges


def calc_output_anchor_from_graphic(doc: Any, *, image_name: str | None = None) -> tuple[str, int, int]:
    """Return (sheet_name, start_col, start_row) one row below the graphic's anchor cell, on the graphic's sheet."""
    # Writer already inserts by name; Calc used to ignore image_name and write under the selection.
    # Named OCR then landed under whatever graphic happened to be selected. Resolve the named
    # shape first so the insert uses that graphic's cell anchor, not a second selection lookup.
    name = str(image_name or "").strip()
    if name:
        obj = get_graphic_object_by_name(doc, name)
        if obj is None:
            raise ToolExecutionError(
                _("Image '{name}' not found. Use image_list or leave image_name empty and select the graphic.").format(name=name),
                code="IMAGE_NOT_FOUND",
                details={"image_name": name},
            )
    else:
        obj = selected_graphic_object(doc)
        if obj is None:
            raise ToolExecutionError(_("Select an embedded image, then Run again."), code="NO_IMAGE_SELECTED")

    anchor = uno_read(
        lambda: obj.getPropertyValue("Anchor"),
        None,
        object_type="calc",
    )

    if anchor is None:
        raise ToolExecutionError(_("Anchor the image to a cell, select it, then Run again."), code="NO_OUTPUT_ANCHOR")

    try:
        addr = anchor.getCellAddress()
        col = int(addr.Column)
        row = int(addr.Row)
        sheet_name = str(doc.getSheets().getByIndex(int(addr.Sheet)).getName())
    except Exception as exc:
        reraise_if_disposed(exc, object_type="calc")
        raise ToolExecutionError(_("Anchor the image to a cell, select it, then Run again."), code="NO_OUTPUT_ANCHOR") from None

    return sheet_name, col, row + 1


def insert_vision_html_into_calc(doc: Any, uno_ctx: Any, html: str, *, image_name: str | None = None) -> None:
    """Paste vision HTML into the cell below the graphic anchor."""
    sheet_name, col, row = calc_output_anchor_from_graphic(doc, image_name=image_name)
    # The graphic's sheet becomes active because paste targets the active sheet selection.
    bridge = CalcBridge(doc)
    sheet = bridge.get_sheet(sheet_name)
    doc.getCurrentController().setActiveSheet(sheet)
    # *row* is already one below the graphic anchor (see calc_output_anchor_from_graphic).
    cell_address = format_address(col, row)
    insert_cell_html_rich(doc, uno_ctx, cell_address, html)


def insert_vision_structure_into_calc(doc: Any, result: dict[str, Any], *, image_name: str | None = None) -> int:
    """Write extract_structure blocks/tables as native Calc cells below the graphic anchor."""
    sheet_name, col, row = calc_output_anchor_from_graphic(doc, image_name=image_name)
    grid, merges = _vision_structure_calc_layout(result)
    if len(grid) <= 1:
        raise ToolExecutionError(_("No structured tables or text blocks to insert."), code="VISION_ERROR", details={"vision_result": result})
    grid = pad_grid(grid)
    bridge = CalcBridge(doc)
    manipulator = CellManipulator(bridge)
    addr = qualify_a1(sheet_name, format_address(col, row))
    # literal_text: setString, so "=..." is not a live formula and "000123" is not float()'d.
    manipulator.write_formula_range(addr, grid, literal_text=True)
    for r1, c1, r2, c2 in merges:
        start = format_address(col + c1, row + r1)
        end = format_address(col + c2, row + r2)
        manipulator.merge_cells(qualify_a1(sheet_name, f"{start}:{end}"), center=False)
    return len(grid)
