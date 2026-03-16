# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Writer table tools."""

import logging


log = logging.getLogger("writeragent.writer")


# class ListTables(ToolBase):
#     """List all text tables in the document."""

#     name = "list_tables"
#     intent = "edit"
#     description = (
#         "List all text tables in the document with their names "
#         "and dimensions (rows x cols)."
#     )
#     parameters = {
#         "type": "object",
#         "properties": {},
#         "required": [],
#     }
#     doc_types = ["writer"]

#     def execute(self, ctx, **kwargs):
#         doc = ctx.doc
#         tables_sup = self.get_collection(doc, "getTextTables", "Document does not support text tables.")
#         if isinstance(tables_sup, dict):
#             return tables_sup

#         tables = []
#         for name in tables_sup.getElementNames():
#             table = tables_sup.getByName(name)
#             tables.append({
#                 "name": name,
#                 "rows": table.getRows().getCount(),
#                 "cols": table.getColumns().getCount(),
#             })
#         return {"status": "ok", "tables": tables, "count": len(tables)}


# class ReadTable(ToolBase):
#     """Read all cell contents from a named Writer table."""

#     name = "read_table"
#     intent = "edit"
#     description = "Read all cell contents from a named Writer table as a 2D array."
#     parameters = {
#         "type": "object",
#         "properties": {
#             "table_name": {
#                 "type": "string",
#                 "description": "The table name from list_tables.",
#             },
#         },
#         "required": ["table_name"],
#     }
#     doc_types = ["writer"]

#     def execute(self, ctx, **kwargs):
#         table_name = kwargs.get("table_name", "")

#         table = self.get_item(
#             ctx.doc, "getTextTables", table_name,
#             missing_msg="Document does not support text tables.",
#             not_found_msg="Table '%s' not found." % table_name
#         )
#         if isinstance(table, dict):
#             return table

#         rows = table.getRows().getCount()
#         cols = table.getColumns().getCount()

#         try:
#             # Fetch all data in a single API call for performance
#             raw_data = table.getDataArray()
#             # getDataArray returns tuple of tuples; original API returned list of lists of strings
#             data = [[str(cell) if cell is not None else "" for cell in row] for row in raw_data]
#         except Exception as e:
#             log.warning("getDataArray failed on table %s: %s. Falling back to cell-by-cell.", table_name, e)
#             data = []
#             for r in range(rows):
#                 row_data = []
#                 for c in range(cols):
#                     col_letter = _col_letter(c)
#                     cell_ref = "%s%d" % (col_letter, r + 1)
#                     try:
#                         row_data.append(table.getCellByName(cell_ref).getString())
#                     except Exception:
#                         row_data.append("")
#                 data.append(row_data)

#         return {
#             "status": "ok",
#             "table_name": table_name,
#             "rows": rows,
#             "cols": cols,
#             "data": data,
#         }


# class WriteTableCells(ToolBase):
#     """Write a 2D block of values to a named Writer table."""

#     name = "write_table_cells"
#     intent = "edit"
#     description = (
#         "Write a 2D block of values to a named Writer table. "
#         "Data is the same shape as read_table's data (array of rows, each row an array of values). "
#         "Use start_cell (default A1) as the top-left corner. "
#         "Numeric values are stored as numbers; others as text. Ragged rows are allowed."
#     )
#     parameters = {
#         "type": "object",
#         "properties": {
#             "table_name": {
#                 "type": "string",
#                 "description": "The table name from list_tables.",
#             },
#             "data": {
#                 "type": "array",
#                 "items": {
#                     "type": "array",
#                     "items": {"oneOf": [{"type": "string"}, {"type": "number"}]},
#                 },
#                 "description": "2D array of values (same shape as read_table data).",
#             },
#             "start_cell": {
#                 "type": "string",
#                 "description": "Top-left cell where data[0][0] is written (default A1).",
#             },
#         },
#         "required": ["table_name", "data"],
#     }
#     doc_types = ["writer"]
#     is_mutation = True

#     def execute(self, ctx, **kwargs):
#         table_name = kwargs.get("table_name", "").strip()
#         data = kwargs.get("data")
#         start_cell = (kwargs.get("start_cell") or "A1").strip().upper()

