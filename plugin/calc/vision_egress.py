# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Insert trusted vision helper HTML and structured grids into Calc."""

from __future__ import annotations

from typing import Any

from plugin.calc.address_utils import index_to_column
from plugin.calc.bridge import CalcBridge
from plugin.calc.manipulator import CellManipulator
from plugin.calc.rich_html import insert_cell_html_rich
from plugin.framework.errors import DocumentDisposedError, ToolExecutionError, is_disposed_exception
from plugin.framework.i18n import _
from plugin.writer.images.image_tools import _get_selected_graphic_object


def _cell(value: Any) -> Any:
    """Keep OCR cell text unchanged. Numeric coercion happens only at write time."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _append_blank(rows: list[list[Any]]) -> None:
    if rows and rows[-1]:
        rows.append([])


def _table_span_merges(table: dict[str, Any], *, header_grid_row: int, max_grid_row: int) -> list[tuple[int, int, int, int]]:
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
        row = int(span.get("row") or 0)
        col = int(span.get("col") or 0)
        rowspan = max(int(span.get("rowspan") or 1), 1)
        colspan = max(int(span.get("colspan") or 1), 1)
        if rowspan <= 1 and colspan <= 1:
            continue
        r1 = header_grid_row + row
        c1 = col
        r2 = r1 + rowspan - 1
        c2 = c1 + colspan - 1
        if r1 > max_grid_row:
            continue
        if r2 > max_grid_row:
            r2 = max_grid_row
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
            if isinstance(columns, list) and columns:
                rows.append([str(col) for col in columns])
            else:
                header_grid_row = len(rows)
            if isinstance(table_rows, list):
                for row in table_rows:
                    if isinstance(row, list):
                        rows.append([_cell(cell) for cell in row])
                    else:
                        rows.append([_cell(row)])
            merges.extend(_table_span_merges(table, header_grid_row=header_grid_row, max_grid_row=len(rows) - 1))
            if table.get("truncated"):
                total = table.get("total_rows")
                note = f"(showing first rows; {total} total)" if total is not None else "(truncated)"
                rows.append([note])

    if len(rows) == 1:
        rows.append(["(no tabular output)"])
    return rows, merges


def format_vision_structure_for_calc(result: dict[str, Any]) -> list[list[Any]]:
    """Turn extract_structure tables/blocks into a row-major grid for write_formula_range."""
    grid, _merges = _vision_structure_calc_layout(result)
    return grid


def structure_calc_grid_has_content(grid: list[list[Any]]) -> bool:
    """True when the grid has more than the title row and placeholder."""
    if len(grid) <= 1:
        return False
    if len(grid) == 2 and grid[1] == ["(no tabular output)"]:
        return False
    return True


def calc_output_anchor_from_graphic(doc: Any, *, image_name: str | None = None) -> tuple[int, int]:
    """Return (start_col, start_row) one row below the graphic's anchor cell."""
    # Writer already inserts by name; Calc used to ignore image_name and write under the selection.
    # Named OCR then landed under whatever graphic happened to be selected. Resolve the named
    # shape first so the insert uses that graphic's cell anchor, not a second selection lookup.
    name = str(image_name or "").strip()
    if name:
        from plugin.doc.visual_helpers import get_graphic_object_by_name

        obj = get_graphic_object_by_name(doc, name)
        if obj is None:
            raise ToolExecutionError(
                _("Image '{name}' not found. Use image_list or leave image_name empty and select the graphic.").format(name=name),
                code="IMAGE_NOT_FOUND",
                details={"image_name": name},
            )
    else:
        obj, _doc_type = _get_selected_graphic_object(doc)
        if obj is None:
            raise ToolExecutionError(_("Select an embedded image, then Run again."), code="NO_IMAGE_SELECTED")

    anchor = None
    try:
        if hasattr(obj, "getPropertyValue"):
            anchor = obj.getPropertyValue("Anchor")
    except Exception as exc:
        if is_disposed_exception(exc):
            raise DocumentDisposedError("Document disposed", object_type="calc") from exc
        anchor = None

    if anchor is None:
        raise ToolExecutionError(_("Anchor the image to a cell, select it, then Run again."), code="NO_OUTPUT_ANCHOR")

    try:
        addr = anchor.getCellAddress()
        col = int(addr.Column)
        row = int(addr.Row)
    except Exception as exc:
        if is_disposed_exception(exc):
            raise DocumentDisposedError("Document disposed", object_type="calc") from exc
        raise ToolExecutionError(_("Anchor the image to a cell, select it, then Run again."), code="NO_OUTPUT_ANCHOR") from None

    return col, row + 1


def insert_vision_html_into_calc(doc: Any, uno_ctx: Any, html: str, *, image_name: str | None = None) -> None:
    """Paste vision HTML into the cell below the graphic anchor."""
    col, row = calc_output_anchor_from_graphic(doc, image_name=image_name)
    # *row* is already one below the graphic anchor (see calc_output_anchor_from_graphic).
    cell_address = f"{index_to_column(col)}{row + 1}"
    insert_cell_html_rich(doc, uno_ctx, cell_address, html)


def insert_vision_structure_into_calc(doc: Any, uno_ctx: Any, result: dict[str, Any], *, image_name: str | None = None) -> int:
    """Write extract_structure blocks/tables as native Calc cells below the graphic anchor."""
    del uno_ctx
    col, row = calc_output_anchor_from_graphic(doc, image_name=image_name)
    grid, merges = _vision_structure_calc_layout(result)
    if not structure_calc_grid_has_content(grid):
        raise ToolExecutionError(_("No structured tables or text blocks to insert."), code="VISION_ERROR", details={"vision_result": result})
    bridge = CalcBridge(doc)
    manipulator = CellManipulator(bridge)
    addr = f"{index_to_column(col)}{row + 1}"
    # literal_text: setString, so "=..." is not a live formula and "000123" is not float()'d.
    manipulator.write_formula_range(addr, grid, literal_text=True)
    for r1, c1, r2, c2 in merges:
        start = f"{index_to_column(col + c1)}{row + r1 + 1}"
        end = f"{index_to_column(col + c2)}{row + r2 + 1}"
        if start != end:
            manipulator.merge_cells(f"{start}:{end}", center=False)
    return len(grid)
