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
from plugin.calc.python_function import to_calc_compatible
from plugin.calc.rich_html import insert_cell_html_rich
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _
from plugin.writer.images.image_tools import _get_selected_graphic_object


def _cell(value: Any) -> Any:
    return to_calc_compatible(value)


def _append_blank(rows: list[list[Any]]) -> None:
    if rows and rows[-1]:
        rows.append([])


def format_vision_structure_for_calc(result: dict[str, Any]) -> list[list[Any]]:
    """Turn extract_structure tables/blocks into a row-major grid for write_formula_range."""
    rows: list[list[Any]] = []
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
            if not (isinstance(columns, list) and columns) and not (
                isinstance(table_rows, list) and table_rows
            ):
                continue
            table_count += 1
            _append_blank(rows)
            rows.append([str(table.get("name") or f"table_{table_count}")])
            if isinstance(columns, list) and columns:
                rows.append([str(col) for col in columns])
            if isinstance(table_rows, list):
                for row in table_rows:
                    if isinstance(row, list):
                        rows.append([_cell(cell) for cell in row])
                    else:
                        rows.append([_cell(row)])
            if table.get("truncated"):
                total = table.get("total_rows")
                note = f"(showing first rows; {total} total)" if total is not None else "(truncated)"
                rows.append([note])

    if len(rows) == 1:
        rows.append(["(no tabular output)"])
    return rows


def structure_calc_grid_has_content(grid: list[list[Any]]) -> bool:
    """True when the grid has more than the title row and placeholder."""
    if len(grid) <= 1:
        return False
    if len(grid) == 2 and grid[1] == ["(no tabular output)"]:
        return False
    return True


def calc_output_anchor_from_graphic(doc: Any) -> tuple[int, int]:
    """Return (start_col, start_row) one row below the selected graphic's anchor cell."""
    obj, _doc_type = _get_selected_graphic_object(doc)
    if obj is None:
        raise ToolExecutionError(
            _("Select an embedded image, then Run again."),
            code="NO_IMAGE_SELECTED",
        )

    anchor = None
    try:
        if hasattr(obj, "getPropertyValue"):
            anchor = obj.getPropertyValue("Anchor")
    except Exception:
        anchor = None

    if anchor is None:
        raise ToolExecutionError(
            _("Anchor the image to a cell, select it, then Run again."),
            code="NO_OUTPUT_ANCHOR",
        )

    try:
        addr = anchor.getCellAddress()
        col = int(addr.Column)
        row = int(addr.Row)
    except Exception:
        raise ToolExecutionError(
            _("Anchor the image to a cell, select it, then Run again."),
            code="NO_OUTPUT_ANCHOR",
        ) from None

    return col, row + 1


def insert_vision_html_into_calc(doc: Any, uno_ctx: Any, html: str) -> None:
    """Paste vision HTML into the cell below the selected graphic anchor."""
    col, row = calc_output_anchor_from_graphic(doc)
    # *row* is already one below the graphic anchor (see calc_output_anchor_from_graphic).
    cell_address = f"{index_to_column(col)}{row + 1}"
    insert_cell_html_rich(doc, uno_ctx, cell_address, html)


def insert_vision_structure_into_calc(doc: Any, uno_ctx: Any, result: dict[str, Any]) -> int:
    """Write extract_structure blocks/tables as native Calc cells below the graphic anchor."""
    del uno_ctx
    col, row = calc_output_anchor_from_graphic(doc)
    grid = format_vision_structure_for_calc(result)
    if not structure_calc_grid_has_content(grid):
        raise ToolExecutionError(
            _("No structured tables or text blocks to insert."),
            code="VISION_ERROR",
            details={"vision_result": result},
        )
    bridge = CalcBridge(doc)
    manipulator = CellManipulator(bridge)
    addr = f"{index_to_column(col)}{row + 1}"
    manipulator.write_formula_range(addr, grid)
    return len(grid)
