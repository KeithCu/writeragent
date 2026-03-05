"""Writer table tools."""

import logging

from plugin.framework.tool_base import ToolBase

log = logging.getLogger("localwriter.writer")


class ListTables(ToolBase):
    """List all text tables in the document."""

    name = "list_tables"
    intent = "edit"
    description = (
        "List all text tables in the document with their names "
        "and dimensions (rows x cols)."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    doc_types = ["writer"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        if not hasattr(doc, "getTextTables"):
            return {"status": "error", "message": "Document does not support text tables."}

        tables_sup = doc.getTextTables()
        tables = []
        for name in tables_sup.getElementNames():
            table = tables_sup.getByName(name)
            tables.append({
                "name": name,
                "rows": table.getRows().getCount(),
                "cols": table.getColumns().getCount(),
            })
        return {"status": "ok", "tables": tables, "count": len(tables)}


class ReadTable(ToolBase):
    """Read all cell contents from a named Writer table."""

    name = "read_table"
    intent = "edit"
    description = "Read all cell contents from a named Writer table as a 2D array."
    parameters = {
        "type": "object",
        "properties": {
            "table_name": {
                "type": "string",
                "description": "The table name from list_tables.",
            },
        },
        "required": ["table_name"],
    }
    doc_types = ["writer"]

    def execute(self, ctx, **kwargs):
        table_name = kwargs.get("table_name", "")
        if not table_name:
            return {"status": "error", "message": "table_name is required."}

        doc = ctx.doc
        tables_sup = doc.getTextTables()
        if not tables_sup.hasByName(table_name):
            available = list(tables_sup.getElementNames())
            return {
                "status": "error",
                "message": "Table '%s' not found." % table_name,
                "available": available,
            }

        table = tables_sup.getByName(table_name)
        rows = table.getRows().getCount()
        cols = table.getColumns().getCount()
        data = []
        for r in range(rows):
            row_data = []
            for c in range(cols):
                col_letter = _col_letter(c)
                cell_ref = "%s%d" % (col_letter, r + 1)
                try:
                    row_data.append(table.getCellByName(cell_ref).getString())
                except Exception:
                    row_data.append("")
            data.append(row_data)

        return {
            "status": "ok",
            "table_name": table_name,
            "rows": rows,
            "cols": cols,
            "data": data,
        }


class WriteTableCells(ToolBase):
    """Write a 2D block of values to a named Writer table."""

    name = "write_table_cells"
    intent = "edit"
    description = (
        "Write a 2D block of values to a named Writer table. "
        "Data is the same shape as read_table's data (array of rows, each row an array of values). "
        "Use start_cell (default A1) as the top-left corner. "
        "Numeric values are stored as numbers; others as text. Ragged rows are allowed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "table_name": {
                "type": "string",
                "description": "The table name from list_tables.",
            },
            "data": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"oneOf": [{"type": "string"}, {"type": "number"}]},
                },
                "description": "2D array of values (same shape as read_table data).",
            },
            "start_cell": {
                "type": "string",
                "description": "Top-left cell where data[0][0] is written (default A1).",
            },
        },
        "required": ["table_name", "data"],
    }
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        table_name = kwargs.get("table_name", "").strip()
        data = kwargs.get("data")
        start_cell = (kwargs.get("start_cell") or "A1").strip().upper()

        if not table_name:
            return {"status": "error", "message": "table_name is required."}
        if not data or not isinstance(data, list):
            return {"status": "error", "message": "data must be a non-empty array of rows."}
        if not any(isinstance(row, list) and len(row) > 0 for row in data):
            return {"status": "error", "message": "data must contain at least one row with at least one value."}

        parsed = _parse_cell(start_cell)
        if parsed is None:
            return {"status": "error", "message": "Invalid start_cell: %s (use Excel-style e.g. A1, B3)." % start_cell}
        start_row, start_col = parsed

        doc = ctx.doc
        tables_sup = doc.getTextTables()
        if not tables_sup.hasByName(table_name):
            available = list(tables_sup.getElementNames())
            return {
                "status": "error",
                "message": "Table '%s' not found." % table_name,
                "available": available,
            }

        table = tables_sup.getByName(table_name)
        table_rows = table.getRows().getCount()
        table_cols = table.getColumns().getCount()
        num_rows = len(data)
        num_cols = max(len(row) for row in data if isinstance(row, list)) if data else 0

        if start_row + num_rows > table_rows or start_col + num_cols > table_cols:
            return {
                "status": "error",
                "message": "Data block (rows=%d, cols=%d) at %s would exceed table %s dimensions (%d x %d)."
                % (num_rows, num_cols, start_cell, table_name, table_rows, table_cols),
                "table_rows": table_rows,
                "table_cols": table_cols,
            }

        cells_written = 0
        for r, row in enumerate(data):
            if not isinstance(row, list):
                continue
            for c, value in enumerate(row):
                cell_row = start_row + r
                cell_col = start_col + c
                cell_ref = _col_letter(cell_col) + str(cell_row + 1)
                try:
                    cell_obj = table.getCellByName(cell_ref)
                    try:
                        cell_obj.setValue(float(value))
                    except (ValueError, TypeError):
                        cell_obj.setString(str(value))
                    cells_written += 1
                except Exception as e:
                    log.warning("write_table_cells: failed to write %s: %s", cell_ref, e)
                    return {
                        "status": "error",
                        "message": "Failed to write cell %s: %s" % (cell_ref, e),
                        "cells_written": cells_written,
                    }

        end_cell = _col_letter(start_col + num_cols - 1) + str(start_row + num_rows)
        return {
            "status": "ok",
            "table_name": table_name,
            "cells_written": cells_written,
            "start_cell": start_cell,
            "end_cell": end_cell,
        }


