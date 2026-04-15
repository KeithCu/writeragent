# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# -----------------------------------------------------------------------------
# Reference (behavior / schema only; implementation is LibreOffice UNO DataPilot):
#   OnlyOffice Document Builder-style pivot flow is described in
#   onlyofficeai/scripts/helpers/helpers.js (insertPivotTable / ~6621–7040 in many
#   forks). That code uses Api.InsertPivotNewWorksheet — not portable here.
#   Crosswalk: onlyoffice_calc_impressplan.md (Part A.2, Part B §1).
#
# Future work (not implemented here; duplicated in AGENTS.md §5):
#   - Pivot charts (chart bound to an existing DataPilot table).
#   - Natural-language-only pivot setup without explicit header names (would need
#     an LLM step to map text to field names, similar to OO’s parsed.rowIndices).
#   - Sheet standard filter: see docs/calc-sheet-filter.md and sheet_filter.py.
# -----------------------------------------------------------------------------
"""Calc DataPilot (pivot table) tools — specialized tier."""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.errors import ToolExecutionError, UnoObjectError
from plugin.modules.calc.address_utils import parse_address, parse_range_string
from plugin.modules.calc.base import ToolCalcPivotBase
from plugin.modules.calc.bridge import CalcBridge

logger = logging.getLogger("writeragent.calc")


def _query_interface(obj: Any, typename: str) -> Any:
    """PyUNO requires ``uno.getTypeByName`` for ``queryInterface``; imported IDL classes fail."""
    import uno

    return obj.queryInterface(uno.getTypeByName(typename))


def _sheet_index_by_name(doc, name: str) -> int:
    sheets = doc.getSheets()
    for i in range(sheets.getCount()):
        if sheets.getByIndex(i).getName() == name:
            return i
    raise UnoObjectError(f"No sheet named '{name}'.")


def _get_dp_tables(sheet) -> Any:
    sup = _query_interface(sheet, "com.sun.star.sheet.XDataPilotTablesSupplier")
    if sup is None:
        raise ToolExecutionError("Sheet does not support DataPilot tables.")
    return sup.getDataPilotTables()


def _field_name_map(desc) -> dict[str, Any]:
    out: dict[str, Any] = {}
    fields = desc.getDataPilotFields()
    for i in range(fields.getCount()):
        f = fields.getByIndex(i)
        named = _query_interface(f, "com.sun.star.container.XNamed")
        if named is None:
            continue
        out[named.getName()] = f
    return out


def _set_field_orientations(
    desc,
    row_fields: list[str],
    column_fields: list[str],
    data_fields: list[str],
    page_fields: list[str],
) -> None:
    from com.sun.star.sheet.DataPilotFieldOrientation import (
        COLUMN,
        DATA,
        HIDDEN,
        PAGE,
        ROW,
    )

    names = row_fields + column_fields + data_fields + page_fields
    if len(names) != len(set(names)):
        raise ToolExecutionError("Duplicate field name in row/column/data/page lists.")

    layout: dict[str, Any] = {}
    for n in row_fields:
        layout[n] = ROW
    for n in column_fields:
        layout[n] = COLUMN
    for n in data_fields:
        layout[n] = DATA
    for n in page_fields:
        layout[n] = PAGE

    by_name = _field_name_map(desc)
    missing = [n for n in layout if n not in by_name]
    if missing:
        avail = sorted(by_name.keys())
        raise ToolExecutionError(
            "Unknown pivot field name(s): "
            + ", ".join(missing)
            + ". Available column headers: "
            + ", ".join(avail)
        )

    for nm, fld in by_name.items():
        ps = _query_interface(fld, "com.sun.star.beans.XPropertySet")
        if ps is None:
            continue
        orient = layout.get(nm, HIDDEN)
        ps.setPropertyValue("Orientation", orient)


def _range_address_for_sheet(sheet_idx: int, range_str: str) -> Any:
    from com.sun.star.table import CellRangeAddress

    (sc, sr), (ec, er) = parse_range_string(range_str)
    addr = CellRangeAddress()
    addr.Sheet = sheet_idx
    addr.StartColumn = sc
    addr.StartRow = sr
    addr.EndColumn = ec
    addr.EndRow = er
    return addr