#         if not data or not isinstance(data, list):
#             return {"status": "error", "message": "data must be a non-empty array of rows."}
#         if not any(isinstance(row, list) and len(row) > 0 for row in data):
#             return {"status": "error", "message": "data must contain at least one row with at least one value."}

#         parsed = _parse_cell(start_cell)
#         if parsed is None:
#             return {"status": "error", "message": "Invalid start_cell: %s (use Excel-style e.g. A1, B3)." % start_cell}
#         start_row, start_col = parsed

#         table = self.get_item(
#             ctx.doc, "getTextTables", table_name,
#             missing_msg="Document does not support text tables.",
#             not_found_msg="Table '%s' not found." % table_name
#         )
#         if isinstance(table, dict):
#             return table

#         table_rows = table.getRows().getCount()
#         table_cols = table.getColumns().getCount()
#         num_rows = len(data)
#         num_cols = max(len(row) for row in data if isinstance(row, list)) if data else 0

#         if start_row + num_rows > table_rows or start_col + num_cols > table_cols:
#             return {
#                 "status": "error",
#                 "message": "Data block (rows=%d, cols=%d) at %s would exceed table %s dimensions (%d x %d)."
#                 % (num_rows, num_cols, start_cell, table_name, table_rows, table_cols),
#                 "table_rows": table_rows,
#                 "table_cols": table_cols,
#             }

#         cells_written = 0
#         for r, row in enumerate(data):
#             if not isinstance(row, list):
#                 continue
#             for c, value in enumerate(row):
#                 cell_row = start_row + r
#                 cell_col = start_col + c
#                 cell_ref = _col_letter(cell_col) + str(cell_row + 1)
#                 try:
#                     cell_obj = table.getCellByName(cell_ref)
#                     try:
#                         cell_obj.setValue(float(value))
#                     except (ValueError, TypeError):
#                         cell_obj.setString(str(value))
#                     cells_written += 1
#                 except Exception as e:
#                     log.warning("write_table_cells: failed to write %s: %s", cell_ref, e)
#                     return {
#                         "status": "error",
#                         "message": "Failed to write cell %s: %s" % (cell_ref, e),
#                         "cells_written": cells_written,
#                     }

#         end_cell = _col_letter(start_col + num_cols - 1) + str(start_row + num_rows)
#         return {
#             "status": "ok",
#             "table_name": table_name,
#             "cells_written": cells_written,
#             "start_cell": start_cell,
#             "end_cell": end_cell,
#         }


# class CreateTable(ToolBase):
#     """Create a new table at a paragraph position."""

#     name = "create_table"
#     intent = "edit"
#     description = (
#         "Create a new table at a paragraph position. "
#         "The table is inserted relative to the target paragraph. "
#         "Provide either a locator string or a paragraph_index."
#     )
#     parameters = {
#         "type": "object",
#         "properties": {
#             "rows": {
#                 "type": "integer",
#                 "description": "Number of rows.",
#             },
#             "cols": {
#                 "type": "integer",
#                 "description": "Number of columns.",
#             },
#             "paragraph_index": {
#                 "type": "integer",
#                 "description": "Paragraph index for insertion point.",
#             },
#             "locator": {
#                 "type": "string",
#                 "description": (
#                     "Unified locator for insertion point "
#                     "(e.g. 'bookmark:NAME', 'heading_text:Title')."
#                 ),
#             },
#             "position": {
#                 "type": "string",
#                 "enum": ["before", "after"],
#                 "description": (
#                     "Insert before or after the target paragraph "
#                     "(default: after)."
#                 ),
#             },
#         },
#         "required": ["rows", "cols"],
#     }
#     doc_types = ["writer"]
#     is_mutation = True

#     def execute(self, ctx, **kwargs):
#         rows = kwargs.get("rows")
#         cols = kwargs.get("cols")
#         if rows < 1 or cols < 1:
#             return {"status": "error", "message": "rows and cols must be >= 1."}

#         paragraph_index = kwargs.get("paragraph_index")
#         locator = kwargs.get("locator")
#         position = kwargs.get("position", "after")

