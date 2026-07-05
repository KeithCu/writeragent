# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Specialized tools for reading and editing text tables in Writer (XTextTable).

Structural table editing that the surface lacked entirely: petitions carry fee/valuation tables and
the only prior path was rewriting the whole document. Cell text edits are PLAIN (not tracked
changes) in this first version — table-cell redlines are a separate, harder problem. Tables are
addressed by name (list_tables shows them); cells by A1-style name (getCellByName)."""
from typing import Any

from ..specialized_base import ToolWriterTableBase


def _tables(doc: Any) -> Any:
    """The document's text-table collection (XNameAccess)."""
    if not hasattr(doc, "getTextTables"):
        raise ValueError("This document has no text tables.")
    return doc.getTextTables()


def _get_table(doc: Any, name: str) -> Any:
    """Table by name, or a ValueError listing the available names."""
    tables = _tables(doc)
    names = list(tables.getElementNames())
    if not name or not tables.hasByName(name):
        listing = ", ".join(names) if names else "none"
        raise ValueError("No table named '%s'. Open tables (call list_tables): %s." % (name, listing))
    return tables.getByName(name)


def _dims(table: Any) -> tuple[int, int]:
    return int(table.getRows().getCount()), int(table.getColumns().getCount())


def _col_letters(col_idx: int) -> str:
    """0-based column index -> spreadsheet letters (0->A, 25->Z, 26->AA).

    NOTE: Writer's OWN naming diverges past column Z (it continues with lowercase a..z, not AA),
    so this is only used for the fallback read path and the <=26-column range hint. Reads prefer
    getCellByPosition, and set_table_cell validates against the table's REAL getCellNames()."""
    s = ""
    n = col_idx
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def _cell_name(col_idx: int, row_idx: int) -> str:
    """0-based (col, row) -> A1-style name (col 0/row 0 -> 'A1'). See _col_letters caveat."""
    return "%s%d" % (_col_letters(col_idx), row_idx + 1)


def _resolve_cell_name(table: Any, raw: str) -> str | None:
    """Match a user-supplied cell address against the table's REAL cell names.

    Exact match first, then the uppercased form — NEVER a blind upper rewrite: on a >26-column
    table 'a1' (Writer's real name for column 27) and 'A1' are DIFFERENT cells, and upping the
    input would silently write the wrong one."""
    names = set(table.getCellNames())
    if raw in names:
        return raw
    up = raw.upper()
    if up in names:
        return up
    return None


class ListTables(ToolWriterTableBase):
    name = "list_tables"
    description = "List the text tables in the document with their name and dimensions (rows x columns). Use the name to read or edit a table."
    is_mutation = False
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            tables = _tables(ctx.doc)
            out = []
            for name in tables.getElementNames():
                rows, cols = _dims(tables.getByName(name))
                out.append({"name": name, "rows": rows, "cols": cols})
            return {"status": "ok", "count": len(out), "tables": out}
        except Exception as e:
            return self._tool_error("Could not list tables: %s" % e)


