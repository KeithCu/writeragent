# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Table tools: Writer XTextTable and Draw/Impress TableShape (same names, different UNO).

Writer: named text tables (table_list / getCellByName). Draw: TableShape on a page
(page + shape index, or shape.Name). Cell text is PLAIN (not tracked changes).
"""
import logging
from typing import Any

from ..specialized_base import ToolWriterTableBase

log = logging.getLogger("writeragent.writer.specialized.tables")


def _is_draw_doc(doc: Any) -> bool:
    """True for Draw/Impress. Mocks without supportsService are treated as Writer."""
    try:
        ss = getattr(doc, "supportsService", None)
        if not callable(ss):
            return False
        return bool(
            ss("com.sun.star.drawing.DrawingDocument") or ss("com.sun.star.presentation.PresentationDocument")
        )
    except Exception:
        return False


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
        raise ValueError("No table named '%s'. Open tables (call table_list): %s." % (name, listing))
    return tables.getByName(name)


def _dims(table: Any) -> tuple[int, int]:
    return int(table.getRows().getCount()), int(table.getColumns().getCount())


def _col_letters(col_idx: int) -> str:
    """0-based column index -> spreadsheet letters (0->A, 25->Z, 26->AA).

    NOTE: Writer's OWN naming diverges past column Z (it continues with lowercase a..z, not AA),
    so this is only used for the fallback read path and the <=26-column range hint. Reads prefer
    getCellByPosition, and table_set_cell validates against the table's REAL getCellNames()."""
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


def _find_parent_table(doc: Any, child_name: str) -> dict[str, Any]:
    """Return the direct Writer table/cell that contains *child_name*, if any.

    Writer exposes a nested TextTable as an element in the owning cell's
    XEnumeration. Inspect only that structural level; this does not depend on
    rendered geometry or on TextTable anchors.
    """
    tables = _tables(doc)

    for parent_name in tables.getElementNames():
        if parent_name == child_name:
            continue

        try:
            parent = tables.getByName(parent_name)
            cell_names = parent.getCellNames()
        except Exception:
            continue

        for cell_name in cell_names:
            try:
                cell = parent.getCellByName(cell_name)
                enum = cell.createEnumeration()
            except Exception:
                continue

            while enum.hasMoreElements():
                try:
                    element = enum.nextElement()
                except Exception:
                    break

                try:
                    services = element.getSupportedServiceNames()
                except Exception:
                    services = ()

                if "com.sun.star.text.TextTable" not in services:
                    continue

                try:
                    nested_name = element.getName()
                except Exception:
                    nested_name = ""

                if nested_name == child_name:
                    return {
                        "is_nested": True,
                        "parent_table": parent_name,
                        "parent_cell": cell_name,
                    }

    return {
        "is_nested": False,
        "parent_table": None,
        "parent_cell": None,
    }


class TableList(ToolWriterTableBase):
    name = "table_list"
    description = (
        "List tables with name and dimensions (rows x columns). Writer: text-table names. "
        "Draw/Impress: also page and shape index."
    )
    is_mutation = False
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            if _is_draw_doc(ctx.doc):
                from plugin.draw.tables import list_draw_tables

                out = list_draw_tables(ctx.doc)
                return {"status": "ok", "count": len(out), "tables": out}
            tables = _tables(ctx.doc)
            out = []
            for name in tables.getElementNames():
                rows, cols = _dims(tables.getByName(name))
                out.append({"name": name, "rows": rows, "cols": cols})
            return {"status": "ok", "count": len(out), "tables": out}
        except Exception as e:
            log.exception("Could not list tables")
            return self._tool_error("Could not list tables: %s" % e)