#         doc = ctx.doc
#         doc_svc = ctx.services.document

#         # Resolve locator to paragraph index
#         if locator is not None and paragraph_index is None:
#             resolved = doc_svc.resolve_locator(doc, locator)
#             paragraph_index = resolved.get("para_index")

#         if paragraph_index is None:
#             return {
#                 "status": "error",
#                 "message": "Provide locator or paragraph_index.",
#             }

#         # Find the target paragraph element
#         target, _ = doc_svc.find_paragraph_element(doc, paragraph_index)
#         if target is None:
#             return {
#                 "status": "error",
#                 "message": "Paragraph %d not found." % paragraph_index,
#             }

#         # Create and insert the table
#         table = doc.createInstance("com.sun.star.text.TextTable")
#         table.initialize(rows, cols)

#         doc_text = doc.getText()
#         if position == "before":
#             cursor = doc_text.createTextCursorByRange(target.getStart())
#         else:
#             cursor = doc_text.createTextCursorByRange(target.getEnd())

#         doc_text.insertTextContent(cursor, table, False)

#         table_name = table.getName()

#         return {
#             "status": "ok",
#             "table_name": table_name,
#             "rows": rows,
#             "cols": cols,
#         }


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


# class DeleteTable(ToolBase):
#     """Delete a table from the document."""

#     name = "delete_table"
#     intent = "edit"
#     description = "Delete a named table from the Writer document."
#     parameters = {
#         "type": "object",
#         "properties": {
#             "table_name": {
#                 "type": "string",
#                 "description": "The table name from list_tables.",
#             },
#         },
#         "required": ["table_name"],
#     }
#     doc_types = ["writer"]
#     is_mutation = True

#     def execute(self, ctx, **kwargs):
#         table_name = kwargs.get("table_name", "")
#         doc = ctx.doc
#         tables_sup = doc.getTextTables()
#         if not tables_sup.hasByName(table_name):
#             return {"status": "error", "message": "Table '%s' not found." % table_name}

#         table = tables_sup.getByName(table_name)
#         try:
#             anchor = table.getAnchor()
#             text = anchor.getText()
#             text.removeTextContent(table)
#             return {"status": "ok", "deleted": table_name}
#         except Exception as e:
#             return {"status": "error", "error": str(e)}


# # ------------------------------------------------------------------
# # SetTableProperties
# # ------------------------------------------------------------------

# class SetTableProperties(ToolBase):
#     """Set table layout properties: width, alignment, equal columns."""

#     name = "set_table_properties"
#     intent = "edit"
#     description = (
#         "Set layout properties on a Writer table: width, alignment, "
#         "equal-width columns, repeat header row, background color. "
#         "Use equal_columns=true to make all columns the same width."
#     )
#     parameters = {
#         "type": "object",
#         "properties": {
#             "table_name": {
#                 "type": "string",
#                 "description": "The table name from list_tables.",
#             },
#             "width_mm": {
#                 "type": "number",
#                 "description": "Table width in millimetres.",
#             },
#             "equal_columns": {
#                 "type": "boolean",
#                 "description": "Set all columns to equal width (default: false).",
#             },
#             "column_widths": {
#                 "type": "array",
#                 "items": {"type": "number"},
#                 "description": (
#                     "Relative column widths (e.g. [1, 2, 1] = 25%/50%/25%). "
#                     "Number of values must match number of columns."
#                 ),
#             },
#             "alignment": {
#                 "type": "string",
#                 "enum": ["left", "center", "right", "full"],
#                 "description": "Horizontal alignment (default: full = stretch to margins).",
#             },
#             "repeat_header": {
#                 "type": "boolean",
#                 "description": "Repeat first row as header on each page.",
#             },
#             "header_rows": {
#                 "type": "integer",
#                 "description": "Number of header rows to repeat (default: 1).",
#             },
#             "bg_color": {
#                 "type": "string",
#                 "description": "Background color as hex (#RRGGBB) or name.",
#             },
#         },
#         "required": ["table_name"],
#     }
#     doc_types = ["writer"]
#     is_mutation = True