class GetTableCells(ToolWriterTableBase):
    name = "get_table_cells"
    description = "Return a table's cell text as a row-major matrix (matrix[r][c]). Cell addresses are A1-style (column letter + 1-based row)."
    is_mutation = False
    parameters = {
        "type": "object",
        "properties": {"table_name": {"type": "string", "description": "Table name from list_tables."}},
        "required": ["table_name"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = (kwargs.get("table_name") or "").strip()
        try:
            table = _get_table(ctx.doc, name)
            rows, cols = _dims(table)
            matrix = []
            for r in range(rows):
                row = []
                for c in range(cols):
                    # Prefer position-based access (naming-scheme-proof: Writer names columns
                    # A..Z then lowercase a..z); fall back to the computed A1 name, then blank
                    # (merged/covered cells have no addressable cell).
                    val = ""
                    try:
                        val = table.getCellByPosition(c, r).getString()
                    except Exception:
                        try:
                            val = table.getCellByName(_cell_name(c, r)).getString()
                        except Exception:
                            val = ""
                    row.append(val)
                matrix.append(row)
            return {"status": "ok", "table_name": name, "rows": rows, "cols": cols, "matrix": matrix}
        except ValueError as ve:
            return self._tool_error(str(ve))
        except Exception as e:
            return self._tool_error("Could not read table '%s': %s" % (name, e))


class SetTableCell(ToolWriterTableBase):
    name = "set_table_cell"
    description = (
        "Set the plain-text content of ONE table cell, addressed A1-style (e.g. 'B2'). Replaces the "
        "cell's text. Not a tracked change even when review mode is on."
    )
    is_mutation = True
    parameters = {
        "type": "object",
        "properties": {
            "table_name": {"type": "string", "description": "Table name from list_tables."},
            "cell": {"type": "string", "description": "A1-style cell address, e.g. 'B2'."},
            "text": {"type": "string", "description": "New plain text for the cell."},
        },
        "required": ["table_name", "cell", "text"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = (kwargs.get("table_name") or "").strip()
        cell_raw = (kwargs.get("cell") or "").strip()
        text = kwargs.get("text")
        if text is None:
            return self._tool_error("text is required.")
        try:
            table = _get_table(ctx.doc, name)
            cell_name = _resolve_cell_name(table, cell_raw)
            if cell_name is None:
                names = list(table.getCellNames())
                sample = ", ".join(names[:8]) + ((", …, %s" % names[-1]) if len(names) > 8 else "")
                return self._tool_error(
                    "Cell '%s' not in table '%s'. Its cells are: %s." % (cell_raw, name, sample))
            cell = table.getCellByName(cell_name)
            old = cell.getString()
            cell.setString(str(text))
            return {"status": "ok", "table_name": name, "cell": cell_name, "old_text": old, "new_text": str(text)}
        except ValueError as ve:
            return self._tool_error(str(ve))
        except Exception as e:
            return self._tool_error("Could not set cell '%s' in table '%s': %s" % (cell_name, name, e))


def _row_col_edit(self, ctx, kwargs, *, axis, insert):
    """Shared body for the four row/column insert/delete tools."""
    name = (kwargs.get("table_name") or "").strip()
    idx_key = "row_index" if axis == "rows" else "col_index"
    raw = kwargs.get(idx_key)
    try:
        idx = int(raw)
    except (TypeError, ValueError):
        return self._tool_error("%s must be an integer." % idx_key)
    if idx < 0:
        return self._tool_error("%s must be non-negative." % idx_key)
    try:
        table = _get_table(ctx.doc, name)
        band = table.getRows() if axis == "rows" else table.getColumns()
        count = band.getCount()
        # insertByIndex(idx, n) inserts BEFORE idx (idx==count appends). removeByIndex needs a real
        # index, and removing the last row/column of a table is not allowed.
        if insert:
            if idx > count:
                return self._tool_error("%s %d out of range (table has %d %s; use 0..%d)." % (idx_key, idx, count, axis, count))
            band.insertByIndex(idx, 1)
        else:
            if idx >= count:
                return self._tool_error("%s %d out of range (table has %d %s; use 0..%d)." % (idx_key, idx, count, axis, count - 1))
            if count <= 1:
                return self._tool_error("Cannot remove the last %s of a table." % axis[:-1])
            band.removeByIndex(idx, 1)
        rows, cols = _dims(table)
        return {"status": "ok", "table_name": name, "rows": rows, "cols": cols}
    except ValueError as ve:
        return self._tool_error(str(ve))
    except Exception as e:
        return self._tool_error("Could not edit %s of table '%s': %s" % (axis, name, e))


class InsertTableRow(ToolWriterTableBase):
    name = "insert_table_row"
    description = "Insert one empty row into a table. row_index is where the new row goes (0-based; equal to the current row count appends at the end)."
    is_mutation = True
    parameters = {
        "type": "object",
        "properties": {
            "table_name": {"type": "string", "description": "Table name from list_tables."},
            "row_index": {"type": "integer", "description": "0-based position for the new row (row count = append)."},
        },
        "required": ["table_name", "row_index"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return _row_col_edit(self, ctx, kwargs, axis="rows", insert=True)


class DeleteTableRow(ToolWriterTableBase):
    name = "delete_table_row"
    description = "Delete one row from a table by 0-based row_index. Cannot delete the last remaining row."
    is_mutation = True
    parameters = {
        "type": "object",
        "properties": {
            "table_name": {"type": "string", "description": "Table name from list_tables."},
            "row_index": {"type": "integer", "description": "0-based row to delete."},
        },
        "required": ["table_name", "row_index"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return _row_col_edit(self, ctx, kwargs, axis="rows", insert=False)


class InsertTableColumn(ToolWriterTableBase):
    name = "insert_table_column"
    description = "Insert one empty column into a table. col_index is where the new column goes (0-based; equal to the current column count appends at the end)."
    is_mutation = True
    parameters = {
        "type": "object",
        "properties": {
            "table_name": {"type": "string", "description": "Table name from list_tables."},
            "col_index": {"type": "integer", "description": "0-based position for the new column (column count = append)."},
        },
        "required": ["table_name", "col_index"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return _row_col_edit(self, ctx, kwargs, axis="columns", insert=True)


class DeleteTableColumn(ToolWriterTableBase):
    name = "delete_table_column"
    description = "Delete one column from a table by 0-based col_index. Cannot delete the last remaining column."
    is_mutation = True
    parameters = {
        "type": "object",
        "properties": {
            "table_name": {"type": "string", "description": "Table name from list_tables."},
            "col_index": {"type": "integer", "description": "0-based column to delete."},
        },
        "required": ["table_name", "col_index"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return _row_col_edit(self, ctx, kwargs, axis="columns", insert=False)