class TableGetCells(ToolWriterTableBase):
    name = "table_get_cells"
    description = (
        "Return a table's cell text as a row-major matrix (matrix[row][col]) by position — not by "
        "cell name. Use matrix indices to read values; for table_set_cell use Writer cell names from "
        "table_set_cell error hints (columns A..Z then a..z past column 26, not spreadsheet AA)."
    )
    is_mutation = False
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Table name from table_list."},
            "page": {"type": "integer", "description": "Draw/Impress: 0-based page index."},
            "index": {"type": "integer", "description": "Draw/Impress: shape index on the page."},
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = str(kwargs.get("name") or "").strip()
        try:
            if _is_draw_doc(ctx.doc):
                from plugin.draw.tables import get_draw_cells, resolve_draw_table

                entry = resolve_draw_table(ctx.doc, name=name, page=kwargs.get("page"), index=kwargs.get("index"))
                matrix = get_draw_cells(entry)
                return {
                    "status": "ok",
                    "table_name": entry.get("name") or name,
                    "page": entry.get("page"),
                    "index": entry.get("index"),
                    "rows": entry.get("rows"),
                    "cols": entry.get("cols"),
                    "matrix": matrix,
                }
            if not name:
                return self._tool_error("name is required.")
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
            nesting = _find_parent_table(ctx.doc, name)
            return {
                "status": "ok",
                "table_name": name,
                "rows": rows,
                "cols": cols,
                "matrix": matrix,
                "nesting": nesting,
            }
        except ValueError as ve:
            return self._tool_error(str(ve))
        except Exception as e:
            log.exception("Could not read table '%s'", name)
            return self._tool_error("Could not read table '%s': %s" % (name, e))