#     def execute(self, ctx, **kwargs):
#         table_name = kwargs.get("table_name", "")
#         doc = ctx.doc
#         tables_sup = doc.getTextTables()
#         if not tables_sup.hasByName(table_name):
#             return {"status": "error", "message": "Table '%s' not found." % table_name}

#         table = tables_sup.getByName(table_name)
#         updated = []

#         # Width
#         width_mm = kwargs.get("width_mm")
#         if width_mm is not None:
#             table.setPropertyValue("Width", int(width_mm * 100))
#             updated.append("width")

#         # Alignment
#         alignment = kwargs.get("alignment")
#         if alignment is not None:
#             # HoriOrientation: 0=NONE, 1=RIGHT, 2=CENTER, 3=LEFT, 4=FULL
#             align_map = {"left": 3, "center": 2, "right": 1, "full": 4, "none": 0}
#             if alignment in align_map:
#                 table.setPropertyValue("HoriOrient", align_map[alignment])
#                 updated.append("alignment")

#         # Column widths (equal or custom ratios)
#         equal = kwargs.get("equal_columns", False)
#         custom_widths = kwargs.get("column_widths")

#         if equal or custom_widths:
#             try:
#                 cols = table.getColumns().getCount()
#                 rel_sum = table.getPropertyValue("TableColumnRelativeSum")
#                 seps = list(table.getPropertyValue("TableColumnSeparators"))

#                 if cols < 2:
#                     pass  # single column, nothing to adjust
#                 elif equal:
#                     # Equal-width: place separators at even intervals
#                     for i in range(len(seps)):
#                         seps[i].Position = int(rel_sum * (i + 1) / cols)
#                     table.setPropertyValue("TableColumnSeparators", tuple(seps))
#                     updated.append("equal_columns")
#                 elif custom_widths and len(custom_widths) == cols:
#                     # Custom ratios
#                     total = sum(custom_widths)
#                     cumulative = 0
#                     for i in range(len(seps)):
#                         cumulative += custom_widths[i]
#                         seps[i].Position = int(rel_sum * cumulative / total)
#                     table.setPropertyValue("TableColumnSeparators", tuple(seps))
#                     updated.append("column_widths")
#                 elif custom_widths:
#                     return {
#                         "status": "error",
#                         "message": "column_widths length (%d) != column count (%d)" % (
#                             len(custom_widths), cols),
#                     }
#             except Exception as e:
#                 log.debug("set_table_properties: column adjust failed: %s", e)

#         # Repeat header
#         repeat = kwargs.get("repeat_header")
#         if repeat is not None:
#             table.setPropertyValue("RepeatHeadline", bool(repeat))
#             updated.append("repeat_header")

#         header_rows = kwargs.get("header_rows")
#         if header_rows is not None:
#             try:
#                 table.setPropertyValue("HeaderRowCount", int(header_rows))
#                 updated.append("header_rows")
#             except Exception:
#                 pass

#         # Background color
#         bg_color = kwargs.get("bg_color")
#         if bg_color is not None:
#             color_val = _parse_color(bg_color)
#             if color_val is not None:
#                 table.setPropertyValue("BackTransparent", False)
#                 table.setPropertyValue("BackColor", color_val)
#                 updated.append("bg_color")

#         return {"status": "ok", "table_name": table_name, "updated": updated}


# # ------------------------------------------------------------------
# # AddTableRows / AddTableColumns
# # ------------------------------------------------------------------

# class AddTableRows(ToolBase):
#     """Add rows to a Writer table."""

#     name = "add_table_rows"
#     intent = "edit"
#     description = "Insert one or more rows into a Writer table at a given position."
#     parameters = {
#         "type": "object",
#         "properties": {
#             "table_name": {
#                 "type": "string",
#                 "description": "The table name.",
#             },
#             "count": {
#                 "type": "integer",
#                 "description": "Number of rows to add (default: 1).",
#             },
#             "at_index": {
#                 "type": "integer",
#                 "description": "Row index to insert before (appends at end if omitted).",
#             },
#         },
#         "required": ["table_name"],
#     }
#     doc_types = ["writer"]
#     is_mutation = True