def _cell_address(sheet_idx: int, cell_str: str) -> Any:
    from com.sun.star.table import CellAddress

    col, row = parse_address(cell_str)
    a = CellAddress()
    a.Sheet = sheet_idx
    a.Column = col
    a.Row = row
    return a


class CreatePivotTable(ToolCalcPivotBase):
    """Create a DataPilot (pivot) table from a rectangular source range."""

    name = "create_pivot_table"
    description = (
        "Create a pivot table (DataPilot) from a source data range. "
        "Field names must match the header row column titles in the source range. "
        "Place the result on an existing sheet at destination_cell (e.g. new sheet via create_sheet first)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pivot_table_name": {
                "type": "string",
                "description": "Unique name for this pivot table in the document.",
            },
            "source_range": {
                "type": "string",
                "description": "Data range including headers, e.g. A1:D20.",
            },
            "source_sheet_name": {
                "type": "string",
                "description": "Sheet containing source_range. Omit to use the active sheet.",
            },
            "destination_cell": {
                "type": "string",
                "description": "Top-left cell for the pivot output, e.g. A1 or F3.",
            },
            "destination_sheet_name": {
                "type": "string",
                "description": "Sheet where the pivot is drawn. Omit to use the active sheet.",
            },
            "row_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Source column headers to use as row fields.",
            },
            "column_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Source column headers to use as column fields.",
            },
            "data_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Source column headers to aggregate in the data area (required).",
            },
            "page_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional page/filter fields (headers).",
            },
        },
        "required": ["pivot_table_name", "source_range", "destination_cell", "data_fields"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        doc = bridge.get_active_document()
        sheets_coll = doc.getSheets()
        pivot_name = (kwargs.get("pivot_table_name") or "").strip()
        if not pivot_name:
            return self._tool_error("pivot_table_name is required.")

        data_fields = kwargs.get("data_fields") or []
        if not isinstance(data_fields, list) or len(data_fields) < 1:
            return self._tool_error("data_fields must be a non-empty array of header names.")

        src_sheet_name = kwargs.get("source_sheet_name")
        if src_sheet_name:
            if not sheets_coll.hasByName(src_sheet_name):
                return self._tool_error(f"No sheet named '{src_sheet_name}'.")
            src_sheet = sheets_coll.getByName(src_sheet_name)
        else:
            src_sheet = bridge.get_active_sheet()
        src_idx = _sheet_index_by_name(doc, src_sheet.getName())

        dest_sheet_name = kwargs.get("destination_sheet_name")
        if dest_sheet_name:
            if not sheets_coll.hasByName(dest_sheet_name):
                return self._tool_error(f"No sheet named '{dest_sheet_name}'.")
            dest_sheet = sheets_coll.getByName(dest_sheet_name)
        else:
            dest_sheet = bridge.get_active_sheet()
        dest_idx = _sheet_index_by_name(doc, dest_sheet.getName())

        range_str = kwargs.get("source_range") or ""
        dest_cell = (kwargs.get("destination_cell") or "").strip()
        if not range_str or not dest_cell:
            return self._tool_error("source_range and destination_cell are required.")

        row_fields = list(kwargs.get("row_fields") or [])
        column_fields = list(kwargs.get("column_fields") or [])
        page_fields = list(kwargs.get("page_fields") or [])

        try:
            dp_tables = _get_dp_tables(dest_sheet)
            if dp_tables.hasByName(pivot_name):
                return self._tool_error(f"A pivot table named '{pivot_name}' already exists.")

            desc = dp_tables.createDataPilotDescriptor()
            desc.setSourceRange(_range_address_for_sheet(src_idx, range_str))
            _set_field_orientations(desc, row_fields, column_fields, data_fields, page_fields)

            dp_tables.insertNewByName(
                pivot_name,
                _cell_address(dest_idx, dest_cell),
                desc,
            )

            # Refresh so output is materialized (avoids #VALUE! in some layouts).
            tbl_any = dp_tables.getByName(pivot_name)
            dpt = _query_interface(tbl_any, "com.sun.star.sheet.XDataPilotTable")
            if dpt is not None:
                dpt.refresh()

            return {
                "status": "ok",
                "message": f"Created pivot table '{pivot_name}' at {dest_sheet.getName()}!{dest_cell}.",
                "pivot_table_name": pivot_name,
                "destination_sheet": dest_sheet.getName(),
                "destination_cell": dest_cell,
            }
        except (ToolExecutionError, UnoObjectError):
            raise
        except Exception as e:
            logger.exception("create_pivot_table")
            raise ToolExecutionError(str(e)) from e


class RefreshPivotTable(ToolCalcPivotBase):
    """Refresh a DataPilot table from its current source range."""

    name = "refresh_pivot_table"
    description = (
        "Reload pivot table data from the source range. "
        "If sheet_name is omitted, searches all sheets for pivot_table_name."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pivot_table_name": {"type": "string", "description": "Name of the pivot table."},
            "sheet_name": {
                "type": "string",
                "description": "Sheet containing the pivot. Omit to search the workbook.",
            },
        },
        "required": ["pivot_table_name"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        doc = bridge.get_active_document()
        name = (kwargs.get("pivot_table_name") or "").strip()
        if not name:
            return self._tool_error("pivot_table_name is required.")
        sheet_name = kwargs.get("sheet_name")

        try:
            if sheet_name:
                sh = doc.getSheets()
                if not sh.hasByName(sheet_name):
                    return self._tool_error(f"No sheet named '{sheet_name}'.")
                sheets = [sh.getByName(sheet_name)]
            else:
                sh = doc.getSheets()
                sheets = [sh.getByIndex(i) for i in range(sh.getCount())]

            dpt = None
            for sheet in sheets:
                dp_tables = _get_dp_tables(sheet)
                if dp_tables.hasByName(name):
                    tbl_any = dp_tables.getByName(name)
                    dpt = _query_interface(tbl_any, "com.sun.star.sheet.XDataPilotTable")
                    break
            if dpt is None:
                return self._tool_error(f"Pivot table '{name}' not found.")

            dpt.refresh()
            return {"status": "ok", "message": f"Refreshed pivot table '{name}'."}
        except (ToolExecutionError, UnoObjectError):
            raise
        except Exception as e:
            logger.exception("refresh_pivot_table")
            raise ToolExecutionError(str(e)) from e


class ListPivotTables(ToolCalcPivotBase):
    """List DataPilot tables and their output locations."""

    name = "list_pivot_tables"
    description = "List pivot tables in the spreadsheet, optionally limited to one sheet."
    parameters = {
        "type": "object",
        "properties": {
            "sheet_name": {
                "type": "string",
                "description": "If set, only list pivot tables on this sheet.",
            },
        },
        "required": [],
    }
    is_mutation = False

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        doc = bridge.get_active_document()
        only_sheet = kwargs.get("sheet_name")

        try:
            sh = doc.getSheets()
            out: list[dict[str, Any]] = []

            if only_sheet:
                if not sh.hasByName(only_sheet):
                    return self._tool_error(f"No sheet named '{only_sheet}'.")
                sheet_iter = [sh.getByName(only_sheet)]
            else:
                sheet_iter = [sh.getByIndex(i) for i in range(sh.getCount())]

            for sheet in sheet_iter:
                dp_tables = _get_dp_tables(sheet)
                for pname in dp_tables.getElementNames():
                    entry: dict[str, Any] = {
                        "name": pname,
                        "sheet": sheet.getName(),
                    }
                    try:
                        tbl_any = dp_tables.getByName(pname)
                        dpt = _query_interface(tbl_any, "com.sun.star.sheet.XDataPilotTable")
                        if dpt is not None:
                            ora = dpt.getOutputRange()
                            entry["output_sheet_index"] = ora.Sheet
                            entry["output_range"] = CalcBridge._range_to_str(ora)
                    except Exception as e:
                        logger.debug("list_pivot_tables extra info: %s", e)
                    out.append(entry)

            return {"status": "ok", "pivot_tables": out, "count": len(out)}
        except (ToolExecutionError, UnoObjectError):
            raise
        except Exception as e:
            logger.exception("list_pivot_tables")
            raise ToolExecutionError(str(e)) from e