class TableSetCell(ToolWriterTableBase):
    name = "table_set_cell"
    description = (
        "Set the plain-text content of ONE table cell, addressed A1-style (e.g. 'B2'). Replaces the "
        "cell's text and any in-cell formatting (setString). Not a tracked change even when review mode is on."
    )
    is_mutation = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Table name from table_list."},
            "cell": {"type": "string", "description": "A1-style cell address, e.g. 'B2'."},
            "text": {"type": "string", "description": "New plain text for the cell."},
            "page": {"type": "integer", "description": "Draw/Impress: 0-based page index."},
            "index": {"type": "integer", "description": "Draw/Impress: shape index on the page."},
        },
        "required": ["cell", "text"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = str(kwargs.get("name") or "").strip()
        cell_raw = (kwargs.get("cell") or "").strip()
        text = kwargs.get("text")
        if text is None:
            return self._tool_error("text is required.")
        cell_name = cell_raw  # bound for the except below even if _get_table raises before resolution
        try:
            if _is_draw_doc(ctx.doc):
                from plugin.draw.tables import resolve_draw_table, set_draw_cell

                entry = resolve_draw_table(ctx.doc, name=name, page=kwargs.get("page"), index=kwargs.get("index"))
                old, new = set_draw_cell(entry, cell_raw, str(text))
                return {
                    "status": "ok",
                    "table_name": entry.get("name") or name,
                    "page": entry.get("page"),
                    "index": entry.get("index"),
                    "cell": cell_raw,
                    "old_text": old,
                    "new_text": new,
                }
            if not name:
                return self._tool_error("name is required.")
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
            log.exception("Could not set cell '%s' in table '%s'", cell_name, name)
            return self._tool_error("Could not set cell '%s' in table '%s': %s" % (cell_name, name, e))


class ManageTableStructure(ToolWriterTableBase):
    """Insert or delete one row/column. The four former skinny tools shared table_name + index."""

    name = "manage_table_structure"
    description = (
        "Insert or delete one table row or column. "
        "index is 0-based (for insert, equal to the current count appends at the end). "
        "Cannot delete the last remaining row or column."
    )
    is_mutation = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["insert", "delete"],
                "description": "insert or delete one band member.",
            },
            "axis": {
                "type": "string",
                "enum": ["row", "column"],
                "description": "Whether to edit rows or columns.",
            },
            "name": {"type": "string", "description": "Table name from table_list."},
            "index": {
                "type": "integer",
                "description": "0-based row or column index (insert at count = append).",
            },
            "page": {"type": "integer", "description": "Draw/Impress: 0-based page index."},
            "shape_index": {
                "type": "integer",
                "description": "Draw/Impress: table shape index (not the row/column index).",
            },
        },
        "required": ["action", "axis", "index"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action")
        axis_arg = kwargs.get("axis")
        if action not in ("insert", "delete"):
            return self._tool_error("action must be 'insert' or 'delete'.")
        if axis_arg not in ("row", "column"):
            return self._tool_error("axis must be 'row' or 'column'.")
        name = str(kwargs.get("name") or "").strip()
        raw = kwargs.get("index")
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            return self._tool_error("index must be an integer.")
        try:
            idx = int(raw)
        except ValueError:
            return self._tool_error("index must be an integer.")
        if idx < 0:
            return self._tool_error("index must be non-negative.")
        axis = "rows" if axis_arg == "row" else "columns"
        insert = action == "insert"
        try:
            if _is_draw_doc(ctx.doc):
                from plugin.draw.tables import manage_draw_structure, resolve_draw_table

                entry = resolve_draw_table(
                    ctx.doc,
                    name=name,
                    page=kwargs.get("page"),
                    index=kwargs.get("shape_index"),
                )
                rows, cols = manage_draw_structure(entry, str(action), str(axis_arg), idx)
                return {
                    "status": "ok",
                    "table_name": entry.get("name") or name,
                    "page": entry.get("page"),
                    "index": entry.get("index"),
                    "rows": rows,
                    "cols": cols,
                }
            if not name:
                return self._tool_error("name is required.")
            table = _get_table(ctx.doc, name)
            band = table.getRows() if axis == "rows" else table.getColumns()
            count = band.getCount()
            # insertByIndex(idx, n) inserts BEFORE idx (idx==count appends). removeByIndex needs a real
            # index, and removing the last row/column of a table is not allowed.
            if insert:
                if idx > count:
                    return self._tool_error(
                        "index %d out of range (table has %d %s; use 0..%d)."
                        % (idx, count, axis, count)
                    )
                band.insertByIndex(idx, 1)
            else:
                if idx >= count:
                    return self._tool_error(
                        "index %d out of range (table has %d %s; use 0..%d)."
                        % (idx, count, axis, count - 1)
                    )
                if count <= 1:
                    return self._tool_error("Cannot remove the last %s of a table." % axis[:-1])
                band.removeByIndex(idx, 1)
            rows, cols = _dims(table)
            return {"status": "ok", "table_name": name, "rows": rows, "cols": cols}
        except ValueError as ve:
            return self._tool_error(str(ve))
        except Exception as e:
            log.exception("Could not edit %s of table '%s'", axis, name)
            return self._tool_error("Could not edit %s of table '%s': %s" % (axis, name, e))


class TableInsert(ToolWriterTableBase):
    name = "table_insert"
    intent = "edit"
    description = (
        "Insert a table. Writer: text table at the view cursor (or document end). "
        "Draw/Impress: TableShape; position/size in 1/100 mm. Optional data is a 2D array of cell strings."
    )
    parameters = {
        "type": "object",
        "properties": {
            "rows": {"type": "integer", "description": "Number of rows"},
            "columns": {"type": "integer", "description": "Number of columns"},
            "data": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
                "description": "2D cell strings",
            },
            "page": {"type": "integer", "description": "Draw/Impress: 0-based page index (active if omitted)"},
            "x": {"type": "integer", "description": "Draw/Impress: X in 1/100 mm (default: 3000)"},
            "y": {"type": "integer", "description": "Draw/Impress: Y in 1/100 mm (default: 4000)"},
            "width": {"type": "integer", "description": "Draw/Impress: width in 1/100 mm (default: 20000)"},
            "height": {"type": "integer", "description": "Draw/Impress: height in 1/100 mm (default: 10000)"},
        },
        "required": ["rows", "columns"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        if _is_draw_doc(ctx.doc):
            from plugin.draw.tables import insert_draw_table
            from plugin.framework.errors import make_tool_error

            result = insert_draw_table(ctx, **kwargs)
            if result.get("status") != "ok":
                return make_tool_error(str(result.get("message") or "Insert failed"), code=str(result.get("code") or "TOOL_EXECUTION_ERROR"))
            return result

        rows = kwargs.get("rows")
        columns = kwargs.get("columns")
        if rows is None or columns is None:
            return self._tool_error("rows and columns are required.")
        rows = int(rows)
        columns = int(columns)
        if rows < 1 or columns < 1:
            return self._tool_error("rows and columns must be at least 1.")
        try:
            doc = ctx.doc
            table = doc.createInstance("com.sun.star.text.TextTable")
            table.initialize(rows, columns)
            text = doc.getText()
            cursor = None
            try:
                cursor = doc.getCurrentController().getViewCursor()
            except Exception:
                cursor = None
            if cursor is None:
                cursor = text.getEnd()
            text.insertTextContent(cursor, table, False)
            written = 0
            data = kwargs.get("data")
            if data:
                from plugin.draw.tables import fill_table_cells

                written = fill_table_cells(table, data)
            name = ""
            try:
                name = str(table.getName() if hasattr(table, "getName") else getattr(table, "Name", "") or "")
            except Exception:
                pass
            return {
                "status": "ok",
                "message": "Table inserted",
                "table_name": name,
                "rows": rows,
                "columns": columns,
                "cells_written": written,
            }
        except Exception as e:
            log.exception("Could not insert Writer table")
            return self._tool_error("Could not insert table: %s" % e)
