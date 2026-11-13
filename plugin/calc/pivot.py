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
"""Calc DataPilot (pivot table) tools — specialized tier.

Field names are the source header row, unlisted fields stay hidden, pivot
names are unique in the document, and refresh materializes the output.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

try:
    from com.sun.star.sheet.GeneralFunction import (
        AVERAGE,
        COUNT,
        COUNTNUMS,
        MAX,
        MIN,
        PRODUCT,
        STDEV,
        STDEVP,
        SUM,
        VAR,
        VARP,
    )
except (ImportError, ModuleNotFoundError):
    AVERAGE = "AVERAGE"  # type: ignore
    COUNT = "COUNT"  # type: ignore
    COUNTNUMS = "COUNTNUMS"  # type: ignore
    MAX = "MAX"  # type: ignore
    MIN = "MIN"  # type: ignore
    PRODUCT = "PRODUCT"  # type: ignore
    STDEV = "STDEV"  # type: ignore
    STDEVP = "STDEVP"  # type: ignore
    SUM = "SUM"  # type: ignore
    VAR = "VAR"  # type: ignore
    VARP = "VARP"  # type: ignore

try:
    from com.sun.star.sheet.DataPilotFieldOrientation import COLUMN, DATA, HIDDEN, PAGE, ROW
except (ImportError, ModuleNotFoundError):
    COLUMN = "COLUMN"  # type: ignore
    DATA = "DATA"  # type: ignore
    HIDDEN = "HIDDEN"  # type: ignore
    PAGE = "PAGE"  # type: ignore
    ROW = "ROW"  # type: ignore

try:
    from com.sun.star.sheet.FilterOperator import EQUAL
except (ImportError, ModuleNotFoundError):
    EQUAL = "EQUAL"  # type: ignore

from plugin.calc.address_utils import format_range_address, split_sheet_prefix
from plugin.calc.base import ToolCalcPivotBase
from plugin.calc.bridge import CalcBridge, filter_agent_sheet_names
from plugin.calc.calc_utils import query_interface as _query_interface
from plugin.framework.errors import ToolExecutionError, WriterAgentException, is_disposal, name_miss_details

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

_DATA_FUNCTIONS: dict[str, Any] = {
    "SUM": SUM,
    "COUNT": COUNT,
    "AVERAGE": AVERAGE,
    "MAX": MAX,
    "MIN": MIN,
    "PRODUCT": PRODUCT,
    "COUNTNUMS": COUNTNUMS,
    "COUNT_NUMS": COUNTNUMS,
    "STDEV": STDEV,
    "STDEVP": STDEVP,
    "VAR": VAR,
    "VARP": VARP,
}


def _get_dp_tables(sheet: Any) -> Any:
    sup = _query_interface(sheet, "com.sun.star.sheet.XDataPilotTablesSupplier")
    if sup is None:
        raise ToolExecutionError("Sheet does not support DataPilot tables.")
    return sup.getDataPilotTables()


def _set_field_orientations(
    desc: Any,
    row_fields: list[str],
    column_fields: list[str],
    data_fields: list[str],
    page_fields: list[str],
    *,
    data_functions: dict[str, str] | None = None,
    page_filters: dict[str, Any] | None = None,
) -> None:
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

    fields = desc.getDataPilotFields()
    by_name: dict[str, tuple[int, Any]] = {}
    for i in range(fields.getCount()):
        field = fields.getByIndex(i)
        by_name[field.getName()] = (i, field)
    missing = [n for n in layout if n not in by_name]
    if missing:
        avail = sorted(by_name.keys())
        raise ToolExecutionError("Unknown pivot field name(s): " + ", ".join(missing) + ". Available column headers: " + ", ".join(avail))

    fn_by_field: dict[str, Any] = {}
    for fld_name, func_name in (data_functions or {}).items():
        if fld_name not in data_fields:
            raise ToolExecutionError(f"Field '{fld_name}' in data_functions must be in data_fields.")
        key = str(func_name).strip().upper()
        if key not in _DATA_FUNCTIONS:
            allowed = ", ".join(sorted(_DATA_FUNCTIONS.keys()))
            raise ToolExecutionError(f"Unknown data function '{func_name}'. Allowed functions: {allowed}")
        fn_by_field[fld_name] = _DATA_FUNCTIONS[key]

    for nm, (_idx, fld) in by_name.items():
        orient = layout.get(nm, HIDDEN)
        fld.setPropertyValue("Orientation", orient)
        if orient == DATA:
            fld.setPropertyValue("Function", fn_by_field.get(nm, SUM))

    if page_filters:
        import uno

        filter_fields = []
        for pf_name, pf_val in page_filters.items():
            if pf_name not in page_fields:
                raise ToolExecutionError(f"Field '{pf_name}' in page_filters must be in page_fields.")
            f_idx, _fld = by_name[pf_name]
            tff = uno.createUnoStruct("com.sun.star.sheet.TableFilterField")
            setattr(tff, "Field", f_idx)
            setattr(tff, "Operator", EQUAL)
            is_num = isinstance(pf_val, (int, float)) and not isinstance(pf_val, bool)
            setattr(tff, "IsNumeric", is_num)
            setattr(tff, "NumericValue", float(pf_val) if is_num else 0.0)
            setattr(tff, "StringValue", str(pf_val))
            filter_fields.append(tff)

        filter_desc = desc.getFilterDescriptor()
        if filter_desc is not None:
            filter_desc.setFilterFields(tuple(filter_fields))


def _pivot_sheets(doc: Any, sheet_name: str | None = None) -> list[Any]:
    """Sheets a pivot lookup or list scans: the named sheet, else every agent-visible sheet.

    A named sheet comes from CalcBridge.get_sheet, so a missing name raises its
    UnoObjectError (UNO_OBJECT_ERROR, with the available sheet names), which the
    tools let reach execute_safe. With no name, internal sheets whose name starts
    with "_" are skipped, the same rule sheet_list follows, so a lookup never
    finds or suggests a pivot the agent cannot see in pivot_table_list.
    """
    if sheet_name:
        return [CalcBridge(doc).get_sheet(sheet_name)]
    sheets = doc.getSheets()
    return [sheets.getByName(n) for n in filter_agent_sheet_names(sheets.getElementNames())]


def _find_pivot(doc: Any, name: str, sheet_name: str | None = None) -> tuple[Any, Any]:
    """Return ``(table, sheet)`` for the pivot called *name*.

    Raises:
        ToolExecutionError: code PIVOT_TABLE_NOT_FOUND, with ``hint`` and ``similar``
            drawn from the pivots on agent-visible sheets.
        UnoObjectError: *sheet_name* is not a sheet (CalcBridge.get_sheet).
    """
    for sheet in _pivot_sheets(doc, sheet_name):
        dp_tables = _get_dp_tables(sheet)
        if dp_tables.hasByName(name):
            return dp_tables.getByName(name), sheet
    where = f" on sheet '{sheet_name}'" if sheet_name else ""
    known = [str(n) for visible in _pivot_sheets(doc) for n in _get_dp_tables(visible).getElementNames()]
    raise ToolExecutionError(
        f"Pivot table '{name}' not found{where}.",
        code="PIVOT_TABLE_NOT_FOUND",
        details=name_miss_details(name, known, "pivot_table_list"),
    )


def _resolve_qualified_ref(
    bridge: CalcBridge, ref: str, sheet_arg: Any, ref_label: str, sheet_label: str,
) -> Any:
    """Resolve *ref*, qualified by *sheet_arg* when *ref* has no sheet prefix.

    The bridge's own errors (a missing sheet is UNO_OBJECT_ERROR with the sheet
    names) and a disposed document reach execute_safe unchanged. Any other failure
    (an unknown name raises a RuntimeException with an empty message, a malformed
    range a ValueError) is reported with the argument it came from.
    """
    prefix = split_sheet_prefix(ref)[0]
    if prefix is not None and sheet_arg and prefix != sheet_arg:
        raise ToolExecutionError(
            f"{ref_label} names sheet '{prefix}' but {sheet_label} says '{sheet_arg}' — pass one or the other."
        )
    resolved = ref if prefix is not None or not sheet_arg else f"'{sheet_arg}'.{ref}"
    try:
        return bridge.resolve_range_or_address(resolved)
    except Exception as exc:
        if isinstance(exc, WriterAgentException) or is_disposal(exc):
            raise
        raise ToolExecutionError(f"Cannot resolve {ref_label} '{ref}': {exc}") from exc


class CreatePivotTable(ToolCalcPivotBase):
    """Create a DataPilot (pivot) table from a rectangular source range."""

    name: str | None = "pivot_table_create"
    description: str = "Create a pivot table (DataPilot) from a source data range. Field names must match the header row column titles in the source range. Place the result on an existing sheet at destination_cell (e.g. new sheet via sheet_create first)."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Unique name for this pivot table in the document."},
            "source_range": {"type": "string", "description": "Data range including headers, e.g. A1:D20."},
            "source_sheet": {"type": "string", "description": "Sheet containing source_range. Omit to use the active sheet."},
            "destination_cell": {"type": "string", "description": "Top-left cell for the pivot output, e.g. A1 or F3."},
            "destination_sheet": {"type": "string", "description": "Sheet where the pivot is drawn. Omit to use the active sheet."},
            "row_fields": {"type": "array", "items": {"type": "string"}, "description": "Source column headers to use as row fields."},
            "column_fields": {"type": "array", "items": {"type": "string"}, "description": "Source column headers to use as column fields."},
            "data_fields": {"type": "array", "items": {"type": "string"}, "description": "Source column headers to aggregate in the data area (required)."},
            "page_fields": {"type": "array", "items": {"type": "string"}, "description": "Optional page/filter fields (headers)."},
            "data_functions": {"type": "object", "description": "Optional map of data field name to aggregate function: SUM, COUNT, AVERAGE, MAX, MIN, PRODUCT, COUNTNUMS, STDEV, STDEVP, VAR, VARP."},
            "page_filters": {"type": "object", "description": "Optional map of page field name to filter value."},
        },
        "required": ["name", "source_range", "destination_cell", "data_fields"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        bridge = CalcBridge(doc)

        pivot_name = kwargs["name"].strip()
        if not pivot_name:
            return self._tool_error("name is required.")

        data_fields = kwargs["data_fields"]
        if not data_fields:
            return self._tool_error("data_fields must be a non-empty array of header names.")

        range_str = kwargs.get("source_range", "").strip()
        dest_cell = kwargs.get("destination_cell", "").strip()
        if not range_str or not dest_cell:
            return self._tool_error("source_range and destination_cell are required.")

        src_sheet_name = kwargs.get("source_sheet")
        dest_sheet_name = kwargs.get("destination_sheet")

        # Resolve source range
        src_obj = _resolve_qualified_ref(bridge, range_str, src_sheet_name, "source_range", "source_sheet")

        src_addr = src_obj.getRangeAddress()
        num_rows = src_addr.EndRow - src_addr.StartRow + 1
        num_cols = src_addr.EndColumn - src_addr.StartColumn + 1
        if num_rows < 2:
            return self._tool_error("Source range must have at least 2 rows (header row and data rows).")

        # Header validation: every header cell non-empty
        for c in range(num_cols):
            if not src_obj.getCellByPosition(c, 0).getString().strip():
                return self._tool_error(f"Header cell in column {c + 1} of source range is empty.")

        # Resolve destination cell
        dest_obj = _resolve_qualified_ref(bridge, dest_cell, dest_sheet_name, "destination_cell", "destination_sheet")

        from com.sun.star.table import CellAddress
        # A cell and a range both expose getRangeAddress (ScCellObj derives from
        # ScCellRangeObj, sc/inc/cellsuno.hxx); a range destination anchors the pivot
        # at its top-left cell.
        ra = dest_obj.getRangeAddress()
        dest_addr = CellAddress(Sheet=ra.Sheet, Column=ra.StartColumn, Row=ra.StartRow)

        dest_sheet = doc.getSheets().getByIndex(dest_addr.Sheet)

        row_fields = list(kwargs.get("row_fields") or [])
        column_fields = list(kwargs.get("column_fields") or [])
        page_fields = list(kwargs.get("page_fields") or [])
        data_functions = kwargs.get("data_functions")
        page_filters = kwargs.get("page_filters")

        # Pivot names are unique across the workbook: LibreOffice renames a duplicate
        # made on another sheet instead of refusing it (ScDBDocFunc::CreatePivotTable,
        # sc/source/ui/docshell/dbdocfun.cxx, the #i94570 check). Scan every sheet,
        # internal ones included, before insert so no stray table is left behind
        # under a generated name.
        sheets = doc.getSheets()
        for sname in sheets.getElementNames():
            if _get_dp_tables(sheets.getByName(sname)).hasByName(pivot_name):
                return self._tool_error(f"Pivot table '{pivot_name}' already exists on sheet '{sname}'.")

        dp_tables = _get_dp_tables(dest_sheet)
        desc = dp_tables.createDataPilotDescriptor()
        desc.setSourceRange(src_addr)
        try:
            _set_field_orientations(
                desc,
                row_fields,
                column_fields,
                data_fields,
                page_fields,
                data_functions=data_functions,
                page_filters=page_filters,
            )
        except ToolExecutionError as exc:
            return self._tool_error(str(exc))

        before_names = set(dp_tables.getElementNames())
        dp_tables.insertNewByName(pivot_name, dest_addr, desc)
        after_names = set(dp_tables.getElementNames())
        new_names = after_names - before_names

        # Safety net if insert still does not yield the requested name.
        if not dp_tables.hasByName(pivot_name):
            for stray_name in new_names:
                try:
                    dp_tables.removeByName(stray_name)
                except Exception as rm_exc:
                    log.debug("Failed to remove stray pivot table %s: %s", stray_name, rm_exc)
            return self._tool_error(f"Pivot table '{pivot_name}' was not created; that name may already exist in the document.")

        # Refresh so output is materialized (avoids #VALUE! in some layouts).
        tbl_any = dp_tables.getByName(pivot_name)
        dpt = _query_interface(tbl_any, "com.sun.star.sheet.XDataPilotTable")
        if dpt is not None:
            try:
                dpt.refresh()
            except Exception as exc:
                if is_disposal(exc):
                    raise
                try:
                    dp_tables.removeByName(pivot_name)
                except Exception as rm_exc:
                    log.debug("Failed to remove pivot table %s after refresh failure: %s", pivot_name, rm_exc)
                return self._tool_error(f"Failed to refresh new pivot table '{pivot_name}': {exc}")

        return {
            "status": "ok",
            "message": f"Created pivot table '{pivot_name}' at {dest_sheet.getName()}!{dest_cell}.",
            "name": pivot_name,
            "destination_sheet": dest_sheet.getName(),
            "destination_cell": dest_cell,
        }


class RefreshPivotTable(ToolCalcPivotBase):
    """Refresh a DataPilot table from its current source range."""

    name: str | None = "pivot_table_refresh"
    description: str = "Reload pivot table data from the source range. If sheet is omitted, searches all sheets for name."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"name": {"type": "string", "description": "Name of the pivot table."}, "sheet": {"type": "string", "description": "Sheet containing the pivot. Omit to search the workbook."}}, "required": ["name"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        name = kwargs["name"].strip()
        if not name:
            return self._tool_error("name is required.")

        tbl_any, _sheet = _find_pivot(doc, name, sheet_name=kwargs.get("sheet"))

        dpt = _query_interface(tbl_any, "com.sun.star.sheet.XDataPilotTable")
        if dpt is None:
            return self._tool_error(f"Cannot access DataPilot interface for '{name}'.")

        dpt.refresh()
        return {"status": "ok", "message": f"Refreshed pivot table '{name}'."}


class DeletePivotTable(ToolCalcPivotBase):
    """Delete a DataPilot (pivot) table."""

    name: str | None = "pivot_table_delete"
    description: str = "Delete a pivot table by name. If sheet is omitted, searches all sheets for name."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the pivot table to delete."},
            "sheet": {"type": "string", "description": "Sheet containing the pivot table. Omit to search all sheets."},
        },
        "required": ["name"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        name = kwargs["name"].strip()
        if not name:
            return self._tool_error("name is required.")

        _tbl, sheet = _find_pivot(doc, name, sheet_name=kwargs.get("sheet"))
        dp_tables = _get_dp_tables(sheet)
        dp_tables.removeByName(name)
        return {"status": "ok", "message": f"Deleted pivot table '{name}' from sheet '{sheet.getName()}'."}


class ListPivotTables(ToolCalcPivotBase):
    """List DataPilot tables and their output locations."""

    name: str | None = "pivot_table_list"
    description: str = "List pivot tables in the spreadsheet, optionally limited to one sheet."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"sheet": {"type": "string", "description": "If set, only list pivot tables on this sheet."}}, "required": []}
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        out: list[dict[str, Any]] = []

        for sheet in _pivot_sheets(doc, kwargs.get("sheet")):
            dp_tables = _get_dp_tables(sheet)
            for pname in dp_tables.getElementNames():
                entry: dict[str, Any] = {"name": pname, "sheet": sheet.getName()}
                tbl_any = dp_tables.getByName(pname)
                dpt = _query_interface(tbl_any, "com.sun.star.sheet.XDataPilotTable")
                if dpt is not None:
                    ora = dpt.getOutputRange()
                    entry["output_sheet_index"] = ora.Sheet
                    entry["output_range"] = format_range_address(ora)
                out.append(entry)

        return {"status": "ok", "pivot_tables": out, "count": len(out)}