class CreateTable(ToolBase):
    """Create a new table at a paragraph position."""

    name = "create_table"
    intent = "edit"
    description = (
        "Create a new table at a paragraph position. "
        "The table is inserted relative to the target paragraph. "
        "Provide either a locator string or a paragraph_index."
    )
    parameters = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "integer",
                "description": "Number of rows.",
            },
            "cols": {
                "type": "integer",
                "description": "Number of columns.",
            },
            "paragraph_index": {
                "type": "integer",
                "description": "Paragraph index for insertion point.",
            },
            "locator": {
                "type": "string",
                "description": (
                    "Unified locator for insertion point "
                    "(e.g. 'bookmark:NAME', 'heading_text:Title')."
                ),
            },
            "position": {
                "type": "string",
                "enum": ["before", "after"],
                "description": (
                    "Insert before or after the target paragraph "
                    "(default: after)."
                ),
            },
        },
        "required": ["rows", "cols"],
    }
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        rows = kwargs.get("rows")
        cols = kwargs.get("cols")
        if not rows or not cols:
            return {"status": "error", "message": "rows and cols are required."}
        if rows < 1 or cols < 1:
            return {"status": "error", "message": "rows and cols must be >= 1."}

        paragraph_index = kwargs.get("paragraph_index")
        locator = kwargs.get("locator")
        position = kwargs.get("position", "after")

        doc = ctx.doc
        doc_svc = ctx.services.document

        try:
            # Resolve locator to paragraph index
            if locator is not None and paragraph_index is None:
                resolved = doc_svc.resolve_locator(doc, locator)
                paragraph_index = resolved.get("para_index")

            if paragraph_index is None:
                return {
                    "status": "error",
                    "message": "Provide locator or paragraph_index.",
                }

            # Find the target paragraph element
            target, _ = doc_svc.find_paragraph_element(doc, paragraph_index)
            if target is None:
                return {
                    "status": "error",
                    "message": "Paragraph %d not found." % paragraph_index,
                }

            # Create and insert the table
            table = doc.createInstance("com.sun.star.text.TextTable")
            table.initialize(rows, cols)

            doc_text = doc.getText()
            if position == "before":
                cursor = doc_text.createTextCursorByRange(target.getStart())
            else:
                cursor = doc_text.createTextCursorByRange(target.getEnd())

            doc_text.insertTextContent(cursor, table, False)

            table_name = table.getName()

            return {
                "status": "ok",
                "table_name": table_name,
                "rows": rows,
                "cols": cols,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_cell(ref):
    """Parse Excel-style cell reference to 0-based (row, col). Returns None if invalid.

    Examples: A1 -> (0, 0), B3 -> (2, 1), AA10 -> (9, 26).
    """
    if not ref or not isinstance(ref, str):
        return None
    ref = ref.strip().upper()
    if not ref:
        return None
    i = 0
    while i < len(ref) and ref[i].isalpha():
        i += 1
    col_letters = ref[:i]
    row_digits = ref[i:]
    if not col_letters or not row_digits or not row_digits.isdigit():
        return None
    col_0 = 0
    for ch in col_letters:
        col_0 = col_0 * 26 + (ord(ch) - ord("A") + 1)
    col_0 -= 1
    row_0 = int(row_digits) - 1
    if row_0 < 0 or col_0 < 0:
        return None
    return (row_0, col_0)


def _col_letter(c):
    """Convert 0-based column index to Excel-style letter(s)."""
    if c < 26:
        return chr(ord("A") + c)
    return "A" + chr(ord("A") + c - 26)