#     def execute(self, ctx, **kwargs):
#         table_name = kwargs.get("table_name", "")
#         doc = ctx.doc
#         tables_sup = doc.getTextTables()
#         if not tables_sup.hasByName(table_name):
#             return {"status": "error", "message": "Table '%s' not found." % table_name}

#         table = tables_sup.getByName(table_name)
#         rows = table.getRows()
#         count = kwargs.get("count", 1)
#         at_index = kwargs.get("at_index")
#         if at_index is None:
#             at_index = rows.getCount()

#         try:
#             rows.insertByIndex(at_index, count)
#             return {
#                 "status": "ok",
#                 "table_name": table_name,
#                 "rows_added": count,
#                 "at_index": at_index,
#                 "total_rows": rows.getCount(),
#             }
#         except Exception as e:
#             return {"status": "error", "error": str(e)}


# class AddTableColumns(ToolBase):
#     """Add columns to a Writer table."""

#     name = "add_table_columns"
#     intent = "edit"
#     description = "Insert one or more columns into a Writer table at a given position."
#     parameters = {
#         "type": "object",
#         "properties": {
#             "table_name": {
#                 "type": "string",
#                 "description": "The table name.",
#             },
#             "count": {
#                 "type": "integer",
#                 "description": "Number of columns to add (default: 1).",
#             },
#             "at_index": {
#                 "type": "integer",
#                 "description": "Column index to insert before (appends at end if omitted).",
#             },
#         },
#         "required": ["table_name"],
#     }
#     doc_types = ["writer"]
#     is_mutation = True

#     def execute(self, ctx, **kwargs):
#         table_name = kwargs.get("table_name", "")
#         doc = ctx.doc
#         tables_sup = doc.getTextTables()
#         if not tables_sup.hasByName(table_name):
#             return {"status": "error", "message": "Table '%s' not found." % table_name}

#         table = tables_sup.getByName(table_name)
#         cols = table.getColumns()
#         count = kwargs.get("count", 1)
#         at_index = kwargs.get("at_index")
#         if at_index is None:
#             at_index = cols.getCount()

#         try:
#             cols.insertByIndex(at_index, count)
#             return {
#                 "status": "ok",
#                 "table_name": table_name,
#                 "columns_added": count,
#                 "at_index": at_index,
#                 "total_columns": cols.getCount(),
#             }
#         except Exception as e:
#             return {"status": "error", "error": str(e)}


# # ------------------------------------------------------------------
# # DeleteTableRows / DeleteTableColumns
# # ------------------------------------------------------------------

# class DeleteTableRows(ToolBase):
#     """Delete rows from a Writer table."""

#     name = "delete_table_rows"
#     intent = "edit"
#     description = "Delete one or more rows from a Writer table."
#     parameters = {
#         "type": "object",
#         "properties": {
#             "table_name": {
#                 "type": "string",
#                 "description": "The table name.",
#             },
#             "at_index": {
#                 "type": "integer",
#                 "description": "First row index to delete.",
#             },
#             "count": {
#                 "type": "integer",
#                 "description": "Number of rows to delete (default: 1).",
#             },
#         },
#         "required": ["table_name", "at_index"],
#     }
#     doc_types = ["writer"]
#     is_mutation = True

#     def execute(self, ctx, **kwargs):
#         table_name = kwargs.get("table_name", "")
#         doc = ctx.doc
#         tables_sup = doc.getTextTables()
#         if not tables_sup.hasByName(table_name):
#             return {"status": "error", "message": "Table '%s' not found." % table_name}

#         table = tables_sup.getByName(table_name)
#         rows = table.getRows()
#         at_index = kwargs["at_index"]
#         count = kwargs.get("count", 1)

#         try:
#             rows.removeByIndex(at_index, count)
#             return {
#                 "status": "ok",
#                 "table_name": table_name,
#                 "rows_deleted": count,
#                 "total_rows": rows.getCount(),
#             }
#         except Exception as e:
#             return {"status": "error", "error": str(e)}


# class DeleteTableColumns(ToolBase):
#     """Delete columns from a Writer table."""

#     name = "delete_table_columns"
#     intent = "edit"
#     description = "Delete one or more columns from a Writer table."
#     parameters = {
#         "type": "object",
#         "properties": {
#             "table_name": {
#                 "type": "string",
#                 "description": "The table name.",
#             },
#             "at_index": {
#                 "type": "integer",
#                 "description": "First column index to delete.",
#             },
#             "count": {
#                 "type": "integer",
#                 "description": "Number of columns to delete (default: 1).",
#             },
#         },
#         "required": ["table_name", "at_index"],
#     }
#     doc_types = ["writer"]
#     is_mutation = True

#     def execute(self, ctx, **kwargs):
#         table_name = kwargs.get("table_name", "")
#         doc = ctx.doc
#         tables_sup = doc.getTextTables()
#         if not tables_sup.hasByName(table_name):
#             return {"status": "error", "message": "Table '%s' not found." % table_name}

#         table = tables_sup.getByName(table_name)
#         cols = table.getColumns()
#         at_index = kwargs["at_index"]
#         count = kwargs.get("count", 1)

#         try:
#             cols.removeByIndex(at_index, count)
#             return {
#                 "status": "ok",
#                 "table_name": table_name,
#                 "columns_deleted": count,
#                 "total_columns": cols.getCount(),
#             }
#         except Exception as e:
#             return {"status": "error", "error": str(e)}


# # ------------------------------------------------------------------
# # WriteTableRow (batch write)
# # ------------------------------------------------------------------

# class WriteTableRow(ToolBase):
#     """Write a full row of values to a Writer table."""

#     name = "write_table_row"
#     intent = "edit"
#     description = (
#         "Write a full row of values to a Writer table in one call. "
#         "More efficient than calling write_table_cell for each cell."
#     )
#     parameters = {
#         "type": "object",
#         "properties": {
#             "table_name": {
#                 "type": "string",
#                 "description": "The table name.",
#             },
#             "row": {
#                 "type": "integer",
#                 "description": "0-based row index.",
#             },
#             "values": {
#                 "type": "array",
#                 "items": {"type": "string"},
#                 "description": "Values for each column (left to right).",
#             },
#         },
#         "required": ["table_name", "row", "values"],
#     }
#     doc_types = ["writer"]
#     is_mutation = True

#     def execute(self, ctx, **kwargs):
#         table_name = kwargs.get("table_name", "")
#         row_idx = kwargs.get("row", 0)
#         values = kwargs.get("values", [])

#         doc = ctx.doc
#         tables_sup = doc.getTextTables()
#         if not tables_sup.hasByName(table_name):
#             return {"status": "error", "message": "Table '%s' not found." % table_name}

#         table = tables_sup.getByName(table_name)
#         cols = table.getColumns().getCount()

#         written = 0
#         for c in range(min(len(values), cols)):
#             col_letter = _col_letter(c)
#             cell_ref = "%s%d" % (col_letter, row_idx + 1)
#             cell_obj = table.getCellByName(cell_ref)
#             if cell_obj is None:
#                 continue
#             val = values[c]
#             try:
#                 cell_obj.setValue(float(val))
#             except (ValueError, TypeError):
#                 cell_obj.setString(str(val))
#             written += 1

#         return {
#             "status": "ok",
#             "table_name": table_name,
#             "row": row_idx,
#             "cells_written": written,
#         }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _col_letter(c):
    """Convert 0-based column index to Excel-style letter(s)."""
    if c < 26:
        return chr(ord("A") + c)
    return "A" + chr(ord("A") + c - 26)


def _parse_color(color_str):
    """Parse a color string (hex or name) to integer."""
    if not color_str:
        return None
    color_str = color_str.strip().lower()
    names = {
        "red": 0xFF0000, "green": 0x00FF00, "blue": 0x0000FF,
        "yellow": 0xFFFF00, "white": 0xFFFFFF, "black": 0x000000,
        "orange": 0xFF8C00, "gray": 0x808080,
    }
    if color_str in names:
        return names[color_str]
    if color_str.startswith("#"):
        try:
            return int(color_str[1:], 16)
        except ValueError:
            return None
    return None
